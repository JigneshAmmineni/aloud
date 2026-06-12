"""Transcript ops log tests (SDD §2.7): frames in → batched rows out."""

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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.models import Base, Session, TranscriptEvent, User
from db.transcript_log import TranscriptWriter


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


async def _make_db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(User(id="local-user"))
        db.add(Session(id="s-1", user_id="local-user", status="active"))
        await db.commit()
    return factory


def test_frames_become_rows_and_duplicates_are_ignored():
    async def run():
        factory = await _make_db()
        writer = TranscriptWriter("s-1", factory)
        observer = writer.observer()
        writer.start()

        user_frame = TranscriptionFrame("hello there", "u", "ts", finalized=True)
        await _push(observer, user_frame)
        await _push(observer, user_frame)  # same frame re-pushed at next hop
        await _push(observer, AggregatedTextFrame("Hi. What's on your mind?", "sentence"))
        await _push(observer, AggregatedTextFrame("   ", "sentence"))  # blank: dropped
        await _push(observer, TTSTextFrame("word", "word"))  # word fragment: excluded

        await writer.stop()  # final flush

        async with factory() as db:
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

    asyncio.run(run())


def test_session_row_lifecycle():
    async def run():
        factory = await _make_db()
        async with factory() as db:
            row = await db.get(Session, "s-1")
            assert row.status == "active"
            assert row.ended_at is None

    asyncio.run(run())


def test_large_volume_is_fully_persisted():
    """Batching (25-row batches / 1s flushes) must not drop rows."""

    async def run():
        factory = await _make_db()
        writer = TranscriptWriter("s-1", factory)
        observer = writer.observer()
        writer.start()

        for i in range(60):
            await _push(
                observer, TranscriptionFrame(f"utterance {i}", "u", "ts", finalized=True)
            )
        await writer.stop()

        async with factory() as db:
            rows = (await db.execute(select(TranscriptEvent))).scalars().all()
        assert len(rows) == 60
        assert {r.text for r in rows} == {f"utterance {i}" for i in range(60)}

    asyncio.run(run())


def test_db_failure_never_raises_into_the_pipeline():
    """SDD §2.7: the ops log must not disturb a live conversation. A broken
    DB drops the batch with an ERROR log — no exception escapes."""

    class BrokenFactory:
        def __call__(self):
            raise RuntimeError("db is down")

    async def run():
        writer = TranscriptWriter("s-1", BrokenFactory())
        observer = writer.observer()
        writer.start()
        await _push(
            observer, TranscriptionFrame("does not matter", "u", "ts", finalized=True)
        )
        await writer.stop()  # must complete cleanly despite the broken DB

    asyncio.run(run())
