"""Row-level security proper (FR-31, NFR-8) — Postgres only.

Runs when RLS_TEST_DATABASE_URL points at a Postgres the suite may use
(CI provides a throwaway service container; locally, the compose db works:
RLS_TEST_DATABASE_URL=postgresql+asyncpg://aloud:aloud@localhost:5432/aloud).

The whole point (per the spec): these queries run through the app engine's
dedicated NOSUPERUSER/NOBYPASSRLS role, and the negative assertions hold
even when application-level WHERE scoping is deliberately omitted.
"""

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select, update

from app.auth import AuthedUser
from db.engine import init_db, session_factory, user_scoped_session
from db.models import Artifact, Session, TranscriptEvent, TurnMetric, UsageEvent
from db.sessions_repo import create_session_row
from db.users_repo import provision_user

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="RLS_TEST_DATABASE_URL not set (Postgres required)"
)


def test_rls_scopes_and_blocks_cross_user_access():
    # Unique ids per run: the target DB may persist between runs.
    uid_a, uid_b = f"rls-a-{uuid.uuid4()}", f"rls-b-{uuid.uuid4()}"
    sess_a, sess_b = f"s-{uuid.uuid4()}", f"s-{uuid.uuid4()}"

    async def run():
        await init_db(PG_URL)
        await provision_user(uid_a, "Ada")
        await provision_user(uid_b, "Bob")
        await create_session_row(sess_a, uid_a)
        await create_session_row(sess_b, uid_b)

        # 1. Scoped as A, with NO WHERE clause: only A's rows come back —
        #    the policy filters, not the application.
        async with user_scoped_session(uid_a) as db:
            rows = (await db.execute(select(Session))).scalars().all()
            assert {r.user_id for r in rows} == {uid_a}
            assert sess_b not in {r.id for r in rows}

        # 2. No user context at all: zero rows, not everyone's.
        async with session_factory()() as db:
            rows = (await db.execute(select(Session))).scalars().all()
            assert rows == []

        # 3. WITH CHECK: scoped as A, inserting a row claiming to be B's
        #    must be rejected by the database.
        with pytest.raises(Exception):
            async with user_scoped_session(uid_a) as db:
                db.add(Session(id=f"s-{uuid.uuid4()}", user_id=uid_b, status="active"))
                await db.commit()

        # 4. Direct fetch of the other user's row by primary key: invisible.
        async with user_scoped_session(uid_a) as db:
            assert await db.get(Session, sess_b) is None

    asyncio.run(run())


def test_rls_context_does_not_leak_across_pooled_connection_reuse():
    """FR-31's explicit mandate: SET LOCAL must reset at transaction end so
    a POOLED CONNECTION REUSED by a different user carries no stale context.
    A pool of exactly one connection forces every transaction here onto the
    same physical connection — reuse is guaranteed, not incidental."""
    from sqlalchemy.engine.url import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from db.engine import APP_ROLE

    uid_a, uid_b = f"rlsp-a-{uuid.uuid4()}", f"rlsp-b-{uuid.uuid4()}"
    sess_a, sess_b = f"s-{uuid.uuid4()}", f"s-{uuid.uuid4()}"

    async def run():
        await init_db(PG_URL)  # bootstrap roles/policies as usual
        await provision_user(uid_a, None)
        await provision_user(uid_b, None)
        await create_session_row(sess_a, uid_a)
        await create_session_row(sess_b, uid_b)

        url = make_url(PG_URL).set(username=APP_ROLE)
        one_conn = create_async_engine(url, pool_size=1, max_overflow=0)
        factory = async_sessionmaker(one_conn, expire_on_commit=False)
        try:
            from sqlalchemy import func as sa_func

            # Transaction 1: user A's context on THE connection.
            async with factory() as db:
                await db.execute(
                    select(sa_func.set_config("app.user_id", uid_a, True))
                )
                rows = (await db.execute(select(Session))).scalars().all()
                assert uid_a in {r.user_id for r in rows}
                await db.commit()

            # Transaction 2, same physical connection, NO context set:
            # a stale app.user_id from transaction 1 would return A's rows.
            async with factory() as db:
                rows = (await db.execute(select(Session))).scalars().all()
                assert rows == []

            # Transaction 3, same connection, user B's context: only B's.
            async with factory() as db:
                await db.execute(
                    select(sa_func.set_config("app.user_id", uid_b, True))
                )
                rows = (await db.execute(select(Session))).scalars().all()
                user_ids = {r.user_id for r in rows}
                assert uid_b in user_ids
                assert uid_a not in user_ids
        finally:
            await one_conn.dispose()

    asyncio.run(run())


# ---------- FR-38: the admin RLS escape, tests (a)-(f) ----------

_ADMIN = AuthedUser(
    user_id="admin-test",
    email="admin@example.com",
    email_verified=True,
    name=None,
    is_admin=True,
)

_NOW = datetime.now(timezone.utc)


async def _seed_two_users():
    """Two users, each with a session + usage event + turn metric +
    transcript event + artifact, inserted through their own scoped context."""
    uid_a, uid_b = f"adm-a-{uuid.uuid4()}", f"adm-b-{uuid.uuid4()}"
    sess_a, sess_b = f"s-{uuid.uuid4()}", f"s-{uuid.uuid4()}"
    await init_db(PG_URL)
    for uid, sess in ((uid_a, sess_a), (uid_b, sess_b)):
        await provision_user(uid, None)
        await create_session_row(sess, uid)
        async with user_scoped_session(uid) as db:
            db.add(
                UsageEvent(
                    user_id=uid, session_id=sess, turn_id=1, ts=_NOW,
                    stage="llm", unit="tokens_in", quantity=100.0,
                )
            )
            db.add(
                TurnMetric(
                    user_id=uid, session_id=sess, turn_id=1, ts=_NOW,
                    eot_to_first_audio_ms=1200, stages_ms=None,
                )
            )
            db.add(
                TranscriptEvent(
                    session_id=sess, user_id=uid, ts=_NOW, role="user",
                    kind="final_transcript", text="private words",
                )
            )
            db.add(
                Artifact(
                    session_id=sess, user_id=uid, kind="summary",
                    title="private title", content="private content",
                )
            )
            await db.commit()
    return uid_a, uid_b, sess_a, sess_b


def test_admin_context_reads_scoped_tables_never_content_tables():
    """(a)-(d): user contexts stay isolated on the NEW tables too; no
    context = zero rows; admin reads across users on sessions/usage/metrics
    but gets ZERO rows from transcript_events and artifacts (NFR-9)."""
    from db.admin_repo import admin_scoped_session

    async def run():
        uid_a, uid_b, *_ = await _seed_two_users()

        # (a) user scoping holds on the new tables, no WHERE needed
        async with user_scoped_session(uid_a) as db:
            for model in (UsageEvent, TurnMetric):
                rows = (await db.execute(select(model))).scalars().all()
                assert {r.user_id for r in rows} == {uid_a}

        # (b) neither setting: zero rows everywhere
        async with session_factory()() as db:
            for model in (Session, UsageEvent, TurnMetric, TranscriptEvent, Artifact):
                assert (await db.execute(select(model))).scalars().all() == []

        # (c) admin context reads across users on the three scoped tables
        async with admin_scoped_session(_ADMIN) as db:
            for model in (Session, UsageEvent, TurnMetric):
                users = {
                    r.user_id
                    for r in (await db.execute(select(model))).scalars().all()
                }
                assert {uid_a, uid_b} <= users

            # (d) ...and ZERO rows from the content tables, even here
            for model in (TranscriptEvent, Artifact):
                assert (await db.execute(select(model))).scalars().all() == []

    asyncio.run(run())


def test_admin_context_cannot_write():
    """(e): every write through the admin context fails — READ ONLY layer —
    and with READ ONLY deliberately absent, the untouched write policies
    still reject cross-user INSERT/UPDATE (WITH CHECK) and null out DELETE
    (zero rows matched): two independent layers, either alone suffices."""
    from sqlalchemy import func as sa_func

    from db.admin_repo import admin_scoped_session

    async def run():
        uid_a, uid_b, sess_a, sess_b = await _seed_two_users()

        # READ ONLY: INSERT, UPDATE, DELETE each fail at the database
        with pytest.raises(Exception):
            async with admin_scoped_session(_ADMIN) as db:
                db.add(
                    UsageEvent(
                        user_id=uid_a, session_id=sess_a, turn_id=None,
                        ts=_NOW, stage="stt", unit="seconds", quantity=1.0,
                    )
                )
                await db.commit()
        with pytest.raises(Exception):
            async with admin_scoped_session(_ADMIN) as db:
                await db.execute(
                    update(Session).where(Session.id == sess_b).values(status="ended")
                )
                await db.commit()
        with pytest.raises(Exception):
            async with admin_scoped_session(_ADMIN) as db:
                await db.execute(delete(Session).where(Session.id == sess_b))
                await db.commit()

        # Policy layer alone (no READ ONLY), app.is_admin set:
        async with session_factory()() as db:
            await db.execute(select(sa_func.set_config("app.is_admin", "true", True)))
            # INSERT: WITH CHECK (user-only) rejects
            with pytest.raises(Exception):
                db.add(
                    UsageEvent(
                        user_id=uid_a, session_id=sess_a, turn_id=None,
                        ts=_NOW, stage="stt", unit="seconds", quantity=1.0,
                    )
                )
                await db.commit()
        async with session_factory()() as db:
            await db.execute(select(sa_func.set_config("app.is_admin", "true", True)))
            # UPDATE/DELETE: user-only USING matches zero rows
            upd = await db.execute(
                update(Session).where(Session.id == sess_b).values(status="ended")
            )
            assert upd.rowcount == 0
            del_ = await db.execute(delete(Session).where(Session.id == sess_b))
            assert del_.rowcount == 0
            await db.rollback()

        # WITH CHECK on UPDATE proper: user A reassigning their own row to B
        with pytest.raises(Exception):
            async with user_scoped_session(uid_a) as db:
                await db.execute(
                    update(UsageEvent)
                    .where(UsageEvent.user_id == uid_a)
                    .values(user_id=uid_b)
                )
                await db.commit()

        # B's session survived every attempt
        async with user_scoped_session(uid_b) as db:
            assert await db.get(Session, sess_b) is not None

    asyncio.run(run())


def test_create_artifact_handler_succeeds_under_real_rls():
    """Regression: the handler once refreshed its row AFTER commit — the
    transaction-local RLS context had evaporated, the refresh SELECT matched
    zero rows, and every artifact save failed on Postgres while sqlite tests
    stayed green. The handler must run cleanly under real policies."""
    from unittest.mock import AsyncMock, MagicMock

    from agent.tools import make_create_artifact_handler
    from db.models import Artifact as ArtifactModel

    async def run():
        uid = f"art-{uuid.uuid4()}"
        sess = f"s-{uuid.uuid4()}"
        await init_db(PG_URL)
        await provision_user(uid, None)
        await create_session_row(sess, uid)

        params = MagicMock()
        params.arguments = {"title": "T", "kind": "summary", "content": "body"}
        params.llm.push_frame = AsyncMock()
        params.result_callback = AsyncMock()
        await make_create_artifact_handler(sess, uid)(params)

        assert params.result_callback.call_args.args[0]["status"] == "created"
        async with user_scoped_session(uid) as db:
            rows = (
                (
                    await db.execute(
                        select(ArtifactModel).where(ArtifactModel.session_id == sess)
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
        # the in-transaction artifact.count event landed too (FR-32/FR-38)
        async with user_scoped_session(uid) as db:
            events = (
                (
                    await db.execute(
                        select(UsageEvent).where(
                            UsageEvent.session_id == sess,
                            UsageEvent.stage == "artifact",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(events) == 1

    asyncio.run(run())


def test_admin_setting_does_not_leak_across_pooled_connection_reuse():
    """(f): the FR-31 pooled-reuse test repeated for app.is_admin — a leak
    here would grant cross-user READS to the next transaction on the
    connection."""
    from sqlalchemy import func as sa_func
    from sqlalchemy.engine.url import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from db.engine import APP_ROLE

    async def run():
        uid_a, uid_b, *_ = await _seed_two_users()

        url = make_url(PG_URL).set(username=APP_ROLE)
        one_conn = create_async_engine(url, pool_size=1, max_overflow=0)
        factory = async_sessionmaker(one_conn, expire_on_commit=False)
        try:
            # Transaction 1: admin context on THE connection reads across users.
            async with factory() as db:
                await db.execute(
                    select(sa_func.set_config("app.is_admin", "true", True))
                )
                users = {
                    r.user_id
                    for r in (await db.execute(select(Session))).scalars().all()
                }
                assert {uid_a, uid_b} <= users
                await db.commit()

            # Transaction 2, same physical connection, NO context: a stale
            # app.is_admin would return everyone's rows here.
            async with factory() as db:
                assert (await db.execute(select(Session))).scalars().all() == []
        finally:
            await one_conn.dispose()

    asyncio.run(run())
