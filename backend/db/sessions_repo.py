"""Session row lifecycle (SDD §2.8 + §3)."""

from datetime import datetime, timezone

from db.engine import DEFAULT_USER_ID, session_factory
from db.models import Session


async def create_session_row(session_id: str) -> None:
    async with session_factory()() as db:
        db.add(Session(id=session_id, user_id=DEFAULT_USER_ID, status="active"))
        await db.commit()


async def end_session_row(session_id: str, end_reason: str) -> None:
    async with session_factory()() as db:
        row = await db.get(Session, session_id)
        if row is not None:
            row.status = "ended"
            row.end_reason = end_reason
            row.ended_at = datetime.now(timezone.utc)
            await db.commit()
