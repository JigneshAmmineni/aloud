"""Schema and DB lifecycle contracts (NFR-6, FR-24, FR-31 groundwork)."""

import asyncio

from sqlalchemy import Text, select

from db.engine import init_db, session_factory
from db.models import Base, Session, User
from db.sessions_repo import create_session_row, end_session_row
from db.users_repo import provision_user

# Schema snapshot: adding tables/columns (memory layer, artifacts, …) must
# update this snapshot consciously — that's the point.
EXPECTED_SCHEMA = {
    "users": {"id", "created_at", "preferred_name"},
    "sessions": {"id", "user_id", "started_at", "ended_at", "status", "end_reason"},
    "transcript_events": {
        "id",
        "session_id",
        "user_id",
        "ts",
        "role",
        "kind",
        "text",
        "turn_id",
        "latency_ms",
    },
    "artifacts": {"id", "session_id", "user_id", "created_at", "kind", "title", "content"},
    # FR-32/FR-33: metadata-only usage + latency tables (no sensitive columns)
    "usage_events": {
        "id",
        "user_id",
        "session_id",
        "turn_id",
        "ts",
        "stage",
        "unit",
        "quantity",
        "detail",
    },
    "turn_metrics": {
        "id",
        "user_id",
        "session_id",
        "turn_id",
        "ts",
        "eot_to_first_audio_ms",
        "stages_ms",
    },
}

# NFR-6: sensitive (🔒, to-be-encrypted) content columns, kept separate from
# metadata. Every Text-typed column must be accounted for here.
SENSITIVE_COLUMNS = {
    ("transcript_events", "text"),
    ("artifacts", "title"),
    ("artifacts", "content"),
}


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


def test_init_db_is_idempotent_and_seeds_nothing(tmp_path):
    """FR-24: users appear only through provisioning — no stub rows."""
    url = f"sqlite+aiosqlite:///{tmp_path}/aloud_test.db"

    async def run():
        await init_db(url)
        await init_db(url)  # second boot: no error
        async with session_factory()() as db:
            users = (await db.execute(select(User))).scalars().all()
        assert users == []

    asyncio.run(run())


def test_session_row_lifecycle(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/aloud_test.db"

    async def run():
        await init_db(url)
        await provision_user("uid-a", "Ada")
        await create_session_row("sess-1", "uid-a")
        async with session_factory()() as db:
            row = await db.get(Session, "sess-1")
            assert row.status == "active"
            assert row.user_id == "uid-a"
            assert row.ended_at is None

        await end_session_row("sess-1", "uid-a", "user")
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
        await end_session_row("never-existed", "uid-a", "error")  # must not raise

    asyncio.run(run())


def test_session_is_active_lifecycle(tmp_path):
    """The client's while-active liveness poll: true only for the owner's
    still-active session — ended, unknown, and other-user sessions are all
    authoritatively dead."""
    from db.sessions_repo import session_is_active

    url = f"sqlite+aiosqlite:///{tmp_path}/aloud_test.db"

    async def run():
        await init_db(url)
        await provision_user("uid-a", None)
        await provision_user("uid-b", None)
        await create_session_row("sess-1", "uid-a")

        assert await session_is_active("sess-1", "uid-a") is True
        assert await session_is_active("sess-1", "uid-b") is False  # not theirs
        assert await session_is_active("nope", "uid-a") is False
        await end_session_row("sess-1", "uid-a", "user")
        assert await session_is_active("sess-1", "uid-a") is False

    asyncio.run(run())


def test_bootstrap_engine_retires_loudly(tmp_path):
    """The RLS-exempt escape hatch must be structurally closed after boot:
    usable before retirement, a loud RuntimeError after — never a silent
    zero-row success (the artifact-save bug's shape, inverted)."""
    import pytest

    from db.engine import bootstrap_session, retire_bootstrap_engine

    url = f"sqlite+aiosqlite:///{tmp_path}/aloud_test.db"

    async def run():
        await init_db(url)
        async with bootstrap_session():  # open before retirement
            pass
        await retire_bootstrap_engine()
        with pytest.raises(RuntimeError):
            async with bootstrap_session():
                pass
        await init_db(url)  # a fresh boot re-opens it (new process semantics)
        async with bootstrap_session():
            pass

    asyncio.run(run())


def test_boot_sweep_closes_orphans_and_emits_stt_usage(tmp_path):
    """FR-32's boot sweep: still-active sessions are closed as 'interrupted'
    (not 'error' — deploys cause this too), ended_at inferred from the max
    event timestamp (fallback started_at), and the STT usage event the dead
    process never wrote is emitted from that inferred duration."""
    from datetime import datetime, timedelta, timezone

    from db.models import TranscriptEvent, UsageEvent
    from db.sessions_repo import sweep_orphaned_sessions

    url = f"sqlite+aiosqlite:///{tmp_path}/aloud_test.db"
    started = datetime.now(timezone.utc) - timedelta(minutes=10)
    last_event = started + timedelta(minutes=4)

    async def run():
        await init_db(url)
        await provision_user("uid-a", None)
        async with session_factory()() as db:
            # crashed mid-conversation: active, with a last recorded event
            db.add(
                Session(
                    id="s-crashed", user_id="uid-a", status="active",
                    started_at=started,
                )
            )
            db.add(
                TranscriptEvent(
                    session_id="s-crashed", user_id="uid-a", ts=last_event,
                    role="user", kind="final_transcript", text="x",
                )
            )
            # crashed instantly: active, no events at all
            db.add(
                Session(
                    id="s-empty", user_id="uid-a", status="active",
                    started_at=started,
                )
            )
            # died AFTER its STT event committed but BEFORE the row closed:
            # must be swept WITHOUT a second stt event (append-only table —
            # a duplicate would permanently double the session's minutes)
            db.add(
                Session(
                    id="s-half-dead", user_id="uid-a", status="active",
                    started_at=started,
                )
            )
            db.add(
                UsageEvent(
                    user_id="uid-a", session_id="s-half-dead", turn_id=None,
                    ts=last_event, stage="stt", unit="seconds", quantity=240.0,
                )
            )
            # cleanly ended: must be untouched
            db.add(
                Session(
                    id="s-done", user_id="uid-a", status="ended",
                    end_reason="user", started_at=started,
                    ended_at=started + timedelta(minutes=2),
                )
            )
            await db.commit()

        assert await sweep_orphaned_sessions() == 3

        async with session_factory()() as db:
            crashed = await db.get(Session, "s-crashed")
            assert crashed.status == "ended"
            assert crashed.end_reason == "interrupted"
            assert crashed.ended_at.replace(tzinfo=timezone.utc) == last_event

            empty = await db.get(Session, "s-empty")
            assert empty.end_reason == "interrupted"
            assert empty.ended_at.replace(tzinfo=timezone.utc) == started

            done = await db.get(Session, "s-done")
            assert done.end_reason == "user"

            stt = (
                (
                    await db.execute(
                        select(UsageEvent).where(UsageEvent.stage == "stt")
                    )
                )
                .scalars()
                .all()
            )
            by_session: dict[str, list] = {}
            for e in stt:
                by_session.setdefault(e.session_id, []).append(e)
            assert by_session["s-crashed"][0].quantity == 240.0  # 4 minutes
            assert by_session["s-crashed"][0].turn_id is None
            assert by_session["s-empty"][0].quantity == 0.0
            # the guard: still closed as interrupted, but NO duplicate event
            assert len(by_session["s-half-dead"]) == 1
            half = await db.get(Session, "s-half-dead")
            assert half.end_reason == "interrupted"

        # second boot: nothing left to sweep
        assert await sweep_orphaned_sessions() == 0

    asyncio.run(run())
