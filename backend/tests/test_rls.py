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

import pytest
from sqlalchemy import select

from db.engine import init_db, session_factory, user_scoped_session
from db.models import Session
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
