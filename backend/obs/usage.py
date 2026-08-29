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

from datetime import datetime, timezone

from loguru import logger
from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import LLMUsageMetricsData, TTSUsageMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection

from db.batch_writer import BackgroundBatchWriter
from db.engine import user_scoped_session
from db.models import TurnMetric, UsageEvent


class UsageRecorder:
    """One per session. Builds metadata-only rows and hands them to the
    background writer; never touches the DB on the calling path."""

    def __init__(self, session_id: str, user_id: str):
        self._session_id = session_id
        self._user_id = user_id
        self.current_turn: int | None = None
        self._log = logger.bind(session_id=session_id, component="obs.usage")
        self._writer = BackgroundBatchWriter(self._flush, self._log)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._writer.start()

    async def stop(self) -> None:
        await self._writer.stop()

    # -- hot-path recording (enqueue only) ---------------------------------

    def record_llm_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        now = datetime.now(timezone.utc)
        if prompt_tokens:
            self._writer.enqueue(self._event("llm", "tokens_in", prompt_tokens, now))
        if completion_tokens:
            self._writer.enqueue(self._event("llm", "tokens_out", completion_tokens, now))

    def record_tts_characters(self, characters: int) -> None:
        if characters:
            self._writer.enqueue(
                self._event("tts", "characters", characters, datetime.now(timezone.utc))
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

    def record_turn_metric(self, eot_to_first_audio_ms: int, stages_ms: dict) -> None:
        if self.current_turn is None:
            return  # no turn context; the latency log line still exists
        self._writer.enqueue(
            TurnMetric(
                user_id=self._user_id,
                session_id=self._session_id,
                turn_id=self.current_turn,
                ts=datetime.now(timezone.utc),
                eot_to_first_audio_ms=eot_to_first_audio_ms,
                stages_ms=stages_ms,
            )
        )

    # -- internals ---------------------------------------------------------

    def _event(self, stage: str, unit: str, quantity: float, ts) -> UsageEvent:
        return UsageEvent(
            user_id=self._user_id,
            session_id=self._session_id,
            turn_id=self.current_turn,
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
    (enable_usage_metrics=True) and enqueues via the recorder."""

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
            if isinstance(metric, LLMUsageMetricsData):
                usage = metric.value
                self._recorder.record_llm_usage(
                    usage.prompt_tokens or 0, usage.completion_tokens or 0
                )
            elif isinstance(metric, TTSUsageMetricsData):
                self._recorder.record_tts_characters(metric.value or 0)
