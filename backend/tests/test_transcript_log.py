"""Transcript ops log tests (FR-20): frames in → batched, user-scoped rows out."""

import asyncio
from unittest.mock import MagicMock

from pipecat.frames.frames import (
    AggregatedTextFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection
from sqlalchemy import select

import db.transcript_log as transcript_log
from db.engine import init_db, session_factory
from db.models import Session, TranscriptEvent
from db.sessions_repo import create_session_row
from db.transcript_log import TranscriptWriter
from db.users_repo import provision_user


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


async def _setup_db(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/transcript_test.db")
    await provision_user("uid-a", None)
    await create_session_row("s-1", "uid-a")


def test_frames_become_rows_and_duplicates_are_ignored(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        writer = TranscriptWriter("s-1", "uid-a")
        observer = writer.observer()
        writer.start()

        user_frame = TranscriptionFrame("hello there", "u", "ts", finalized=True)
        await _push(observer, user_frame)
        await _push(observer, user_frame)  # same frame re-pushed at next hop
        await _push(observer, AggregatedTextFrame("Hi. What's on your mind?", "sentence"))
        await _push(observer, AggregatedTextFrame("   ", "sentence"))  # blank: dropped
        await _push(observer, TTSTextFrame("word", "word"))  # word fragment: excluded

        await writer.stop()  # final flush

        async with session_factory()() as db:
            rows = (
                (await db.execute(select(TranscriptEvent).order_by(TranscriptEvent.id)))
                .scalars()
                .all()
            )
        assert [(r.role, r.kind) for r in rows] == [
            ("user", "final_transcript"),
            ("agent", "agent_text"),
        ]
        assert rows[0].text == "hello there"
        assert rows[0].session_id == "s-1"
        assert rows[0].user_id == "uid-a"  # FR-31: writer stamps its user

    asyncio.run(run())


def test_session_row_lifecycle(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        async with session_factory()() as db:
            row = await db.get(Session, "s-1")
            assert row.status == "active"
            assert row.ended_at is None

    asyncio.run(run())


def test_large_volume_is_fully_persisted(tmp_path):
    """Batching (25-row batches / 1s flushes) must not drop rows."""

    async def run():
        await _setup_db(tmp_path)
        writer = TranscriptWriter("s-1", "uid-a")
        observer = writer.observer()
        writer.start()

        for i in range(60):
            await _push(
                observer, TranscriptionFrame(f"utterance {i}", "u", "ts", finalized=True)
            )
        await writer.stop()

        async with session_factory()() as db:
            rows = (await db.execute(select(TranscriptEvent))).scalars().all()
        assert len(rows) == 60
        assert {r.text for r in rows} == {f"utterance {i}" for i in range(60)}

    asyncio.run(run())


def test_db_failure_never_raises_into_the_pipeline(monkeypatch):
    """FR-20 discipline: the ops log must not disturb a live conversation. A
    broken DB drops the batch with an ERROR log — no exception escapes."""

    def broken_scope(user_id):
        raise RuntimeError("db is down")

    monkeypatch.setattr(transcript_log, "user_scoped_session", broken_scope)

    async def run():
        writer = TranscriptWriter("s-1", "uid-a")
        observer = writer.observer()
        writer.start()
        await _push(
            observer, TranscriptionFrame("does not matter", "u", "ts", finalized=True)
        )
        await writer.stop()  # must complete cleanly despite the broken DB

    asyncio.run(run())
