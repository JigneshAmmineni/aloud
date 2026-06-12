"""Schema and DB lifecycle contracts (SDD §3, NFR-6, NFR-7 groundwork)."""

import asyncio

from sqlalchemy import Text, select

from db.engine import DEFAULT_USER_ID, init_db, session_factory
from db.models import Base, Session, TranscriptEvent, User
from db.sessions_repo import create_session_row, end_session_row

# Schema snapshot: adding tables/columns (memory layer, artifacts, …) must
# update this snapshot consciously — that's the point.
EXPECTED_SCHEMA = {
    "users": {"id", "created_at"},
    "sessions": {"id", "user_id", "started_at", "ended_at", "status", "end_reason"},
    "transcript_events": {
        "id",
        "session_id",
        "ts",
        "role",
        "kind",
        "text",
        "turn_id",
        "latency_ms",
    },
}

# NFR-6: sensitive (🔒, to-be-encrypted) content columns, kept separate from
# metadata. Every Text-typed column must be accounted for here.
SENSITIVE_COLUMNS = {("transcript_events", "text")}


def test_schema_snapshot():
    actual = {
        name: {c.name for c in table.columns}
        for name, table in Base.metadata.tables.items()
    }
    assert actual == EXPECTED_SCHEMA


def test_sensitive_columns_are_exactly_the_declared_ones():
    text_columns = {
        (table.name, c.name)
        for table in Base.metadata.tables.values()
        for c in table.columns
        if isinstance(c.type, Text)
    }
    assert text_columns == SENSITIVE_COLUMNS


def test_init_db_is_idempotent_and_seeds_single_user(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/aloud_test.db"

    async def run():
        await init_db(url)
        await init_db(url)  # second boot: no error, no duplicate seed
        async with session_factory()() as db:
            users = (await db.execute(select(User))).scalars().all()
        assert [u.id for u in users] == [DEFAULT_USER_ID]

    asyncio.run(run())


def test_session_row_lifecycle(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/aloud_test.db"

    async def run():
        await init_db(url)
        await create_session_row("sess-1")
        async with session_factory()() as db:
            row = await db.get(Session, "sess-1")
            assert row.status == "active"
            assert row.user_id == DEFAULT_USER_ID
            assert row.ended_at is None

        await end_session_row("sess-1", "user")
        async with session_factory()() as db:
            row = await db.get(Session, "sess-1")
            assert row.status == "ended"
            assert row.end_reason == "user"
            assert row.ended_at is not None

    asyncio.run(run())


def test_end_session_row_tolerates_unknown_session(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/aloud_test.db"

    async def run():
        await init_db(url)
        await end_session_row("never-existed", "error")  # must not raise

    asyncio.run(run())
