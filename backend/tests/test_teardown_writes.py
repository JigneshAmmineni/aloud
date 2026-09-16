"""FR-46 teardown write protection at the companion level.

The SIGTERM order test lives in test_pipeline_setup (drain awaits before
cancelling); these cover the End-tap twin — a real in-flight artifact
write survives teardown and BOTH its row and its usage event land — and
the expired-budget path (cancelled + WARNING, never destroyed-task noise).
"""

import asyncio

from loguru import logger
from sqlalchemy import select

import agent.companion as companion
from agent.companion import _await_writes
from db.artifacts_repo import create_artifact_row
from db.engine import init_db, session_factory
from db.models import Artifact, UsageEvent
from db.sessions_repo import create_session_row
from db.users_repo import provision_user


def test_end_tap_mid_write_lands_row_and_usage_event(tmp_path):
    """The write task is created OUTSIDE pipeline cancellation and teardown
    awaits it: the artifact row and its artifact_created event both land."""

    async def slow_create():
        await asyncio.sleep(0.1)  # the "in-flight" part
        return await create_artifact_row(
            "uid-a", "s-1", "summary", "T", "body", turn_id=3
        )

    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/teardown.db")
        await provision_user("uid-a", None)
        await create_session_row("s-1", "uid-a")

        write_task = asyncio.create_task(slow_create())
        # End-tap arrives immediately; teardown awaits under the grace
        await _await_writes(
            {write_task}, logger.bind(session_id="s-1"), companion.WRITE_GRACE_S
        )

        async with session_factory()() as db:
            artifacts = (await db.execute(select(Artifact))).scalars().all()
            events = (
                (
                    await db.execute(
                        select(UsageEvent).where(UsageEvent.stage == "artifact")
                    )
                )
                .scalars()
                .all()
            )
        assert len(artifacts) == 1
        assert [(e.unit, e.turn_id) for e in events] == [("count", 3)]

    asyncio.run(run())


def test_expired_grace_cancels_and_warns(monkeypatch):
    monkeypatch.setattr(companion, "WRITE_GRACE_S", 0.05)
    lines = []
    sink_id = logger.add(lambda m: lines.append(m), level="WARNING")

    async def hung_write():
        await asyncio.sleep(60)

    async def run():
        task = asyncio.create_task(hung_write())
        await _await_writes({task}, logger.bind(session_id="s-1"), companion.WRITE_GRACE_S)
        assert task.cancelled() or task.cancelling()
        await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        logger.remove(sink_id)
    assert any("write abandoned" in line for line in lines)


def test_drain_sweeps_writes_started_during_the_grace_window(monkeypatch):
    """Round-2 blocking 2: sessions stay live through the drain's grace
    wait, so a write STARTED inside that window is missing from the first
    snapshot — the post-cancel sweep must await it on the same single
    budget, never leave it to die mid-commit at process exit."""
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr(companion, "WRITE_GRACE_S", 1.0)
    finished = []

    async def late_write():
        await asyncio.sleep(0.1)
        finished.append(True)

    session_writes: set[asyncio.Task] = set()
    task = MagicMock()
    task.queue_frames = AsyncMock()

    async def cancel():
        # the pipeline dies mid-turn; a write it issued during the grace
        # window is registered only NOW — after the drain's first snapshot
        session_writes.add(asyncio.create_task(late_write()))

    task.cancel = cancel

    async def run():
        companion._live_tasks["drain-late"] = task
        companion._inflight_writes["drain-late"] = session_writes
        await companion.drain_live_sessions()

    try:
        asyncio.run(run())
        assert finished == [True]  # the late write completed, not abandoned
    finally:
        companion._live_tasks.pop("drain-late", None)
        companion._inflight_writes.pop("drain-late", None)
        companion._draining = False


def test_drain_abandonment_warning_names_the_session(monkeypatch):
    """Round-4 finding 3: FR-46 names session_id on the abandonment
    WARNING — the drain must bind it, not a bare component logger."""
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr(companion, "WRITE_GRACE_S", 0.05)
    records = []
    sink_id = logger.add(lambda m: records.append(m.record), level="WARNING")

    async def hung_write():
        await asyncio.sleep(60)

    task = MagicMock()
    task.queue_frames = AsyncMock()
    task.cancel = AsyncMock()

    async def run():
        write = asyncio.create_task(hung_write())
        companion._live_tasks["drain-warn"] = task
        companion._inflight_writes["drain-warn"] = {write}
        await companion.drain_live_sessions()
        await asyncio.gather(write, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        logger.remove(sink_id)
        companion._live_tasks.pop("drain-warn", None)
        companion._inflight_writes.pop("drain-warn", None)
        companion._draining = False
    warning = next(
        r for r in records if r["extra"].get("event") == "tool.write_abandoned"
    )
    assert warning["extra"]["session_id"] == "drain-warn"
