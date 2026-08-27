"""Session row lifecycle. Auth-agnostic: user_id arrives as a plain argument
(never defaulted — a forgotten argument must be a loud TypeError), and every
query runs in a user-scoped session (FR-31)."""

from datetime import datetime, timezone

from db.engine import user_scoped_session
from db.models import Session


async def create_session_row(session_id: str, user_id: str) -> None:
    async with user_scoped_session(user_id) as db:
        db.add(Session(id=session_id, user_id=user_id, status="active"))
        await db.commit()


async def end_session_row(session_id: str, user_id: str, end_reason: str) -> None:
    async with user_scoped_session(user_id) as db:
        row = await db.get(Session, session_id)
        if row is not None:
            row.status = "ended"
            row.end_reason = end_reason
            row.ended_at = datetime.now(timezone.utc)
            await db.commit()
