"""Observability tests (SDD §4): JSON log lines, GCP severity, latency events."""

import asyncio
import json
from unittest.mock import MagicMock

from loguru import logger
from pipecat.frames.frames import BotStartedSpeakingFrame, UserStoppedSpeakingFrame
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
