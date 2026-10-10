"""Document queries for §4.11 (FR-50–FR-53). Standard repo discipline:
user_id is a plain required argument (verified upstream, never
model-supplied), every query runs user-scoped (FR-31 RLS backstop), and the
repo owns filtering, ordering, and caps — routes and tools stay
schema-shaped. The InMemoryDocumentStore this replaces named exactly this
module as its swap point.

Writes that spend something pair the row change with its usage event in ONE
transaction (FR-32); uploads emit no usage event (nothing was spent — the
`document.uploaded` structured log is the operational record).

Size ceilings are passed in by callers (the values live with the upload/
extraction module and the tool layer); this module owns their atomicity.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import delete, func, insert, literal, select

from db.engine import user_scoped_session
from db.models import Artifact, Document, UsageEvent

# §4.11 knob table: caps live in the repo layer.
WORKSPACE_LIST_CAP = 200  # GET /documents; tracks the quota
LIST_DOCUMENTS_CAP = 20  # list_documents tool (renamed from LIST_ARTIFACTS_CAP)
MAX_DOCUMENTS_PER_USER = 200  # per-user row quota; binds upload and create


class DocumentQuotaError(Exception):
    """The per-user document quota is full. POST /documents maps this to a
    clear 400 naming the limit; the tool layer maps it to a steering result
    — never the generic could-not-be-saved fallback (FR-51)."""


def _activity_order():
    """Newest-activity-first with id DESC as the tiebreaker — activity ties
    are the norm, not the edge (rows seeded in one transaction share now()),
    and untied ordering makes offset paging repeat and skip rows."""
    return (
        func.coalesce(Document.updated_at, Document.created_at).desc(),
        Document.id.desc(),
    )


async def _quota_insert(db, user_id: str, columns: list[str], values: list):
    """INSERT … SELECT … WHERE count < quota, one statement — an approximate
    bound under concurrency, stated rather than asserted away (FR-51): two
    boundary inserts can each read a passing count at READ COMMITTED, an
    overshoot of at-most-concurrent-inserts, cosmetic for a per-user cap."""
    sel = select(*[literal(v) for v in values]).where(
        select(func.count())
        .select_from(Document)
        .where(Document.user_id == user_id)
        .scalar_subquery()
        < MAX_DOCUMENTS_PER_USER
    )
    stmt = insert(Document).from_select(columns, sel).returning(Document)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise DocumentQuotaError(
            f"document limit reached ({MAX_DOCUMENTS_PER_USER} per account)"
        )
    return row


async def insert_upload_row(
    user_id: str, title: str, format: str, content: str
) -> Document:
    """Persist an upload (FR-51). No usage event — nothing was spent."""
    async with user_scoped_session(user_id) as db:
        row = await _quota_insert(
            db,
            user_id,
            ["user_id", "source", "format", "title", "content"],
            [user_id, "uploaded", format, title, content],
        )
        await db.commit()
    return row


async def create_document_row(
    user_id: str,
    session_id: str,
    kind: str,
    title: str,
    content: str,
    turn_id: int | None,
) -> Document:
    """Agent-created document + artifact_created usage event, one
    transaction (FR-32 discipline, transferred from the artifacts repo)."""
    async with user_scoped_session(user_id) as db:
        row = await _quota_insert(
            db,
            user_id,
            ["user_id", "session_id", "source", "format", "kind", "title", "content"],
            [user_id, session_id, "agent", "markdown", kind, title, content],
        )
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
    return row


@dataclass
class WorkspaceItem:
    """List-row shape: metadata plus title and a computed char_count —
    never content (FR-52)."""

    id: int
    source: str
    format: str
    kind: str | None
    title: str
    char_count: int
    created_at: datetime
    updated_at: datetime | None


async def list_workspace_rows(
    user_id: str, offset: int = 0
) -> tuple[list[WorkspaceItem], int]:
    """GET /documents: both sources, newest-activity-first, capped, with a
    total so truncation is visible. `offset` is clamped >= 0 (FastAPI's int
    coercion accepts negatives and Postgres errors on OFFSET -1); an offset
    past total returns an empty page with the same total, never an error."""
    offset = max(0, offset)
    async with user_scoped_session(user_id) as db:
        total = (
            await db.execute(
                select(func.count())
                .select_from(Document)
                .where(Document.user_id == user_id)
            )
        ).scalar_one()
        rows = (
            await db.execute(
                select(
                    Document.id,
                    Document.source,
                    Document.format,
                    Document.kind,
                    Document.title,
                    func.length(Document.content).label("char_count"),
                    Document.created_at,
                    Document.updated_at,
                )
                .where(Document.user_id == user_id)
                .order_by(*_activity_order())
                .offset(offset)
                .limit(WORKSPACE_LIST_CAP)
            )
        ).all()
        return [WorkspaceItem(*r) for r in rows], total


async def get_document_row(user_id: str, document_id: int) -> Document | None:
    """One owned document, full content — another user's id resolves to
    None (NFR-8). Serves preview, download, and the oversized-announce
    refetch (FR-52/55)."""
    async with user_scoped_session(user_id) as db:
        return (
            await db.execute(
                select(Document).where(
                    Document.id == document_id, Document.user_id == user_id
                )
            )
        ).scalar_one_or_none()


async def delete_document_row(user_id: str, document_id: int) -> bool:
    """Hard delete (FR-52): for a migrated document the SAME transaction
    deletes the paired legacy artifacts row via legacy_artifact_id —
    otherwise the 🔒 title/content would survive on disk until the drop
    release, behind an affordance that told the user they were destroyed.
    Usage events are untouched (spent is recorded). Logs the structured
    event a hard, irreversible delete needs — ids and source only."""
    async with user_scoped_session(user_id) as db:
        row = (
            await db.execute(
                select(Document.id, Document.source, Document.legacy_artifact_id).where(
                    Document.id == document_id, Document.user_id == user_id
                )
            )
        ).first()
        if row is None:
            return False
        await db.execute(
            delete(Document).where(
                Document.id == document_id, Document.user_id == user_id
            )
        )
        if row.legacy_artifact_id is not None:
            await db.execute(
                delete(Artifact).where(
                    Artifact.id == row.legacy_artifact_id,
                    Artifact.user_id == user_id,
                )
            )
        await db.commit()
    logger.bind(
        component="db",
        event="document.deleted",
        document_id=document_id,
        source=row.source,
        paired_legacy_row=row.legacy_artifact_id is not None,
    ).info("document hard-deleted")
    return True


@dataclass
class AttachedDocument:
    """What attach resolution hands the context block (FR-51/21)."""

    id: int
    title: str
    format: str
    content: str


async def resolve_attach_rows(
    user_id: str, ids: list[int], budget: int
) -> list[AttachedDocument]:
    """Attach resolution, bounded in the repo query (FR-51): a lengths-first
    resolve (no content) walks the attach order against the remaining
    MAX_TOTAL_CHARS budget; content is fetched only for documents that fit,
    the boundary document sliced SQL-side with substr (portable — this is
    the one query on the session-establishment path, and its tests live in
    the ordinary suite). Unknown and unowned ids are silently skipped (a
    stale reference must not break a session). The block's own truncation
    stays a backstop, no longer the only bound."""
    if not ids:
        return []
    async with user_scoped_session(user_id) as db:
        lengths = dict(
            (
                await db.execute(
                    select(Document.id, func.length(Document.content)).where(
                        Document.id.in_(ids), Document.user_id == user_id
                    )
                )
            ).all()
        )
        remaining = budget
        full_ids: list[int] = []
        boundary: tuple[int, int] | None = None  # (id, chars that still fit)
        for doc_id in ids:  # attach order
            length = lengths.get(doc_id)
            if length is None or remaining <= 0:
                continue
            if length <= remaining:
                full_ids.append(doc_id)
                remaining -= length
            elif boundary is None:
                boundary = (doc_id, remaining)
                remaining = 0

        out: dict[int, AttachedDocument] = {}
        if full_ids:
            for r in (
                await db.execute(
                    select(
                        Document.id, Document.title, Document.format, Document.content
                    ).where(Document.id.in_(full_ids), Document.user_id == user_id)
                )
            ).all():
                out[r.id] = AttachedDocument(r.id, r.title, r.format, r.content)
        if boundary is not None:
            b_id, b_chars = boundary
            r = (
                await db.execute(
                    select(
                        Document.id,
                        Document.title,
                        Document.format,
                        func.substr(Document.content, 1, b_chars),
                    ).where(Document.id == b_id, Document.user_id == user_id)
                )
            ).first()
            if r is not None:
                out[b_id] = AttachedDocument(r[0], r[1], r[2], r[3])

        ordered = full_ids + ([boundary[0]] if boundary else [])
        return [out[i] for i in ordered if i in out]
