"""FR-47 per-turn metrics buffer: ONE row writer with the observer →
recorder → loop handoff, dual-signal flush, measurement gating, and
explicit turn attribution for LLM usage.
"""

import asyncio

from loguru import logger
from sqlalchemy import select

import obs.usage as usage_mod
from db.engine import init_db, session_factory
from db.models import TurnMetric, UsageEvent
from db.sessions_repo import create_session_row
from db.users_repo import provision_user
from obs.usage import UsageRecorder


async def _setup_db(tmp_path, name):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/{name}.db")
    await provision_user("uid-a", None)
    await create_session_row("s-1", "uid-a")


async def _rows(model):
    async with session_factory()() as db:
        return (await db.execute(select(model))).scalars().all()


def test_three_step_turn_writes_one_row_with_step_indexed_keys(tmp_path):
    """Mandated (i): a 3-step turn with a repeated tool produces ONE row
    whose step-indexed keys cover every step and both tool invocations."""

    async def run():
        await _setup_db(tmp_path, "buf1")
        recorder = UsageRecorder("s-1", "uid-a")
        recorder.start()
        recorder.current_turn = 2
        recorder.record_step_stage(2, "step.1.ttfb.llm", 400)
        recorder.record_step_stage(2, "step.1.tool.read_artifact", 120)
        recorder.record_step_stage(2, "step.2.ttfb.llm", 300)
        recorder.record_step_stage(2, "step.2.tool.read_artifact", 90)
        recorder.record_step_stage(2, "step.3.ttfb.llm", 250)
        recorder.record_measurement(950)  # first audio arrives mid-turn
        recorder.turn_ended(2)
        await recorder.stop()

        metrics = await _rows(TurnMetric)
        assert len(metrics) == 1
        row = metrics[0]
        assert (row.turn_id, row.eot_to_first_audio_ms) == (2, 950)
        assert set(row.stages_ms) == {
            "step.1.ttfb.llm",
            "step.1.tool.read_artifact",
            "step.2.ttfb.llm",
            "step.2.tool.read_artifact",
            "step.3.ttfb.llm",
        }

    asyncio.run(run())


def test_late_measurement_within_window_still_writes_the_row(tmp_path):
    """The common race: the loop finishes ~100ms before first audio. The
    flush must wait for the measurement, or fast turns selectively lose
    their rows (skewing FR-37's p50 slow)."""

    async def run():
        await _setup_db(tmp_path, "buf2")
        recorder = UsageRecorder("s-1", "uid-a")
        recorder.start()
        recorder.current_turn = 3
        recorder.record_step_stage(3, "step.1.ttfb.llm", 300)
        recorder.turn_ended(3)  # loop done, no measurement yet
        await asyncio.sleep(0.1)  # audio lag
        recorder.record_measurement(880)
        await recorder.stop()

        metrics = await _rows(TurnMetric)
        assert len(metrics) == 1
        assert metrics[0].eot_to_first_audio_ms == 880
        assert metrics[0].stages_ms == {"step.1.ttfb.llm": 300}

    asyncio.run(run())


def test_no_measurement_no_row_and_no_credit_to_next_turn(tmp_path, monkeypatch):
    """Mandated (ii, recorder half) + the reset boundary: a turn with no
    measurement (greeting, pre-audio interruption) writes no row, and its
    buffered stages never leak into the next turn's row."""
    monkeypatch.setattr(usage_mod, "MEASUREMENT_WINDOW_S", 0.05)

    async def run():
        await _setup_db(tmp_path, "buf3")
        recorder = UsageRecorder("s-1", "uid-a")
        recorder.start()
        recorder.current_turn = 1  # the greeting: steps but no measurement
        recorder.record_step_stage(1, "step.1.ttfb.llm", 500)
        recorder.turn_ended(1)
        await asyncio.sleep(0.15)  # window expires → no-row path

        recorder.current_turn = 2
        recorder.record_step_stage(2, "step.1.ttfb.llm", 200)
        recorder.record_measurement(700)
        recorder.turn_ended(2)
        await recorder.stop()

        metrics = await _rows(TurnMetric)
        assert [m.turn_id for m in metrics] == [2]
        assert metrics[0].stages_ms == {"step.1.ttfb.llm": 200}  # turn 2 only

    asyncio.run(run())


def test_slow_stage_logs_warning(tmp_path):
    """Mandated (iii): any tool duration / step TTFB over 1s logs WARNING
    naming the guilty stage — C-1 transfers with the measurement."""
    lines = []
    sink_id = logger.add(lambda m: lines.append(m), level="WARNING")
    try:
        recorder = UsageRecorder("s-1", "uid-a")
        recorder.record_step_stage(1, "step.2.tool.read_artifact", 1600)
        recorder.record_step_stage(1, "step.1.ttfb.llm", 400)  # healthy: silent
    finally:
        logger.remove(sink_id)
    assert len(lines) == 1
    assert "step.2.tool.read_artifact" in lines[0]


def test_teardown_with_measurement_flushes_before_writer_stops(tmp_path):
    """FR-47: session teardown mid-turn is a turn end — a row whose
    measurement was already taken must not be lost to End-tap."""

    async def run():
        await _setup_db(tmp_path, "buf4")
        recorder = UsageRecorder("s-1", "uid-a")
        recorder.start()
        recorder.current_turn = 4
        recorder.record_step_stage(4, "step.1.ttfb.llm", 350)
        recorder.record_measurement(1200)
        # no turn_ended: the session is torn down mid-turn
        await recorder.stop()

        metrics = await _rows(TurnMetric)
        assert [m.turn_id for m in metrics] == [4]
        assert metrics[0].eot_to_first_audio_ms == 1200

    asyncio.run(run())


def test_llm_usage_lands_on_the_explicit_turn_never_sampled(tmp_path):
    """FR-47: the tracker advances on barge-in, so a cancelled step's
    late-reported usage must land on the turn that spent it."""

    async def run():
        await _setup_db(tmp_path, "buf5")
        recorder = UsageRecorder("s-1", "uid-a")
        recorder.start()
        recorder.current_turn = 8  # tracker already advanced
        recorder.record_llm_usage(100, 40, turn_id=7)  # the step's own turn
        await recorder.stop()

        events = await _rows(UsageEvent)
        assert {(e.unit, e.turn_id) for e in events} == {
            ("tokens_in", 7),
            ("tokens_out", 7),
        }

    asyncio.run(run())
