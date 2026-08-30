"""Usage + turn-metric capture (FR-32/FR-33): enqueue on the hot path,
batched user-scoped rows out, failures drop rather than raise (NFR-10)."""

import asyncio
from unittest.mock import MagicMock

from loguru import logger
from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import (
    LLMTokenUsage,
    LLMUsageMetricsData,
    TTSUsageMetricsData,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection
from sqlalchemy import select

import obs.usage as usage_mod
from db.batch_writer import BackgroundBatchWriter
from db.engine import init_db, session_factory
from db.models import TurnMetric, UsageEvent
from db.sessions_repo import create_session_row
from db.users_repo import provision_user
from obs.usage import UsageMetricsObserver, UsageRecorder


async def _setup_db(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/usage_test.db")
    await provision_user("uid-a", None)
    await create_session_row("s-1", "uid-a")


def _push(observer, frame):
    return observer.on_push_frame(
        FramePushed(
            source=MagicMock(),
            destination=MagicMock(),
            frame=frame,
            direction=FrameDirection.DOWNSTREAM,
            timestamp=0,
        )
    )


def test_recorder_writes_events_with_turn_identity(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        recorder = UsageRecorder("s-1", "uid-a")
        recorder.start()

        recorder.current_turn = 1
        recorder.record_llm_usage(100, 40)
        recorder.record_tts_characters(250)
        recorder.record_turn_metric(1800, {"ttfb.llm": 900})
        recorder.current_turn = 2
        recorder.record_llm_usage(120, 0)  # zero completion: only tokens_in
        recorder.record_stt_seconds(93.5)  # session-level, no turn
        await recorder.stop()

        async with session_factory()() as db:
            events = (await db.execute(select(UsageEvent))).scalars().all()
            metrics = (await db.execute(select(TurnMetric))).scalars().all()

        by_key = {(e.stage, e.unit, e.turn_id): e.quantity for e in events}
        assert by_key == {
            ("llm", "tokens_in", 1): 100.0,
            ("llm", "tokens_out", 1): 40.0,
            ("tts", "characters", 1): 250.0,
            ("llm", "tokens_in", 2): 120.0,
            ("stt", "seconds", None): 93.5,
        }
        assert all(e.user_id == "uid-a" and e.session_id == "s-1" for e in events)
        assert len(metrics) == 1
        assert metrics[0].turn_id == 1
        assert metrics[0].eot_to_first_audio_ms == 1800
        assert metrics[0].stages_ms == {"ttfb.llm": 900}

    asyncio.run(run())


def test_turn_metric_skipped_without_turn_context(tmp_path):
    """A breakdown arriving before any turn started (no turn number) is
    logged but not persisted — there is no turn to attribute it to."""

    async def run():
        await _setup_db(tmp_path)
        recorder = UsageRecorder("s-1", "uid-a")
        recorder.start()
        assert recorder.current_turn is None
        recorder.record_turn_metric(1500, {})
        await recorder.stop()

        async with session_factory()() as db:
            assert (await db.execute(select(TurnMetric))).scalars().all() == []

    asyncio.run(run())


def test_metrics_observer_dispatches_and_dedups(tmp_path):
    """UsageMetricsObserver: LLM/TTS usage metrics frames become events; the
    same frame re-observed at the next pipeline hop records once."""

    async def run():
        await _setup_db(tmp_path)
        recorder = UsageRecorder("s-1", "uid-a")
        recorder.current_turn = 3
        recorder.start()
        observer = UsageMetricsObserver(recorder)

        frame = MetricsFrame(
            data=[
                LLMUsageMetricsData(
                    processor="llm",
                    value=LLMTokenUsage(
                        prompt_tokens=10, completion_tokens=5, total_tokens=15
                    ),
                ),
                TTSUsageMetricsData(processor="tts", value=17),
            ]
        )
        await _push(observer, frame)
        await _push(observer, frame)  # same frame at the next hop: ignored
        await recorder.stop()

        async with session_factory()() as db:
            events = (await db.execute(select(UsageEvent))).scalars().all()
        assert {(e.stage, e.unit, e.quantity, e.turn_id) for e in events} == {
            ("llm", "tokens_in", 10.0, 3),
            ("llm", "tokens_out", 5.0, 3),
            ("tts", "characters", 17.0, 3),
        }

    asyncio.run(run())


def test_db_failure_drops_batch_and_never_raises(monkeypatch):
    """NFR-10: a broken DB must not disturb the conversation — the batch is
    dropped with an ERROR log, and stop() completes cleanly."""

    def broken_scope(user_id):
        raise RuntimeError("db is down")

    monkeypatch.setattr(usage_mod, "user_scoped_session", broken_scope)

    async def run():
        recorder = UsageRecorder("s-1", "uid-a")
        recorder.start()
        recorder.record_llm_usage(10, 10)
        await recorder.stop()  # must not raise

    asyncio.run(run())


def test_batch_writer_survives_a_failed_flush(tmp_path):
    """The shared writer: a flush that raises drops THAT batch; later
    batches still land."""

    flushed: list[list] = []
    calls = {"n": 0}

    async def flaky_flush(rows):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        flushed.append(list(rows))

    async def run():
        writer = BackgroundBatchWriter(flaky_flush, logger.bind(session_id="s-1"))
        writer.start()
        writer.enqueue("first")
        await asyncio.sleep(0.05)  # let the first (failing) flush happen
        writer.enqueue("second")
        await writer.stop()

    asyncio.run(run())
    assert calls["n"] == 2
    assert flushed == [["second"]]


def test_batch_writer_bounds_a_stalled_flush(monkeypatch):
    """A HANGING flush (Postgres mid-restart: asyncpg waits ~60s) must become
    a logged drop like a failing one — never block stop() (and with it the
    session teardown and uvicorn's graceful shutdown) until SIGKILL."""
    import db.batch_writer as bw

    monkeypatch.setattr(bw, "FLUSH_TIMEOUT_S", 0.1)

    async def hanging_flush(rows):
        await asyncio.sleep(60)

    async def run():
        writer = BackgroundBatchWriter(hanging_flush, logger.bind(session_id="s-1"))
        writer.start()
        writer.enqueue("x")
        await asyncio.wait_for(writer.stop(), timeout=5)  # must not hang

    asyncio.run(run())
