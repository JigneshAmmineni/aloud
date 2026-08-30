"""Session row lifecycle. Auth-agnostic: user_id arrives as a plain argument
(never defaulted — a forgotten argument must be a loud TypeError), and every
query runs in a user-scoped session (FR-31)."""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func, select

from db.engine import user_scoped_session
from db.models import Session, TranscriptEvent, TurnMetric, UsageEvent


async def create_session_row(session_id: str, user_id: str) -> None:
    async with user_scoped_session(user_id) as db:
        # The id is the /start-minted UUID; a renegotiated offer within the
        # signaling TTL reuses it, so an existing row (only ever the owner's,
        # under RLS) is re-activated instead of violating the primary key.
        row = await db.get(Session, session_id)
        if row is None:
            db.add(Session(id=session_id, user_id=user_id, status="active"))
        else:
            row.status = "active"
            row.ended_at = None
            row.end_reason = None
        await db.commit()


async def session_is_active(session_id: str, user_id: str) -> bool:
    """Session-scoped liveness for the client's while-active poll. Answered
    from the DB row (shared truth), so it stays correct if the backend ever
    scales out: crash → the poll itself fails; restart → the boot sweep
    marked the row interrupted; media-timeout → the pipeline closed it."""
    async with user_scoped_session(user_id) as db:
        row = (
            await db.execute(
                select(Session).where(
                    Session.id == session_id, Session.user_id == user_id
                )
            )
        ).scalar()
        return row is not None and row.status == "active"


async def end_session_row(session_id: str, user_id: str, end_reason: str) -> None:
    async with user_scoped_session(user_id) as db:
        row = await db.get(Session, session_id)
        if row is not None:
            row.status = "ended"
            row.end_reason = end_reason
            row.ended_at = datetime.now(timezone.utc)
            await db.commit()


async def sweep_orphaned_sessions() -> int:
    """FR-32's boot-time sweep. On backend startup, any session still marked
    `active` was orphaned by a process death (crash, OOM, deploy restart):
    close it as `interrupted` (deliberately not an error — routine deploys
    cause this too), infer `ended_at` as the max timestamp across the
    session's recorded events (fallback: started_at), and emit the STT usage
    event the dead process never got to write.

    Correct only while the backend is single-instance (a booting instance may
    assume every active session is orphaned) — see the spec's scaling note.

    Cross-user by nature, so it runs on the BOOTSTRAP engine (RLS-exempt),
    which is legitimate here and only here: this is boot-time maintenance
    that executes before the app serves any traffic — not an admin-surface
    read path (those go through admin_scoped_session, FR-38)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from db import engine as _engine_mod

    bootstrap = _engine_mod._bootstrap_engine
    if bootstrap is None:
        return 0
    swept = 0
    boot_sessions = async_sessionmaker(bootstrap, expire_on_commit=False)
    async with boot_sessions() as db:
        orphans = (
            (await db.execute(select(Session).where(Session.status == "active")))
            .scalars()
            .all()
        )
        for s in orphans:
            latest = None
            for model, col in (
                (TranscriptEvent, TranscriptEvent.ts),
                (UsageEvent, UsageEvent.ts),
                (TurnMetric, TurnMetric.ts),
            ):
                ts = (
                    await db.execute(
                        select(func.max(col)).where(model.session_id == s.id)
                    )
                ).scalar()
                if ts is not None and (latest is None or ts > latest):
                    latest = ts
            ended_at = latest or s.started_at
            s.status = "ended"
            s.end_reason = "interrupted"
            s.ended_at = ended_at
            duration_s = max(0.0, (ended_at - s.started_at).total_seconds())
            db.add(
                UsageEvent(
                    user_id=s.user_id,
                    session_id=s.id,
                    turn_id=None,
                    ts=ended_at,
                    stage="stt",
                    unit="seconds",
                    quantity=duration_s,
                )
            )
            swept += 1
        await db.commit()
    if swept:
        logger.bind(component="db.sessions", event="session.sweep", count=swept).info(
            f"closed {swept} orphaned session(s) as interrupted"
        )
    return swept
