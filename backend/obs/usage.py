"""Usage capture (FR-32) and turn-metric capture (FR-33).

Per-session, so per-user: the recorder is created with the verified user_id
at session start and stamps it on every row; flushes go through
user_scoped_session, so RLS applies to this writer like any other (FR-31).
Everything rides the BackgroundBatchWriter — the hot path only enqueues
(NFR-10).

Turn identity (FR-33): the recorder's `current_turn` is set by the pipeline's
TurnTrackingObserver events, wired in CompanionAgent. Usage arriving between
turns (or a turn interrupted before first audio) still records — spent is
recorded — with whatever turn number is current.
"""

import asyncio
from datetime import datetime, timezone

from loguru import logger
from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import TTSUsageMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection

from db.batch_writer import BackgroundBatchWriter
from db.engine import user_scoped_session
from db.models import TurnMetric, UsageEvent

# FR-47: how long after the loop's turn end a late latency measurement may
# still arrive (the stream closes upstream of TTS, so on a one-sentence
# turn the loop finishes ~100ms before first audio — the common case).
MEASUREMENT_WINDOW_S = 2.0

# C-1's per-stage advisory, transferred from the displaced breakdown
# handler to the loop's step entries (FR-47).
STEP_STAGE_WARN_MS = 1000

# Sentinel: "sample current_turn at enqueue" — the legacy metrics-frame
# path. The loop always passes its own turn number explicitly (FR-47: a
# cancelled step's late usage must land on the turn that spent it).
_SAMPLE = object()


class UsageRecorder:
    """One per session. Builds metadata-only rows and hands them to the
    background writer; never touches the DB on the calling path.

    FR-47 adds the per-turn metrics buffer: the loop records step-indexed
    stage entries against an explicit turn number, the latency observer
    hands in its measurement (turn captured WITH it), and the ONE
    turn_metrics row flushes when both signals for a turn have arrived —
    gated on the measurement existing (greeting / pre-audio interruption →
    no row, buffer cleared)."""

    def __init__(self, session_id: str, user_id: str):
        self._session_id = session_id
        self._user_id = user_id
        self.current_turn: int | None = None
        self._log = logger.bind(session_id=session_id, component="obs.usage")
        self._writer = BackgroundBatchWriter(self._flush, self._log)
        # FR-47 per-turn buffer, keyed by turn number so entries can never
        # credit a neighbouring turn's row.
        self._turn_stages: dict[int, dict[str, int]] = {}
        self._measurements: dict[int, int] = {}
        self._awaiting: dict[int, asyncio.TimerHandle] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._writer.start()

    async def stop(self) -> None:
        # Teardown mid-turn is a turn end (FR-47): flush any turn whose
        # measurement exists BEFORE the writer stops — End-tap mid-response
        # must not lose a row whose measurement was already taken.
        for turn_id in list(self._measurements):
            self._flush_turn_row(turn_id)
        for timer in self._awaiting.values():
            timer.cancel()
        self._awaiting.clear()
        self._turn_stages.clear()
        await self._writer.stop()

    # -- hot-path recording (enqueue only) ---------------------------------

    def record_llm_usage(
        self, prompt_tokens: int, completion_tokens: int, *, turn_id=_SAMPLE
    ) -> None:
        turn = self.current_turn if turn_id is _SAMPLE else turn_id
        now = datetime.now(timezone.utc)
        if prompt_tokens:
            self._writer.enqueue(
                self._event("llm", "tokens_in", prompt_tokens, now, turn)
            )
        if completion_tokens:
            self._writer.enqueue(
                self._event("llm", "tokens_out", completion_tokens, now, turn)
            )

    def record_tts_characters(self, characters: int) -> None:
        if characters:
            self._writer.enqueue(
                self._event(
                    "tts",
                    "characters",
                    characters,
                    datetime.now(timezone.utc),
                    self.current_turn,
                )
            )

    def record_stt_seconds(self, seconds: float) -> None:
        """Session-level (turn_id None): the FR-32 streamed-time proxy."""
        self._writer.enqueue(
            UsageEvent(
                user_id=self._user_id,
                session_id=self._session_id,
                turn_id=None,
                ts=datetime.now(timezone.utc),
                stage="stt",
                unit="seconds",
                quantity=float(seconds),
            )
        )

    # -- FR-47 per-turn metrics buffer (loop + latency observer) -----------

    def record_step_stage(self, turn_id: int | None, key: str, ms: int) -> None:
        """A step-indexed stage entry from the loop (step.N.ttfb.llm,
        step.N.tool.name, step.N.filler) against the turn EXPLICITLY named
        by the step that measured it. C-1's over-1s advisory transfers
        here from the displaced breakdown handler."""
        if turn_id is None:
            return  # no turn identity (tracker unavailable) — logs only
        self._turn_stages.setdefault(turn_id, {})[key] = ms
        if ms > STEP_STAGE_WARN_MS:
            self._log.bind(
                event="turn.stage_slow", turn_id=turn_id, stage=key, duration_ms=ms
            ).warning(f"stage {key} took {ms}ms (over {STEP_STAGE_WARN_MS}ms, C-1)")

    def record_measurement(self, e2e_ms: int) -> None:
        """The latency observer's half of the FR-47 handoff, called at
        first audio mid-turn. The turn number is captured HERE, with the
        measurement — the tracker advances before an interrupted flush, so
        sampling at write time would credit the wrong turn."""
        turn_id = self.current_turn
        if turn_id is None:
            return
        self._measurements[turn_id] = e2e_ms
        if self._awaiting.pop(turn_id, None) is not None:
            # the loop's turn end already passed — this was the last signal
            self._flush_turn_row(turn_id)

    def turn_ended(self, turn_id: int | None) -> None:
        """The loop's half: its turn end (ANY path — natural, interrupted,
        fallback). Flush if the measurement already landed; otherwise wait
        a bounded window for it. Either way, entries for OLDER turns can
        never flush now — cleared, so a no-row turn cannot credit the next
        turn's row."""
        for stale in [t for t in self._turn_stages if turn_id is None or t < turn_id]:
            del self._turn_stages[stale]
        for stale in [t for t in self._measurements if turn_id is None or t < turn_id]:
            self._measurements.pop(stale, None)
        if turn_id is None:
            return
        if turn_id in self._measurements:
            self._flush_turn_row(turn_id)
        elif turn_id not in self._awaiting:
            try:
                self._awaiting[turn_id] = asyncio.get_running_loop().call_later(
                    MEASUREMENT_WINDOW_S, self._expire_turn, turn_id
                )
            except RuntimeError:  # no running loop (sync test path): no wait
                self._expire_turn(turn_id)

    def _expire_turn(self, turn_id: int) -> None:
        # No measurement within the window: the no-row path (greeting, or a
        # turn interrupted before audio). Steps live in agent.step logs only.
        self._awaiting.pop(turn_id, None)
        self._turn_stages.pop(turn_id, None)
        self._log.bind(event="turn_metrics.no_measurement", turn_id=turn_id).debug(
            "turn ended without a latency measurement — no row"
        )

    def _flush_turn_row(self, turn_id: int) -> None:
        e2e_ms = self._measurements.pop(turn_id)
        stages = self._turn_stages.pop(turn_id, {})
        timer = self._awaiting.pop(turn_id, None)
        if timer is not None:
            timer.cancel()
        self._writer.enqueue(
            TurnMetric(
                user_id=self._user_id,
                session_id=self._session_id,
                turn_id=turn_id,
                ts=datetime.now(timezone.utc),
                eot_to_first_audio_ms=e2e_ms,
                stages_ms=stages or None,
            )
        )

    # -- internals ---------------------------------------------------------

    def _event(
        self, stage: str, unit: str, quantity: float, ts, turn_id
    ) -> UsageEvent:
        return UsageEvent(
            user_id=self._user_id,
            session_id=self._session_id,
            turn_id=turn_id,
            ts=ts,
            stage=stage,
            unit=unit,
            quantity=float(quantity),
        )

    async def _flush(self, rows: list) -> None:
        async with user_scoped_session(self._user_id) as db:
            db.add_all(rows)
            await db.commit()


class UsageMetricsObserver(BaseObserver):
    """Taps MetricsFrames for the usage the pipeline already emits
    (enable_usage_metrics=True) and enqueues via the recorder.

    TTS ONLY (FR-47): LLM usage is recorded in exactly one place — the
    loop, directly from the provider's response — so nothing can
    double-count even if a future stage emits LLM metrics frames."""

    def __init__(self, recorder: UsageRecorder, **kwargs):
        super().__init__(**kwargs)
        self._recorder = recorder
        self._seen: set = set()

    async def on_push_frame(self, data: FramePushed):
        if data.direction != FrameDirection.DOWNSTREAM:
            return
        frame = data.frame
        if not isinstance(frame, MetricsFrame) or frame.id in self._seen:
            return
        self._seen.add(frame.id)
        for metric in frame.data:
            if isinstance(metric, TTSUsageMetricsData):
                self._recorder.record_tts_characters(metric.value or 0)
