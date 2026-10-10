"""LLM tools. FR-45 amended by §4.11/FR-53: the provider-neutral registry
the agent loop consults — each tool is a name, JSON-schema parameters, an
async handler, and a read/write classification (consumed by FR-46's
interruption rules). Handlers receive verified identity and the
panel-announce emit callback from session state via ToolContext — never
from the model. Queries live in db/documents_repo; tools stay
schema-shaped and own the steering MESSAGES (the repo owns the gates).
"""

from dataclasses import dataclass
from typing import Awaitable, Callable

from loguru import logger

from agent.registry import notify_document_updated
from app.documents import MAX_DOC_CHARS
from db.documents_repo import (
    LIST_DOCUMENTS_CAP,
    READ_DOCUMENT_MAX_CHARS,
    DocumentQuotaError,
    EditDiagnosis,
    append_document_content,
    create_document_row,
    diagnose_edit_failure,
    get_document_row,
    insert_document_line,
    list_workspace_rows,
    read_document_page,
    replace_document_content,
    search_document,
    search_workspace,
    str_replace_document,
)

DOCUMENT_KINDS = ("summary", "action_items", "cleaned_idea")

# FR-53.9: the announce payload carries content only up to this cap — the
# data channel shares the session's transport with live audio, and a
# five-character str_replace must not ship a 200k document mid-session.
# Above it: content_omitted + char_count; the client refetches on demand.
ANNOUNCE_CONTENT_MAX_CHARS = 16_000

# Steering echoes are bounded: old_str is model-controlled and the loop
# re-sends every tool result on each remaining step (FR-53.4).
_ECHO_MAX = 200
_LINES_MAX = 10


@dataclass
class ToolContext:
    """What handlers receive from SESSION STATE, never from the model:
    verified identity, the current turn for usage attribution (FR-45), and
    the loop-injected server-message emit callback — the panel-announce
    seam that keeps Pipecat types out of this module and is safe to call
    after the pipeline closed (FR-46: silent no-op then)."""

    session_id: str
    user_id: str
    turn_id: int | None
    emit: Callable[[dict], Awaitable[None]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema; agent/providers.py translates
    handler: Callable[[dict, ToolContext], Awaitable[dict]]
    is_write: bool  # FR-46: writes finish, reads are abandoned


def _panel_document(row, announce_type: str) -> dict:
    """FR-53.9/55: metadata always (char_count included — the client's
    budget math must not run on stale numbers); content only under the cap."""
    omitted = len(row.content) > ANNOUNCE_CONTENT_MAX_CHARS
    return {
        "type": announce_type,
        "document": {
            "id": row.id,
            "title": row.title,
            "kind": row.kind,
            "format": row.format,
            "source": row.source,
            "char_count": len(row.content),
            "content": None if omitted else row.content,
            "content_omitted": omitted,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        },
    }


def _echo(s: str) -> str:
    return s if len(s) <= _ECHO_MAX else s[:_ECHO_MAX] + "…[truncated]"


async def _create_document(args: dict, ctx: ToolContext) -> dict:
    log = logger.bind(session_id=ctx.session_id, component="agent.tools")
    title = str(args.get("title", "")).strip() or "Untitled"
    kind = args.get("kind", "summary")
    if kind not in DOCUMENT_KINDS:
        kind = "summary"
    content = str(args.get("content", "")).strip()
    if not content:
        return {"status": "error", "error": "content is required"}
    if len(content) > MAX_DOC_CHARS:
        # FR-53.6: create gates on the same ceiling every write mode does.
        return {
            "status": "refused",
            "error": (
                f"this document would exceed the size limit "
                f"({MAX_DOC_CHARS} characters) — split it into smaller "
                "documents"
            ),
        }
    try:
        row = await create_document_row(
            ctx.user_id, ctx.session_id, kind, title, content, ctx.turn_id
        )
    except DocumentQuotaError as e:
        # typed, steerable — never the generic could-not-be-saved fallback
        return {"status": "refused", "error": str(e)}
    except Exception as e:
        # exception TYPE only: SQLAlchemy errors render the SQL parameters —
        # document title and content — and ERROR ships to Cloud Logging (NFR-9)
        log.bind(event="tool.create_document_failed").error(
            f"document save failed: {type(e).__name__}"
        )
        return {"status": "error", "error": "the document could not be saved"}
    await ctx.emit(_panel_document(row, "document.created"))
    # No title in logs: documents.title is 🔒 and INFO ships to Cloud
    # Logging (FR-39/NFR-9).
    log.bind(event="tool.create_document", kind=kind).info(
        f"document saved ({kind}, {len(content)} chars)"
    )
    return {
        "status": "created",
        "document_id": row.id,
        "title": title,
        "note": "Document is now visible on the user's screen.",
    }


async def _list_documents(args: dict, ctx: ToolContext) -> dict:
    page = args.get("page")
    try:
        page = max(1, int(page)) if page is not None else 1
    except (TypeError, ValueError):
        return {"status": "error", "error": "page must be an integer"}
    try:
        items, total = await list_workspace_rows(
            ctx.user_id,
            offset=(page - 1) * LIST_DOCUMENTS_CAP,
            cap=LIST_DOCUMENTS_CAP,
        )
    except Exception as e:
        # a DB blip is a tool RESULT, never a raised turn (round-4 rule);
        # exception type only (NFR-9)
        logger.bind(session_id=ctx.session_id, component="agent.tools").bind(
            event="tool.list_documents_failed"
        ).error(f"document list failed: {type(e).__name__}")
        return {"status": "error", "error": "could not list the documents"}
    return {
        "documents": [
            {
                "id": it.id,
                "title": it.title,
                "source": it.source,
                "kind": it.kind,
                "format": it.format,
                "char_count": it.char_count,
                "created_at": it.created_at.isoformat() if it.created_at else None,
                "updated_at": it.updated_at.isoformat() if it.updated_at else None,
            }
            for it in items
        ],
        "count": len(items),
        "total": total,
        "page": page,
        # FR-53.1: three caps otherwise compose into an unreachable region
        # (20 listed, 50 scanned, 200 permitted)
        "total_pages": max(1, -(-total // LIST_DOCUMENTS_CAP)),
    }


async def _read_document(args: dict, ctx: ToolContext) -> dict:
    try:
        document_id = int(args.get("document_id"))
    except (TypeError, ValueError):
        return {"status": "error", "error": "document_id must be an integer"}
    page = args.get("page")
    line = args.get("line")
    try:
        page = int(page) if page is not None else None
        line = int(line) if line is not None else None
    except (TypeError, ValueError):
        return {"status": "error", "error": "page and line must be integers"}
    try:
        result = await read_document_page(
            ctx.user_id, document_id, page=page, line=line
        )
    except Exception as e:
        logger.bind(session_id=ctx.session_id, component="agent.tools").bind(
            event="tool.read_document_failed"
        ).error(f"document read failed: {type(e).__name__}")
        return {"status": "error", "error": "could not read the document"}
    if result == "not_found":
        return {"status": "not_found", "document_id": document_id}
    if isinstance(result, str):  # page_out_of_range:{total}
        total = result.rsplit(":", 1)[1]
        return {
            "status": "error",
            "error": (
                f"page or line out of range — this document has {total} "
                f"page(s); pass page in [1, {total}] or a line the document "
                "contains"
            ),
        }
    out = {
        "document_id": result.id,
        "title": result.title,
        "kind": result.kind,
        "format": result.format,
        "page": result.page,
        "total_pages": result.total_pages,
        "first_line": result.first_line,
        "last_line": result.last_line,
        "total_lines": result.total_lines,
        "char_count": result.char_count,
        "content": result.rendered,
        "note": (
            "Line-number prefixes and [line continues] markers are display "
            "decoration — the stored text has neither. When copying text "
            "for edit_document's old_str, copy it WITHOUT the 'N: ' prefix."
        ),
    }
    if result.total_pages > result.page:
        out["truncated"] = (
            f"further pages exist — pass page={result.page + 1} to continue"
        )
    return out


async def _search_documents(args: dict, ctx: ToolContext) -> dict:
    query = str(args.get("query", "")).strip()
    if not query:
        # FR-53.3: an empty query would ILIKE '%%' everything into a "match"
        # and divide the count primitive by zero
        return {"status": "error", "error": "search needs a term"}
    document_id = args.get("document_id")
    log = logger.bind(session_id=ctx.session_id, component="agent.tools")
    try:
        if document_id is not None:
            try:
                document_id = int(document_id)
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "error": "document_id must be an integer",
                }
            res = await search_document(ctx.user_id, document_id, query)
            if res is None:
                return {"status": "not_found", "document_id": document_id}
            return {
                "scope": "document",
                "document_id": document_id,
                "matches": [
                    {"line": m.line, "snippet": m.snippet} for m in res.matches
                ],
                "total_matches": res.total_matches,
                "exact_matches": res.exact_matches,
                "note": (
                    "total_matches is case-insensitive; exact_matches is "
                    "verbatim and is the ONLY count safe to pass as "
                    "expected_occurrences — and only when old_str is "
                    "byte-identical to this query."
                ),
            }
        res = await search_workspace(ctx.user_id, query)
    except Exception as e:
        log.bind(event="tool.search_documents_failed").error(
            f"search failed: {type(e).__name__}"
        )
        return {"status": "error", "error": "could not search the documents"}
    out = {
        "scope": "workspace",
        "results": [
            {
                "document_id": h.id,
                "title": h.title,
                "kind": h.kind,
                "format": h.format,
                "match": h.match,
                "title_matched": h.title_matched,
                "line": h.line,
                "snippet": h.snippet,
                "match_count": h.match_count,
            }
            for h in res.hits
        ],
        "documents_matched": res.documents_matched,
        "searched_documents": res.scanned,
    }
    notes = []
    if res.scan_truncated:
        notes.append(f"searched your {res.scanned} most recent documents only")
    if res.documents_matched > len(res.hits):
        notes.append(
            f"{res.documents_matched} documents matched but only "
            f"{len(res.hits)} are shown — tell the user the list is partial"
        )
    if notes:
        out["note"] = "; ".join(notes)
    return out


def _steer_from_diagnosis(diag: EditDiagnosis, document_id: int, old_str: str | None) -> dict:
    """The diagnostic's stated precedence, rendered as steering text —
    always the nearest actionable obstacle."""
    if diag.reason == "not_found":
        return {"status": "not_found", "document_id": document_id}
    if diag.reason == "not_editable":
        return {
            "status": "refused",
            "error": (
                "this document is a PDF (extracted text) and is read-only — "
                "read it and create a new document instead"
            ),
        }
    if diag.reason == "no_match":
        return {
            "status": "refused",
            "error": (
                "No replacement was performed: old_str "
                f"`{_echo(old_str or '')}` did not appear verbatim."
            ),
        }
    if diag.reason == "count_mismatch":
        lines = diag.match_lines or []
        shown = ", ".join(str(n) for n in lines[:_LINES_MAX])
        more = (
            f" and {diag.occurrences - len(lines[:_LINES_MAX])} more"
            if diag.occurrences and diag.occurrences > len(lines[:_LINES_MAX])
            else ""
        )
        where = f", at lines {shown}{more}" if shown else ""
        return {
            "status": "refused",
            "error": (
                f"Found {diag.occurrences} occurrences of old_str{where}. "
                "Provide more surrounding context, or pass replace_all with "
                "expected_occurrences. After widening old_str, re-search the "
                "widened string (one cheap call) or omit expected_occurrences "
                "and rely on this refusal."
            ),
        }
    if diag.reason == "bad_line":
        return {
            "status": "refused",
            "error": (
                f"Invalid `insert_line`. It should be within "
                f"[0, {diag.total_lines}]."
            ),
        }
    if diag.reason == "over_ceiling":
        return {
            "status": "refused",
            "error": (
                f"this document is at its size limit ({MAX_DOC_CHARS} "
                "characters) — create a new one"
            ),
        }
    return {"status": "error", "error": "the edit could not be saved"}


async def _edit_document(args: dict, ctx: ToolContext) -> dict:
    log = logger.bind(session_id=ctx.session_id, component="agent.tools")
    try:
        document_id = int(args.get("document_id"))
    except (TypeError, ValueError):
        return {"status": "error", "error": "document_id must be an integer"}
    mode = args.get("mode")
    if mode not in ("replace", "append", "str_replace", "insert"):
        return {
            "status": "error",
            "error": "mode must be 'replace', 'append', 'str_replace', or 'insert'",
        }

    kw = dict(ceiling=MAX_DOC_CHARS, turn_id=ctx.turn_id, session_id=ctx.session_id)
    row = None
    old_str: str | None = None
    try:
        if mode == "str_replace":
            old_str = args.get("old_str")
            if not isinstance(old_str, str) or old_str == "":
                return {"status": "error", "error": "old_str must be non-empty"}
            new_str = args.get("new_str")
            new_str = new_str if isinstance(new_str, str) else ""
            replace_all = bool(args.get("replace_all", False))
            expected = args.get("expected_occurrences")
            required = 1
            if replace_all:
                try:
                    expected = int(expected)
                except (TypeError, ValueError):
                    return {
                        "status": "error",
                        "error": (
                            "replace_all requires expected_occurrences (use "
                            "search_documents' exact_matches)"
                        ),
                    }
                if expected < 1:
                    # FR-53.4: 0 would pass a zero-count predicate as a
                    # success-reporting no-op write
                    return {
                        "status": "error",
                        "error": "expected_occurrences must be at least 1",
                    }
                required = expected
            row = await str_replace_document(
                ctx.user_id, document_id, old_str, new_str,
                replace_all=replace_all,
                expected_occurrences=expected if replace_all else None,
                **kw,
            )
            if row is None:
                diag = await diagnose_edit_failure(
                    ctx.user_id, document_id, ceiling=MAX_DOC_CHARS,
                    old_str=old_str, new_str=new_str, required=required,
                )
                return _steer_from_diagnosis(diag, document_id, old_str)

        elif mode == "insert":
            text_to_insert = args.get("text")
            if not isinstance(text_to_insert, str) or not text_to_insert:
                return {"status": "error", "error": "text is required for insert"}
            try:
                insert_line = int(args.get("insert_line"))
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "error": "insert_line must be an integer (0 inserts first)",
                }
            if insert_line < 0:
                return {
                    "status": "error",
                    "error": "insert_line must be >= 0",
                }
            row = await insert_document_line(
                ctx.user_id, document_id, insert_line, text_to_insert, **kw
            )
            if row is None:
                diag = await diagnose_edit_failure(
                    ctx.user_id, document_id, ceiling=MAX_DOC_CHARS,
                    insert_line=insert_line, insert_text=text_to_insert,
                )
                return _steer_from_diagnosis(diag, document_id, None)

        elif mode == "append":
            content = str(args.get("content", ""))
            if not content.strip():
                return {"status": "error", "error": "content is required"}
            row = await append_document_content(
                ctx.user_id, document_id, "\n" + content, **kw
            )
            if row is None:
                diag = await diagnose_edit_failure(
                    ctx.user_id, document_id, ceiling=MAX_DOC_CHARS,
                    insert_text=content,
                )
                return _steer_from_diagnosis(diag, document_id, None)

        else:  # replace
            content = str(args.get("content", ""))
            if not content.strip():
                return {"status": "error", "error": "content is required"}
            title = str(args.get("title", "")).strip() or None
            refusal = {
                "status": "refused",
                "error": (
                    "this document is longer than the readable window; "
                    "replace would delete content you have not seen — use "
                    "str_replace or append instead"
                ),
            }
            # pre-read for the FAST refusal; the cap is ENFORCED inside the
            # UPDATE's predicate (a detached FR-46 write can grow the row
            # between this read and the statement)
            current = await get_document_row(ctx.user_id, document_id)
            if current is None:
                return {"status": "not_found", "document_id": document_id}
            if len(current.content) > READ_DOCUMENT_MAX_CHARS:
                return refusal
            row = await replace_document_content(
                ctx.user_id, document_id, content, title,
                max_replaceable_chars=READ_DOCUMENT_MAX_CHARS, **kw,
            )
            if row is None:
                diag = await diagnose_edit_failure(
                    ctx.user_id, document_id, ceiling=MAX_DOC_CHARS,
                    replace_len=len(content),
                )
                if diag.reason == "unknown":
                    # gone or grown past the cap since the read
                    return refusal
                return _steer_from_diagnosis(diag, document_id, None)
    except Exception as e:
        # exception TYPE only — same NFR-9 rule as create
        log.bind(event="tool.edit_document_failed").error(
            f"document edit failed: {type(e).__name__}"
        )
        return {"status": "error", "error": "the edit could not be saved"}

    await ctx.emit(_panel_document(row, "document.updated"))
    # FR-53.8: reconcile EVERY live session's attach block (two tabs with
    # the same document attached must both drop the pre-edit text) — the
    # RETURNING content is in hand, so no hot-path DB read. Synchronous.
    notify_document_updated(ctx.user_id, row.id, row.title, row.content)
    log.bind(event="tool.edit_document", kind=row.kind, mode=mode).info(
        f"document {document_id} edited ({mode})"
    )
    return {
        "status": "edited",
        "document_id": row.id,
        "mode": mode,
        "title": row.title,
        "note": "The updated document is visible on the user's screen.",
    }


def build_registry() -> list[Tool]:
    """FR-53's inventory. Static per process — identity and turn travel
    in ToolContext at call time, not baked into handlers."""
    return [
        Tool(
            name="create_document",
            description=(
                "Write up a document of the conversation and put it on the "
                "user's screen: a structured summary, a list of action "
                "items, or a cleaned-up version of the user's idea. Use "
                "only when the user asks for a write-up."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short, human-readable title.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": list(DOCUMENT_KINDS),
                        "description": "What kind of write-up this is.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "The document body. Markdown with simple "
                            "structure; read on screen, not spoken."
                        ),
                    },
                },
                "required": ["title", "kind", "content"],
            },
            handler=_create_document,
            is_write=True,
        ),
        Tool(
            name="list_documents",
            description=(
                "List the user's recent documents — their uploads and your "
                "write-ups, including past sessions: id, title, source, "
                "kind, format, size, timestamps. Use when the user refers "
                "to an earlier document, to find its id. Pages of 20, "
                "newest activity first; pass page to reach older documents "
                "(total_pages comes back in the result)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "page": {
                        "type": "integer",
                        "description": "1-based page (default 1).",
                    }
                },
            },
            handler=_list_documents,
            is_write=False,
        ),
        Tool(
            name="read_document",
            description=(
                "Read one of the user's documents by id, one page at a "
                "time with line numbers. Pass page to continue a long "
                "document, or line to jump to the page carrying that line "
                "(e.g. from search_documents). Line-number prefixes are "
                "display only — never include them in edit_document's "
                "old_str."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "integer",
                        "description": "The document's id (from list_documents).",
                    },
                    "page": {
                        "type": "integer",
                        "description": "1-based page to read (default 1).",
                    },
                    "line": {
                        "type": "integer",
                        "description": (
                            "Jump to the first page carrying this line "
                            "(wins over page)."
                        ),
                    },
                },
                "required": ["document_id"],
            },
            handler=_read_document,
            is_write=False,
        ),
        Tool(
            name="search_documents",
            description=(
                "Find text in the user's documents — a literal substring "
                "match, case-insensitive, never a regex. Without "
                "document_id it searches titles and content across the "
                "workspace (one result per document; say so if the result "
                "notes the list is partial). With document_id it returns "
                "every match's line and snippet plus two counts: "
                "total_matches (case-insensitive) and exact_matches "
                "(verbatim) — exact_matches is the only number to pass as "
                "edit_document's expected_occurrences, and only when "
                "old_str is byte-identical to the query."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The literal text to find (non-empty).",
                    },
                    "document_id": {
                        "type": "integer",
                        "description": (
                            "Search inside this one document instead of "
                            "across the workspace."
                        ),
                    },
                },
                "required": ["query"],
            },
            handler=_search_documents,
            is_write=False,
        ),
        Tool(
            name="edit_document",
            description=(
                "Edit one of the user's documents (markdown and text only; "
                "PDFs are read-only). Modes: 'str_replace' replaces "
                "old_str (which must occur exactly once — or pass "
                "replace_all with expected_occurrences from "
                "search_documents' exact_matches) with new_str; 'insert' "
                "inserts text after line insert_line (0 = at the start); "
                "'append' adds to the end; 'replace' rewrites the whole "
                "content (short documents only — read it first). old_str "
                "matches the STORED text: never include the 'N: ' "
                "line-number prefixes from read_document."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "integer",
                        "description": "The document's id (from list_documents).",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["str_replace", "insert", "append", "replace"],
                        "description": "How to edit.",
                    },
                    "old_str": {
                        "type": "string",
                        "description": (
                            "str_replace: the exact text to replace, copied "
                            "verbatim from the stored content."
                        ),
                    },
                    "new_str": {
                        "type": "string",
                        "description": (
                            "str_replace: the replacement (empty deletes)."
                        ),
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": (
                            "str_replace: replace every occurrence; requires "
                            "expected_occurrences."
                        ),
                    },
                    "expected_occurrences": {
                        "type": "integer",
                        "description": (
                            "str_replace with replace_all: how many "
                            "occurrences you expect (>= 1); the edit refuses "
                            "if the actual count differs."
                        ),
                    },
                    "insert_line": {
                        "type": "integer",
                        "description": (
                            "insert: the line to insert after (0 = before "
                            "line 1)."
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": "insert: the text to insert.",
                    },
                    "content": {
                        "type": "string",
                        "description": "append/replace: the content.",
                    },
                    "title": {
                        "type": "string",
                        "description": "replace: optional new title.",
                    },
                },
                "required": ["document_id", "mode"],
            },
            handler=_edit_document,
            is_write=True,
        ),
    ]


def registry_schemas(registry: list[Tool]) -> list[dict]:
    """The declarations agent/providers.py translates for the LLM call."""
    return [
        {"name": t.name, "description": t.description, "parameters": t.parameters}
        for t in registry
    ]
