"""Observability tests (SDD §4): JSON log lines, GCP severity, latency events."""

import asyncio
import json
import time
from unittest.mock import MagicMock

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    MetricsFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

from obs.latency import make_latency_observer
from obs.logging import _SEVERITY, _sink


def _capture_line(capsys, level: str, **extra) -> dict:
    logger.remove()
    logger.add(_sink, level="DEBUG")
    logger.bind(**extra).log(level, "test message")
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


def test_line_is_json_with_severity_and_extras(capsys):
    line = _capture_line(
        capsys, "INFO", session_id="s-1", event="turn.latency", duration_ms=812
    )
    assert line["severity"] == "INFO"
    assert line["message"] == "test message"
    assert line["session_id"] == "s-1"
    assert line["event"] == "turn.latency"
    assert line["duration_ms"] == 812


def test_warning_maps_to_gcp_severity(capsys):
    line = _capture_line(capsys, "WARNING")
    assert line["severity"] == "WARNING"


def test_component_override(capsys):
    line = _capture_line(capsys, "INFO", component="obs.latency")
    assert line["component"] == "obs.latency"


def test_all_loguru_levels_have_mappings():
    for name in ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"):
        assert _SEVERITY[name] in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def test_exception_ships_as_formatted_stack_trace(capsys):
    """FR-39: GCP Error Reporting groups an entry only if it carries a real
    stack trace (spike-verified) — the sink must emit the formatted traceback,
    never the loguru namedtuple repr."""
    logger.remove()
    logger.add(_sink, level="DEBUG")
    try:
        raise RuntimeError("obs boom")
    except RuntimeError:
        logger.exception("caught for the log")
    out = capsys.readouterr().out.strip().splitlines()
    line = json.loads(out[-1])
    assert line["severity"] == "ERROR"
    assert line["message"] == "caught for the log"
    assert "Traceback (most recent call last):" in line["stack_trace"]
    assert "RuntimeError: obs boom" in line["stack_trace"]


def test_flux_turn_frames_drive_latency_events():
    """Flux emits UserStoppedSpeaking (not VAD frames); the observer must
    still measure end-of-speech -> bot speech and emit both log events."""
    captured: list = []
    logger.remove()
    logger.add(lambda m: captured.append(m.record), level="DEBUG")

    observer = make_latency_observer("test-session")
    src, dst = MagicMock(), MagicMock()

    def push(frame):
        return observer.on_push_frame(
            FramePushed(
                source=src,
                destination=dst,
                frame=frame,
                direction=FrameDirection.DOWNSTREAM,
                timestamp=0,
            )
        )

    async def run():
        await push(UserStoppedSpeakingFrame())
        await push(BotStartedSpeakingFrame())

    asyncio.run(run())

    events = [r["extra"].get("event") for r in captured]
    assert "turn.latency" in events
    assert "turn.latency_breakdown" in events

    latency_record = next(
        r for r in captured if r["extra"].get("event") == "turn.latency"
    )
    assert latency_record["extra"]["session_id"] == "test-session"
    assert latency_record["extra"]["duration_ms"] >= 0


def _capture_observer_run(frames_then_state=None):
    """Run an observer over a frame sequence; return captured log records."""
    captured: list = []
    logger.remove()
    logger.add(lambda m: captured.append(m.record), level="DEBUG")
    observer = make_latency_observer("test-session")
    return observer, captured


def _pushed(frame):
    return FramePushed(
        source=MagicMock(),
        destination=MagicMock(),
        frame=frame,
        direction=FrameDirection.DOWNSTREAM,
        timestamp=0,
    )


def test_latency_over_budget_logs_error():
    """NFR-1: end-of-speech -> first audio over 3s is an ERROR."""
    observer, captured = _capture_observer_run()

    async def run():
        await observer.on_push_frame(_pushed(UserStoppedSpeakingFrame()))
        observer._user_stopped_time = time.time() - 4.0  # simulate a 4s wait
        await observer.on_push_frame(_pushed(BotStartedSpeakingFrame()))

    asyncio.run(run())

    record = next(r for r in captured if r["extra"].get("event") == "turn.latency")
    assert record["level"].name == "ERROR"
    assert record["extra"]["duration_ms"] >= 3000


def test_slow_stage_logs_warning_with_guilty_stage():
    """C-1: any single stage over 1s is a WARNING naming the stage."""
    observer, captured = _capture_observer_run()

    async def run():
        await observer.on_push_frame(_pushed(UserStoppedSpeakingFrame()))
        await observer.on_push_frame(
            _pushed(
                MetricsFrame(
                    data=[TTFBMetricsData(processor="GoogleLLMService#0", value=1.5)]
                )
            )
        )
        await observer.on_push_frame(_pushed(BotStartedSpeakingFrame()))

    asyncio.run(run())

    record = next(
        r for r in captured if r["extra"].get("event") == "turn.latency_breakdown"
    )
    assert record["level"].name == "WARNING"
    assert record["extra"]["stages_ms"]["ttfb.GoogleLLMService#0"] == 1500


def test_fast_turn_logs_info_not_warning():
    observer, captured = _capture_observer_run()

    async def run():
        await observer.on_push_frame(_pushed(UserStoppedSpeakingFrame()))
        await observer.on_push_frame(
            _pushed(
                MetricsFrame(
                    data=[TTFBMetricsData(processor="GoogleLLMService#0", value=0.6)]
                )
            )
        )
        await observer.on_push_frame(_pushed(BotStartedSpeakingFrame()))

    asyncio.run(run())

    latency = next(r for r in captured if r["extra"].get("event") == "turn.latency")
    breakdown = next(
        r for r in captured if r["extra"].get("event") == "turn.latency_breakdown"
    )
    assert latency["level"].name == "INFO"
    assert breakdown["level"].name == "INFO"
