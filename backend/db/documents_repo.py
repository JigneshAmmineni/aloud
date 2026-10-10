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
from sqlalchemy import case, delete, func, insert, literal, select, text, update

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
    user_id: str, offset: int = 0, cap: int | None = None
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
                .limit(cap if cap is not None else WORKSPACE_LIST_CAP)
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


# ---------------------------------------------------------------------------
# FR-53: the agent tools' query half — pager, search, and edit modes.
# ---------------------------------------------------------------------------

# §4.11 knob table: the paged read's knobs live with the fold (FR-45's
# "tool owns the cap's VALUE" division amended openly by FR-53).
READ_DOCUMENT_MAX_CHARS = 8_000  # per-page char window (content, not decoration)
PAGE_MAX_LINES = 200  # per-page line cap — keeps the decoration bounded

SEARCH_RESULTS_CAP = 10
SEARCH_SCAN_CAP = 50
SEARCH_SNIPPET_CHARS = 200

# FR-53.7: editability is format-gated, not source-gated; pdf is read-only
# (extracted text is a lossy projection — editing it in place would
# misrepresent the upload). The gate lives in the UPDATE predicate itself.
EDITABLE_FORMATS = ("markdown", "text")

_LINE_CONTINUES = " [line continues]"


@dataclass
class PageEntry:
    line_no: int
    text: str
    continues: bool  # hard-cut: this line keeps going on the next page


def fold_pages(content: str, max_chars: int, max_lines: int) -> list[list[PageEntry]]:
    """The in-process pager fold (FR-53.2): boundaries snap to the last
    newline inside the char window; a line cap bounds the decoration; a
    single line longer than the page hard-cuts and KEEPS its line number on
    the next page. Pure — the repo fetches the one bounded row and folds
    here, which is what keeps these tests in the ordinary suite."""
    lines = content.split("\n")
    pages: list[list[PageEntry]] = []
    cur: list[PageEntry] = []
    cur_chars = 0
    i = 0
    offset = 0  # position inside a hard-cut line
    while i < len(lines):
        rest = lines[i][offset:]
        room = max_chars - cur_chars
        if cur and (len(cur) >= max_lines or room <= 0):
            pages.append(cur)
            cur, cur_chars = [], 0
            continue
        if len(rest) <= room or (not cur and len(rest) <= max_chars):
            cur.append(PageEntry(i + 1, rest, False))
            cur_chars += len(rest)
            i += 1
            offset = 0
        elif cur:
            # newline snap: the line fits a fresh page (or gets the full
            # window there) — close this page instead of mid-line cutting
            pages.append(cur)
            cur, cur_chars = [], 0
        else:
            # a single line longer than the whole page: hard cut
            cur.append(PageEntry(i + 1, rest[:max_chars], True))
            offset += max_chars
            pages.append(cur)
            cur, cur_chars = [], 0
    if cur or not pages:
        pages.append(cur)
    return pages


def render_page(entries: list[PageEntry]) -> str:
    """Line-number prefixes and the hard-cut marker are DECORATION, applied
    at render time only — old_str matches the stored content (FR-53.4)."""
    return "\n".join(
        f"{e.line_no}: {e.text}{_LINE_CONTINUES if e.continues else ''}"
        for e in entries
    )


def page_index_for_line(pages: list[list[PageEntry]], line: int) -> int | None:
    """The FIRST page carrying that line (a hard-cut line spans pages and
    resolves to where it starts) — the pager is the single owner of page
    boundaries (FR-53.2/53.3)."""
    for idx, entries in enumerate(pages):
        if any(e.line_no == line for e in entries):
            return idx
    return None


@dataclass
class DocumentPageResult:
    id: int
    title: str
    kind: str | None
    format: str
    page: int  # 1-based
    total_pages: int
    rendered: str
    first_line: int
    last_line: int
    total_lines: int
    char_count: int


async def read_document_page(
    user_id: str,
    document_id: int,
    *,
    page: int | None = None,
    line: int | None = None,
) -> DocumentPageResult | str:
    """FR-53.2: one owned document, one page. Returns the result, or a
    string reason: "not_found", or "page_out_of_range:{total}" for the
    steering message. `line` wins over `page` when both arrive."""
    async with user_scoped_session(user_id) as db:
        row = (
            await db.execute(
                select(Document).where(
                    Document.id == document_id, Document.user_id == user_id
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return "not_found"
    pages = fold_pages(row.content, READ_DOCUMENT_MAX_CHARS, PAGE_MAX_LINES)
    total = len(pages)
    if line is not None:
        idx = page_index_for_line(pages, line)
        if idx is None:
            return f"page_out_of_range:{total}"
    else:
        idx = (page or 1) - 1
        if idx < 0 or idx >= total:
            return f"page_out_of_range:{total}"
    entries = pages[idx]
    total_lines = row.content.count("\n") + 1
    return DocumentPageResult(
        id=row.id,
        title=row.title,
        kind=row.kind,
        format=row.format,
        page=idx + 1,
        total_pages=total,
        rendered=render_page(entries),
        first_line=entries[0].line_no if entries else 1,
        last_line=entries[-1].line_no if entries else 1,
        total_lines=total_lines,
        char_count=len(row.content),
    )


def _escape_like(query: str) -> str:
    r"""FR-53.3: the model's query is a LITERAL substring — %, _, and the
    escape character are escaped before entering the ILIKE pattern; a
    model-supplied wildcard must never widen the scan."""
    return (
        query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


@dataclass
class WorkspaceSearchHit:
    id: int
    title: str
    kind: str | None
    format: str
    match: str  # "content" wins when both halves hit (FR-53.3)
    title_matched: bool
    line: int | None  # first content match (approximate past a case-folding
    # expansion like U+0130 — stated, never fed to the replace chain)
    snippet: str | None  # sliced from the STORED content
    match_count: int  # case-insensitive content occurrences; NEVER a source
    # for expected_occurrences (that is exact_matches, single-document scope)


@dataclass
class WorkspaceSearchResult:
    hits: list[WorkspaceSearchHit]
    documents_matched: int  # among the scanned set — the results cap must
    # not truncate silently (30 matching docs returning 10 must say so)
    scanned: int
    scan_truncated: bool  # SEARCH_SCAN_CAP cut the scan itself


async def search_workspace(user_id: str, query: str) -> WorkspaceSearchResult:
    """FR-53.3 workspace scope: scan the newest SEARCH_SCAN_CAP documents,
    one result per document, snippets cut in SQL — the query returns match
    line and snippet, never whole content columns (a ten-hit search must
    not ship megabytes to produce kilobytes). Dialect-bound (ILIKE, in-SQL
    line counting): the Postgres-only test lane."""
    pat = "%" + _escape_like(query) + "%"
    sql = text(
        r"""
        WITH scanned AS (
            SELECT id, title, kind, format, content
            FROM documents
            WHERE user_id = :uid
            ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
            LIMIT :scan_cap
        ),
        hits AS (
            SELECT *,
                (title ILIKE :pat ESCAPE '\') AS title_hit,
                (content ILIKE :pat ESCAPE '\') AS content_hit,
                CASE WHEN content ILIKE :pat ESCAPE '\'
                     THEN strpos(lower(content), lower(:q)) END AS pos
            FROM scanned
            WHERE title ILIKE :pat ESCAPE '\' OR content ILIKE :pat ESCAPE '\'
        )
        SELECT id, title, kind, format, title_hit, content_hit,
            CASE WHEN content_hit THEN
                1 + length(substr(content, 1, pos - 1))
                  - length(replace(substr(content, 1, pos - 1), chr(10), ''))
            END AS line,
            CASE WHEN content_hit THEN
                substr(content,
                       (CASE WHEN pos > :ctx THEN pos - :ctx ELSE 1 END)::int,
                       (:snip)::int)
            END AS snippet,
            CASE WHEN content_hit THEN
                (length(lower(content))
                 - length(replace(lower(content), lower(:q), '')))
                / length(:q)
            ELSE 0 END AS match_count,
            count(*) OVER () AS documents_matched
        FROM hits
        ORDER BY COALESCE(id, id) DESC
        LIMIT :results_cap
        """
    )
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
                sql,
                {
                    "uid": user_id,
                    "pat": pat,
                    "q": query,
                    "scan_cap": SEARCH_SCAN_CAP,
                    "results_cap": SEARCH_RESULTS_CAP,
                    "ctx": SEARCH_SNIPPET_CHARS // 2,
                    "snip": SEARCH_SNIPPET_CHARS,
                },
            )
        ).all()
    hits = [
        WorkspaceSearchHit(
            id=r.id,
            title=r.title,
            kind=r.kind,
            format=r.format,
            match="content" if r.content_hit else "title",
            title_matched=bool(r.title_hit),
            line=int(r.line) if r.content_hit and r.line is not None else None,
            snippet=r.snippet if r.content_hit else None,
            match_count=int(r.match_count),
        )
        for r in rows
    ]
    return WorkspaceSearchResult(
        hits=hits,
        documents_matched=int(rows[0].documents_matched) if rows else 0,
        scanned=min(total, SEARCH_SCAN_CAP),
        scan_truncated=total > SEARCH_SCAN_CAP,
    )


@dataclass
class DocumentSearchMatch:
    line: int  # approximate for case-insensitive positions (stated)
    snippet: str  # sliced from the STORED content at the located offset


@dataclass
class DocumentSearchResult:
    matches: list[DocumentSearchMatch]
    total_matches: int  # case-insensitive — the search's own semantics
    exact_matches: int  # verbatim — the replace's semantics; THE designed
    # source for replace_all's expected_occurrences


async def search_document(
    user_id: str, document_id: int, query: str
) -> DocumentSearchResult | None:
    """FR-53.3 single-document scope: one result per match plus the two
    exact, uncapped counts — total_matches (insensitive) and exact_matches
    (verbatim), because the two engines disagree on any mixed-case document
    and feeding the insensitive count into the verbatim predicate
    manufactures the refusal round this exists to avoid. Match positions
    enumerate via string_to_array over the lowered pair; snippets slice the
    STORED content at those offsets (approximate only past a case-folding
    expansion — stated; exact_matches comes from the unlowered pair and is
    the only count that feeds the replace chain). Postgres-only lane."""
    sql = text(
        """
        WITH doc AS (
            SELECT content FROM documents
            WHERE id = :id AND user_id = :uid
        ),
        segs AS (
            SELECT t.seg, t.ord::int AS ord
            FROM doc,
                 unnest(string_to_array(lower(doc.content), lower(:q)))
                 WITH ORDINALITY AS t(seg, ord)
        ),
        marks AS (
            SELECT ord,
                sum(length(seg)) OVER (ORDER BY ord)
                    + (ord - 1) * length(:q) + 1 AS pos
            FROM segs
            WHERE ord < (SELECT count(*) FROM segs)
        )
        SELECT m.pos,
            1 + length(substr(d.content, 1, m.pos::int - 1))
              - length(replace(substr(d.content, 1, m.pos::int - 1),
                               chr(10), '')) AS line,
            substr(d.content,
                   (CASE WHEN m.pos > :ctx THEN m.pos - :ctx ELSE 1 END)::int,
                   (:snip)::int) AS snippet,
            (SELECT count(*) - 1 FROM segs) AS total_matches,
            (length(d.content) - length(replace(d.content, :q, '')))
                / length(:q) AS exact_matches
        FROM marks m, doc d
        ORDER BY m.ord
        LIMIT :results_cap
        """
    )
    async with user_scoped_session(user_id) as db:
        exists = (
            await db.execute(
                select(Document.id).where(
                    Document.id == document_id, Document.user_id == user_id
                )
            )
        ).first()
        if exists is None:
            return None
        rows = (
            await db.execute(
                sql,
                {
                    "id": document_id,
                    "uid": user_id,
                    "q": query,
                    "ctx": SEARCH_SNIPPET_CHARS // 2,
                    "snip": SEARCH_SNIPPET_CHARS,
                    "results_cap": SEARCH_RESULTS_CAP,
                },
            )
        ).all()
    if not rows:
        return DocumentSearchResult(matches=[], total_matches=0, exact_matches=0)
    return DocumentSearchResult(
        matches=[
            DocumentSearchMatch(line=int(r.line), snippet=r.snippet) for r in rows
        ],
        total_matches=int(rows[0].total_matches),
        exact_matches=int(rows[0].exact_matches),
    )


# ---------------------------------------------------------------------------
# FR-53.4–53.7: edit modes. Enforcement is atomic and DB-side: every gate —
# ownership, the format gate, the occurrence predicate, the growth ceiling —
# lives INSIDE the UPDATE's own WHERE, where Postgres re-evaluates it against
# the current row version when a concurrent writer got there first (a
# separate CTE count reads the statement snapshot and is NOT re-run, which
# would reopen the outlived-write race). The steering message's facts come
# from a read-only diagnostic AFTER a failed UPDATE — a refused write wrote
# nothing, so no read-modify-write window exists.
# ---------------------------------------------------------------------------


def _kind_or_format(kind: str | None, format: str) -> str:
    """FR-32 amended: artifact_edited's detail is the document's kind, or
    its format for uploaded documents, which have none — a defined value,
    never the NULL FR-50's migration test names as a bug symptom."""
    return kind if kind is not None else format


def _document_edit_event(
    kind: str | None, format: str, user_id: str, session_id: str, turn_id: int | None
) -> UsageEvent:
    """session_id is the EDITING session, never the document's originating
    one: the defining case edits a prior-session document, and turn_id
    belongs to the current session (FR-36's per-turn cost table)."""
    return UsageEvent(
        user_id=user_id,
        session_id=session_id,
        turn_id=turn_id,
        ts=datetime.now(timezone.utc),
        stage="artifact",
        unit="edits",
        quantity=1.0,
        detail=_kind_or_format(kind, format),
    )


def _growth_cap(ceiling: int):
    """FR-53.6: gates on GROWTH, not absolute size — rows above the ceiling
    exist by construction (extract_text persists the cap plus its marker;
    pre-§4.11 create_artifact capped nothing), and an absolute check would
    make exactly those rows permanently uneditable. Spelled portably (a
    CASE, not Postgres' GREATEST) so the ceiling tests run in the ordinary
    suite."""
    return case(
        (func.length(Document.content) > ceiling, func.length(Document.content)),
        else_=ceiling,
    )


async def str_replace_document(
    user_id: str,
    document_id: int,
    old_str: str,
    new_str: str,
    *,
    replace_all: bool,
    expected_occurrences: int | None,
    ceiling: int,
    turn_id: int | None,
    session_id: str,
) -> Document | None:
    """FR-53.4: one UPDATE whose WHERE verifies ownership, the format gate,
    the occurrence count (exactly 1, or exactly expected_occurrences under
    replace_all), and the growth ceiling; SET replaces in the same single
    statement. None = refused or missing — the caller runs the diagnostic.
    The counting predicate is portable (length/replace exist in SQLite)."""
    required = expected_occurrences if replace_all else 1
    count_expr = (
        func.length(Document.content)
        - func.length(func.replace(Document.content, old_str, ""))
    ) / len(old_str)
    stmt = (
        update(Document)
        .where(
            Document.id == document_id,
            Document.user_id == user_id,
            Document.format.in_(EDITABLE_FORMATS),
            count_expr == required,
            func.length(func.replace(Document.content, old_str, new_str))
            <= _growth_cap(ceiling),
        )
        .values(
            content=func.replace(Document.content, old_str, new_str),
            updated_at=datetime.now(timezone.utc),
        )
        .returning(Document)
    )
    async with user_scoped_session(user_id) as db:
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        db.add(
            _document_edit_event(row.kind, row.format, user_id, session_id, turn_id)
        )
        await db.commit()
    return row


async def append_document_content(
    user_id: str,
    document_id: int,
    text_to_append: str,
    *,
    ceiling: int,
    turn_id: int | None,
    session_id: str,
) -> Document | None:
    """FR-45's atomic DB-side concatenation, transferred verbatim — plus
    the format gate and the growth ceiling in the same predicate."""
    stmt = (
        update(Document)
        .where(
            Document.id == document_id,
            Document.user_id == user_id,
            Document.format.in_(EDITABLE_FORMATS),
            func.length(Document.content) + len(text_to_append)
            <= _growth_cap(ceiling),
        )
        .values(
            content=Document.content + text_to_append,
            updated_at=datetime.now(timezone.utc),
        )
        .returning(Document)
    )
    async with user_scoped_session(user_id) as db:
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        db.add(
            _document_edit_event(row.kind, row.format, user_id, session_id, turn_id)
        )
        await db.commit()
    return row


async def replace_document_content(
    user_id: str,
    document_id: int,
    content: str,
    title: str | None,
    *,
    ceiling: int,
    max_replaceable_chars: int,
    turn_id: int | None,
    session_id: str,
) -> Document | None:
    """FR-45's full replace, transferred: the cap-refusal holds inside the
    UPDATE's predicate (a detached FR-46 write can grow the row past the
    readable window between the tool's read and this statement — a replace
    landing then would delete content the model provably never saw)."""
    values: dict = {"content": content, "updated_at": datetime.now(timezone.utc)}
    if title:
        values["title"] = title
    stmt = (
        update(Document)
        .where(
            Document.id == document_id,
            Document.user_id == user_id,
            Document.format.in_(EDITABLE_FORMATS),
            func.length(Document.content) <= max_replaceable_chars,
            len(content) <= _growth_cap(ceiling),
        )
        .values(**values)
        .returning(Document)
    )
    async with user_scoped_session(user_id) as db:
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        db.add(
            _document_edit_event(row.kind, row.format, user_id, session_id, turn_id)
        )
        await db.commit()
    return row


async def insert_document_line(
    user_id: str,
    document_id: int,
    line: int,
    text_to_insert: str,
    *,
    ceiling: int,
    turn_id: int | None,
    session_id: str,
) -> Document | None:
    """FR-53.5: line-addressed insert, built from the stored content inside
    the same statement with Postgres string builtins (array_to_string over
    string_to_array — never read-modify-write). Dialect-specific, stated:
    its atomicity and bounds tests run in the Postgres-only lane. line=0
    prepends; line=n_lines appends after the last line."""
    sql = text(
        """
        UPDATE documents SET
            content = CASE
                WHEN :line = 0 THEN :txt || chr(10) || content
                WHEN :line >= (length(content)
                               - length(replace(content, chr(10), '')) + 1)
                    THEN content || chr(10) || :txt
                ELSE array_to_string(
                         (string_to_array(content, chr(10)))[1:(:line)], chr(10))
                     || chr(10) || :txt || chr(10)
                     || array_to_string(
                         (string_to_array(content, chr(10)))[(:line + 1):], chr(10))
            END,
            updated_at = :now
        WHERE id = :id AND user_id = :uid
          AND format IN ('markdown', 'text')
          AND :line >= 0
          AND :line <= (length(content)
                        - length(replace(content, chr(10), '')) + 1)
          AND length(content) + length(:txt) + 1 <=
              CASE WHEN length(content) > :ceiling
                   THEN length(content) ELSE :ceiling END
        RETURNING id, user_id, session_id, source, format, kind,
                  legacy_artifact_id, created_at, updated_at, title, content
        """
    )
    async with user_scoped_session(user_id) as db:
        row = (
            await db.execute(
                sql,
                {
                    "id": document_id,
                    "uid": user_id,
                    "line": line,
                    "txt": text_to_insert,
                    "ceiling": ceiling,
                    "now": datetime.now(timezone.utc),
                },
            )
        ).first()
        if row is None:
            return None
        db.add(
            _document_edit_event(row.kind, row.format, user_id, session_id, turn_id)
        )
        await db.commit()
    doc = Document(
        user_id=row.user_id,
        session_id=row.session_id,
        source=row.source,
        format=row.format,
        kind=row.kind,
        legacy_artifact_id=row.legacy_artifact_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        title=row.title,
        content=row.content,
    )
    doc.id = row.id
    return doc


@dataclass
class EditDiagnosis:
    """Why a write's UPDATE matched zero rows. `reason` follows the stated
    precedence — the nearest ACTIONABLE obstacle, never whichever predicate
    the implementation happened to re-check first: not-found/not-owned →
    format gate → occurrence count / line bounds → growth ceiling."""

    reason: str  # not_found | not_editable | no_match | count_mismatch
    #              | bad_line | over_ceiling | unknown
    format: str | None = None
    occurrences: int | None = None
    match_lines: list[int] | None = None
    total_lines: int | None = None


async def diagnose_edit_failure(
    user_id: str,
    document_id: int,
    *,
    ceiling: int,
    old_str: str | None = None,
    new_str: str | None = None,
    required: int | None = None,
    insert_line: int | None = None,
    insert_text: str | None = None,
) -> EditDiagnosis:
    """The read-only diagnostic after a failed UPDATE (explicitly permitted:
    a refused write wrote nothing). The base read is portable; the
    multi-match LINE enumeration is string_to_array/strpos-family and runs
    only on Postgres — old_str is model-controlled text, and a regex-based
    lookup on "plan (v2)" would raise mid-turn or quote the wrong lines."""
    cols = [Document.format, func.length(Document.content)]
    if old_str is not None:
        cols.append(
            (
                func.length(Document.content)
                - func.length(func.replace(Document.content, old_str, ""))
            )
            / len(old_str)
        )
        cols.append(
            func.length(
                func.replace(Document.content, old_str, new_str or "")
            )
        )
    else:
        cols.append(
            func.length(Document.content)
            - func.length(func.replace(Document.content, "\n", ""))
            + 1
        )
    async with user_scoped_session(user_id) as db:
        row = (
            await db.execute(
                select(*cols).where(
                    Document.id == document_id, Document.user_id == user_id
                )
            )
        ).first()
        if row is None:
            return EditDiagnosis(reason="not_found")
        format, cur_len = row[0], row[1]
        if format not in EDITABLE_FORMATS:
            return EditDiagnosis(reason="not_editable", format=format)
        cap = max(ceiling, cur_len)

        if old_str is not None:
            occurrences, new_len = int(row[2]), int(row[3])
            if occurrences == 0:
                return EditDiagnosis(reason="no_match", format=format)
            if occurrences != required:
                lines: list[int] = []
                if db.bind.dialect.name == "postgresql":
                    line_rows = (
                        await db.execute(
                            text(
                                """
                                WITH doc AS (
                                    SELECT content FROM documents
                                    WHERE id = :id AND user_id = :uid
                                ),
                                segs AS (
                                    SELECT t.seg, t.ord::int AS ord
                                    FROM doc,
                                         unnest(string_to_array(doc.content, :old))
                                         WITH ORDINALITY AS t(seg, ord)
                                ),
                                nl AS (
                                    SELECT ord,
                                        length(seg)
                                        - length(replace(seg, chr(10), '')) AS n
                                    FROM segs
                                )
                                SELECT 1 + sum(n) OVER (ORDER BY ord)
                                         + (ord - 1) * :old_nl AS line
                                FROM nl
                                WHERE ord < (SELECT count(*) FROM segs)
                                ORDER BY ord
                                LIMIT 10
                                """
                            ),
                            {
                                "id": document_id,
                                "uid": user_id,
                                "old": old_str,
                                "old_nl": old_str.count("\n"),
                            },
                        )
                    ).all()
                    lines = [int(r.line) for r in line_rows]
                return EditDiagnosis(
                    reason="count_mismatch",
                    format=format,
                    occurrences=occurrences,
                    match_lines=lines,
                )
            if new_len > cap:
                return EditDiagnosis(reason="over_ceiling", format=format)
            return EditDiagnosis(reason="unknown", format=format)

        total_lines = int(row[2])
        if insert_line is not None and not (0 <= insert_line <= total_lines):
            return EditDiagnosis(
                reason="bad_line", format=format, total_lines=total_lines
            )
        if (
            insert_text is not None
            and cur_len + len(insert_text) + 1 > cap
        ):
            return EditDiagnosis(reason="over_ceiling", format=format)
        return EditDiagnosis(
            reason="unknown", format=format, total_lines=total_lines
        )
