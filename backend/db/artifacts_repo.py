"""Artifact queries for the FR-45 tools. Standard repo discipline: user_id
is a plain required argument (verified upstream, never model-supplied), every
query runs user-scoped (FR-31 RLS backstop), and the repo owns filtering,
ordering, and caps — tools stay schema-shaped.

Writes pair the row change with its usage event in ONE transaction (FR-32:
admin counts read usage_events, never the content table — the two must not
diverge). Edits follow creates' discipline: `artifact_edited` is
stage='artifact', unit='edits', detail=kind, turn-attributed.
"""

from datetime import datetime, timezone

from sqlalchemy import func, select, update

from db.engine import user_scoped_session
from db.models import Artifact, UsageEvent

# FR-45 assigns caps to the repo layer.
LIST_ARTIFACTS_CAP = 20


def _edit_event(row: Artifact, user_id: str, turn_id: int | None) -> UsageEvent:
    return UsageEvent(
        user_id=user_id,
        session_id=row.session_id,
        turn_id=turn_id,
        ts=datetime.now(timezone.utc),
        stage="artifact",
        unit="edits",
        quantity=1.0,
        detail=row.kind,
    )


async def create_artifact_row(
    user_id: str,
    session_id: str,
    kind: str,
    title: str,
    content: str,
    turn_id: int | None,
) -> Artifact:
    """Row + artifact_created usage event, one transaction (raises on
    failure — the tool handler owns turning that into a spoken result)."""
    row = Artifact(
        session_id=session_id,
        user_id=user_id,
        kind=kind,
        title=title,
        content=content,
    )
    async with user_scoped_session(user_id) as db:
        db.add(row)
        db.add(
            UsageEvent(
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                ts=datetime.now(timezone.utc),
                stage="artifact",
                unit="count",
                quantity=1.0,
                detail=kind,
            )
        )
        await db.commit()
        # No refresh: id is populated by the INSERT's RETURNING at flush; a
        # post-commit refresh would run in a fresh transaction whose RLS
        # context has evaporated.
    return row


async def list_artifact_rows(user_id: str) -> list[Artifact]:
    """The user's artifacts across sessions, most-recent activity first
    (a just-edited artifact surfaces), capped."""
    async with user_scoped_session(user_id) as db:
        rows = (
            await db.execute(
                select(Artifact)
                .where(Artifact.user_id == user_id)
                .order_by(
                    func.coalesce(Artifact.updated_at, Artifact.created_at).desc()
                )
                .limit(LIST_ARTIFACTS_CAP)
            )
        ).scalars()
        return list(rows)


async def get_artifact_row(user_id: str, artifact_id: int) -> Artifact | None:
    """One owned artifact — another user's id resolves to None (NFR-8)."""
    async with user_scoped_session(user_id) as db:
        return (
            await db.execute(
                select(Artifact).where(
                    Artifact.id == artifact_id, Artifact.user_id == user_id
                )
            )
        ).scalar_one_or_none()


async def replace_artifact_content(
    user_id: str,
    artifact_id: int,
    content: str,
    title: str | None,
    turn_id: int | None,
) -> Artifact | None:
    """Full-content replace + artifact_edited event, one transaction.
    Returns the updated row, or None when the id isn't the caller's
    (the cap-refusal rule is the tool's — it owns READ_ARTIFACT_MAX_CHARS)."""
    values: dict = {"content": content, "updated_at": datetime.now(timezone.utc)}
    if title:
        values["title"] = title
    async with user_scoped_session(user_id) as db:
        row = (
            await db.execute(
                update(Artifact)
                .where(Artifact.id == artifact_id, Artifact.user_id == user_id)
                .values(**values)
                .returning(Artifact)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        db.add(_edit_event(row, user_id, turn_id))
        await db.commit()
    return row


async def append_artifact_content(
    user_id: str,
    artifact_id: int,
    text: str,
    title: str | None,
    turn_id: int | None,
) -> Artifact | None:
    """ATOMIC DB-side concatenation (content = content || :text, RETURNING
    the post-edit row) — never read-modify-write, which would silently lose
    an edit when FR-46 lets a write outlive its turn and race the next
    one. RETURNING also hands the artifact.updated announce its full
    payload without a follow-up SELECT reopening the race."""
    values: dict = {
        "content": Artifact.content + text,
        "updated_at": datetime.now(timezone.utc),
    }
    if title:
        values["title"] = title
    async with user_scoped_session(user_id) as db:
        row = (
            await db.execute(
                update(Artifact)
                .where(Artifact.id == artifact_id, Artifact.user_id == user_id)
                .values(**values)
                .returning(Artifact)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        db.add(_edit_event(row, user_id, turn_id))
        await db.commit()
    return row
