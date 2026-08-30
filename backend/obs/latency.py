"""Per-turn latency instrumentation (SDD §4, C-1, NFR-1).

Wraps Pipecat's UserBotLatencyObserver with structured log events:

  turn.latency            — end-of-user-speech → first bot speech, per turn.
                            ERROR when it exceeds 3s (NFR-1).
  turn.latency_breakdown  — per-stage TTFBs + text aggregation for the turn.
                            WARN when any single stage exceeds 1s (C-1).
  session.first_bot_speech — client connect → first bot audio (greeting).

A latency regression therefore names its guilty stage in the logs without
any extra tooling.

Flux caveat: Pipecat's stock observer starts its clock at
``VADUserStoppedSpeakingFrame``, but Deepgram Flux does its own turn
detection and never emits VAD frames — it broadcasts
``UserStoppedSpeakingFrame`` when it declares end-of-turn. Without the
mapping below, no latency event would ever fire. Consequence: measured
latency starts at Flux's end-of-turn *declaration*, so it excludes Flux's
own EOT detection time (~300–500 ms); keep that in mind against the 3s
budget.
"""

import time

from loguru import logger
from pipecat.frames.frames import UserStartedSpeakingFrame, UserStoppedSpeakingFrame
from pipecat.observers.base_observer import FramePushed
from pipecat.observers.user_bot_latency_observer import (
    LatencyBreakdown,
    UserBotLatencyObserver,
)
from pipecat.processors.frame_processor import FrameDirection

STAGE_WARN_S = 1.0  # C-1: any single stage over this is a replacement candidate
E2E_ERROR_S = 3.0  # NFR-1: hard budget, end-of-speech → first audio


class FluxAwareLatencyObserver(UserBotLatencyObserver):
    """Maps Flux's turn frames onto the parent's VAD-based bookkeeping."""

    async def on_push_frame(self, data: FramePushed):
        if (
            data.direction == FrameDirection.DOWNSTREAM
            and data.frame.id not in self._processed_frames
        ):
            if isinstance(data.frame, UserStartedSpeakingFrame):
                # mirror the parent's VADUserStartedSpeaking reset
                self._user_stopped_time = None
                self._user_turn_start_time = None
                self._user_turn = None
                self._reset_accumulators()
            elif isinstance(data.frame, UserStoppedSpeakingFrame):
                if self._user_stopped_time is None:
                    self._user_stopped_time = time.time()
                    self._user_turn_start_time = self._user_stopped_time
        await super().on_push_frame(data)


def make_latency_observer(session_id: str, recorder=None) -> UserBotLatencyObserver:
    """`recorder` (obs.usage.UsageRecorder, optional) additionally persists a
    turn_metrics row per breakdown (FR-33) — enqueue-only, off the hot path."""
    observer = FluxAwareLatencyObserver()
    log = logger.bind(session_id=session_id, component="obs.latency")
    # Closure cell pairing the measured event with its breakdown. None means
    # "no fresh measurement" — a breakdown without one (e.g. the greeting
    # turn, which has no end-of-user-speech) must NOT persist a stale or
    # fake-zero latency into turn_metrics (FR-33/FR-37 trustworthiness).
    last_measured_ms: list[int | None] = [None]

    @observer.event_handler("on_latency_measured")
    async def on_latency_measured(_obs, latency: float):
        last_measured_ms[0] = round(latency * 1000)
        line = log.bind(event="turn.latency", duration_ms=round(latency * 1000))
        if latency > E2E_ERROR_S:
            line.error(
                f"end-of-speech -> first bot audio {latency:.3f}s "
                f"exceeds the {E2E_ERROR_S:.0f}s budget (NFR-1)"
            )
        else:
            line.info(f"end-of-speech -> first bot audio {latency:.3f}s")

    @observer.event_handler("on_latency_breakdown")
    async def on_latency_breakdown(_obs, breakdown: LatencyBreakdown):
        stages_ms: dict[str, int] = {}
        for m in breakdown.ttfb:
            stages_ms[f"ttfb.{m.processor}"] = round(m.duration_secs * 1000)
        if breakdown.text_aggregation:
            ta = breakdown.text_aggregation
            stages_ms[f"text_aggregation.{ta.processor}"] = round(ta.duration_secs * 1000)
        for fc in breakdown.function_calls:
            stages_ms[f"tool.{fc.function_name}"] = round(fc.duration_secs * 1000)

        line = log.bind(
            event="turn.latency_breakdown",
            stages_ms=stages_ms,
            user_turn_ms=(
                round(breakdown.user_turn_secs * 1000)
                if breakdown.user_turn_secs is not None
                else None
            ),
        )
        slow = {k: v for k, v in stages_ms.items() if v > STAGE_WARN_S * 1000}
        if slow:
            line.warning(f"stage(s) over {STAGE_WARN_S:.0f}s (C-1): {slow}")
        else:
            line.info("turn breakdown")
        if recorder is not None and last_measured_ms[0] is not None:
            recorder.record_turn_metric(last_measured_ms[0], stages_ms)
        last_measured_ms[0] = None  # consumed: the next breakdown needs its own

    @observer.event_handler("on_first_bot_speech_latency")
    async def on_first_bot_speech(_obs, latency: float):
        log.bind(
            event="session.first_bot_speech", duration_ms=round(latency * 1000)
        ).info(f"client connect -> first bot audio {latency:.3f}s")

    return observer
