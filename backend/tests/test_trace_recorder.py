"""FR-49 trace recorder: NFR-10's fake-queue/no-database split (mandated),
serialize-at-enqueue (the named deviation), and the broken-database drop.
"""

import asyncio
import json
from contextlib import asynccontextmanager

from sqlalchemy import select

import obs.trace as trace_mod
from db.engine import init_db, session_factory
from db.models import LLMTrace
from db.sessions_repo import create_session_row
from db.users_repo import provision_user
from obs.trace import TraceRecorder


def test_enqueue_touches_no_database():
    """NFR-10 split, capture half: record_trace with NO database configured
    — the enqueue path builds the row in memory and returns."""
    recorder = TraceRecorder("s-1", "uid-a")  # writer never started, no DB
    recorder.record_trace(
        turn_id=2,
        step=1,
        model="gemini-test",
        purpose="turn",
        finish_reason="stop",
        prompt_tokens=100,
        completion_tokens=30,
        ttfb_ms=450,
        duration_ms=900,
        input_messages=[{"role": "user", "content": "hi"}],
        output="hello",
    )
    row = recorder._writer._queue.get_nowait()
    assert isinstance(row, LLMTrace)
    assert (row.session_id, row.user_id, row.turn_id, row.step) == (
        "s-1",
        "uid-a",
        2,
        1,
    )
    assert row.purpose == "turn" and row.finish_reason == "stop"


def test_input_messages_serialized_at_enqueue_not_lazily():
    """FR-49: a later append_step mutating the live list must not rewrite
    history — the trace shows what was SENT."""
    recorder = TraceRecorder("s-1", "uid-a")
    messages = [{"role": "user", "content": "original"}]
    recorder.record_trace(
        turn_id=1,
        step=1,
        model="m",
        purpose="turn",
        finish_reason="stop",
        prompt_tokens=None,
        completion_tokens=None,
        ttfb_ms=None,
        duration_ms=None,
        input_messages=messages,
        output="",
    )
    messages.append({"role": "assistant", "content": "added AFTER the call"})
    row = recorder._writer._queue.get_nowait()
    assert json.loads(row.input_messages) == [{"role": "user", "content": "original"}]


def test_writer_persists_rows(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/trace.db")
        await provision_user("uid-a", None)
        await create_session_row("s-1", "uid-a")
        recorder = TraceRecorder("s-1", "uid-a")
        recorder.start()
        recorder.record_trace(
            turn_id=3,
            step=2,
            model="gemini-test",
            purpose="turn",
            finish_reason="interrupted",
            prompt_tokens=200,
            completion_tokens=15,
            ttfb_ms=400,
            duration_ms=1300,
            input_messages=[{"role": "user", "content": "q"}],
            output="partial answ",
        )
        await recorder.stop()

        async with session_factory()() as db:
            rows = (await db.execute(select(LLMTrace))).scalars().all()
        assert len(rows) == 1
        assert rows[0].finish_reason == "interrupted"
        assert rows[0].output == "partial answ"
        assert json.loads(rows[0].input_messages)[0]["content"] == "q"

    asyncio.run(run())


def test_broken_database_drops_batch_without_raising(monkeypatch):
    """NFR-10 split, writer half: a dead DB logs and drops — never touches
    the conversation."""

    @asynccontextmanager
    async def broken(user_id):
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(trace_mod, "user_scoped_session", broken)

    async def run():
        recorder = TraceRecorder("s-1", "uid-a")
        recorder.start()
        recorder.record_trace(
            turn_id=1,
            step=1,
            model="m",
            purpose="greeting",
            finish_reason="stop",
            prompt_tokens=None,
            completion_tokens=None,
            ttfb_ms=None,
            duration_ms=None,
            input_messages=[],
            output="hi",
        )
        await recorder.stop()  # must complete without raising

    asyncio.run(run())
