"""Async engine + boot-time init, with row-level security (FR-31, NFR-8).

Two engines on Postgres:
  bootstrap engine — the compose superuser (DATABASE_URL). Runs create_all,
      idempotent column migrations, and the RLS bootstrap (role, grants,
      policies). Never used for request-path queries.
  app engine — the dedicated `aloud_app` role: NOSUPERUSER, NOBYPASSRLS,
      not the table owner. Postgres exempts superusers and owners from RLS,
      so connecting as the compose user would silently bypass every policy —
      this split is what makes RLS real. `session_factory()` hands out
      sessions on THIS engine.

On SQLite (tests) there is one engine and RLS is a no-op; isolation tests at
the repo-scoping level still run, and the RLS-proper tests run against
Postgres (see tests/test_rls.py).

Per-request scoping: `user_scoped_session(user_id)` opens a session whose
first statement sets `app.user_id` via set_config(..., true) — transaction-
local by definition, so it can never leak across a pooled connection
(REQUIREMENTS FR-31). All repos and background writers go through it.
"""

from contextlib import asynccontextmanager

from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from db.models import Base

APP_ROLE = "aloud_app"

# Tables holding user-owned rows (FR-31). users is deliberately absent:
# provisioning must insert before any per-user context exists, rows are
# keyed by uid, and the admin surface reads accounts from Firebase.
_RLS_TABLES = (
    "sessions",
    "transcript_events",
    "artifacts",
    "usage_events",
    "turn_metrics",
)

# FR-38: ONLY these tables' FOR SELECT policies carry the admin escape.
# The content-bearing tables (transcript_events, artifacts) never do —
# admin context reads zero rows from them, backing NFR-9 at the DB layer.
_ADMIN_READ_TABLES = ("sessions", "usage_events", "turn_metrics")

_bootstrap_engine: AsyncEngine | None = None
_app_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def session_factory() -> async_sessionmaker:
    assert _session_factory is not None, "init_db() must run first"
    return _session_factory


@asynccontextmanager
async def bootstrap_session():
    """RLS-exempt session on the bootstrap engine — boot-time maintenance
    ONLY (the FR-32 orphan sweep). Never on a request path; admin reads go
    through db/admin_repo's admin_scoped_session (FR-38). Loud if init_db
    hasn't run OR the engine has been retired — a sweep that silently saw
    zero rows would look like success."""
    assert _bootstrap_engine is not None, (
        "bootstrap engine unavailable — init_db() must run first, and after "
        "retire_bootstrap_engine() the escape hatch is closed for good"
    )
    factory = async_sessionmaker(_bootstrap_engine, expire_on_commit=False)
    async with factory() as db:
        yield db


async def retire_bootstrap_engine() -> None:
    """Close the RLS-exempt escape hatch once boot-time maintenance is done.
    The bootstrap engine has no job after startup, and leaving it importable
    AND working would make it the obvious (silently policy-bypassing) reach
    for the next cross-user read — every other dangerous path in this
    codebase fails structurally, so this one must too."""
    global _bootstrap_engine
    if _bootstrap_engine is not None and _bootstrap_engine is not _app_engine:
        await _bootstrap_engine.dispose()  # postgres: separate superuser engine
    # sqlite shares one engine; only the alias is dropped, the app engine
    # keeps its own reference in the session factory
    _bootstrap_engine = None


@asynccontextmanager
async def user_scoped_session(user_id: str):
    """A DB session scoped to one verified user_id. On Postgres the setting
    is transaction-local (third set_config arg), evaporating at commit —
    callers commit once per context, matching one transaction."""
    async with session_factory()() as db:
        if db.bind.dialect.name == "postgresql":
            await db.execute(select(func.set_config("app.user_id", user_id, True)))
        yield db


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def _bootstrap_rls(engine: AsyncEngine, app_password: str) -> None:
    """Idempotent role + policy setup, run as the bootstrap superuser."""
    pw = _quote_literal(app_password)
    statements = [
        # Role: LOGIN, cannot bypass RLS, is not the table owner.
        f"""
        DO $$ BEGIN
            CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {pw};
        EXCEPTION WHEN duplicate_object THEN
            ALTER ROLE {APP_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {pw};
        END $$;
        """,
        f"GRANT USAGE ON SCHEMA public TO {APP_ROLE};",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON users, sessions,"
        f" transcript_events, artifacts, usage_events, turn_metrics"
        f" TO {APP_ROLE};",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE};",
        # Pre-auth databases: add columns create_all won't retrofit.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_name VARCHAR(80);",
        "ALTER TABLE transcript_events ADD COLUMN IF NOT EXISTS user_id VARCHAR(64);",
        # create_all skips existing tables, so the pre-auth DB needs the
        # index the model declares — RLS filters on this column every query.
        "CREATE INDEX IF NOT EXISTS ix_transcript_events_user_id"
        " ON transcript_events (user_id);",
        # FR-37's overview time-windows filter on ts; create_all won't
        # retrofit indexes onto tables that already exist.
        "CREATE INDEX IF NOT EXISTS ix_usage_events_ts ON usage_events (ts);",
        "CREATE INDEX IF NOT EXISTS ix_turn_metrics_ts ON turn_metrics (ts);",
        "CREATE INDEX IF NOT EXISTS ix_sessions_user_id ON sessions (user_id);",
        """
        UPDATE transcript_events te SET user_id = s.user_id
        FROM sessions s WHERE te.session_id = s.id AND te.user_id IS NULL;
        """,
    ]
    # FR-38: command-scoped policies. The split exists because Postgres
    # consults only USING for DELETE (never WITH CHECK), so an admin clause
    # on a generic all-commands policy would let admin-context deletes pass
    # RLS. With a dedicated FOR SELECT policy, every write command keeps
    # user-only predicates — cross-user writes fail at the policy layer
    # independently of the admin transaction's READ ONLY mode.
    user_pred = "user_id = current_setting('app.user_id', true)"
    admin_pred = "current_setting('app.is_admin', true) = 'true'"
    for table in _RLS_TABLES:
        read_pred = (
            f"({user_pred} OR {admin_pred})"
            if table in _ADMIN_READ_TABLES
            else user_pred
        )
        statements += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
            # FORCE is defense-in-depth: even the owner gets policies applied.
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;",
            f"DROP POLICY IF EXISTS user_isolation ON {table};",
            f"DROP POLICY IF EXISTS p_select ON {table};",
            f"DROP POLICY IF EXISTS p_insert ON {table};",
            f"DROP POLICY IF EXISTS p_update ON {table};",
            f"DROP POLICY IF EXISTS p_delete ON {table};",
            # current_setting(..., true) -> NULL when unset: no context, no rows.
            f"CREATE POLICY p_select ON {table} FOR SELECT USING ({read_pred});",
            f"CREATE POLICY p_insert ON {table} FOR INSERT WITH CHECK ({user_pred});",
            f"CREATE POLICY p_update ON {table} FOR UPDATE"
            f" USING ({user_pred}) WITH CHECK ({user_pred});",
            f"CREATE POLICY p_delete ON {table} FOR DELETE USING ({user_pred});",
        ]
    async with engine.begin() as conn:
        for stmt in statements:
            await conn.execute(text(stmt))


async def init_db(database_url: str) -> None:
    global _bootstrap_engine, _app_engine, _session_factory
    url = make_url(database_url)
    _bootstrap_engine = create_async_engine(database_url, echo=False)

    async with _bootstrap_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if _bootstrap_engine.dialect.name == "postgresql":
        # The app role reuses the DB password — accepted tradeoff: both
        # credentials live in the same .env on the same host, so the
        # separation that matters here is privileges (NOBYPASSRLS), not a
        # second secret. But never a BLANK secret: fail fast rather than
        # silently creating a passwordless login role.
        if not url.password:
            raise RuntimeError(
                "DATABASE_URL has no password — refusing to create the "
                f"{APP_ROLE} role without one."
            )
        await _bootstrap_rls(_bootstrap_engine, url.password)
        _app_engine = create_async_engine(
            url.set(username=APP_ROLE), echo=False
        )
    else:  # sqlite in tests — single engine, RLS not available
        _app_engine = _bootstrap_engine

    _session_factory = async_sessionmaker(_app_engine, expire_on_commit=False)
    logger.bind(component="db", event="db.initialized").info(
        f"Database ready ({_bootstrap_engine.dialect.name}; "
        f"app role: {APP_ROLE if _app_engine is not _bootstrap_engine else 'shared'})"
    )
