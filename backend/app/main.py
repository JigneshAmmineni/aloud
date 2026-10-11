"""FastAPI app: signaling (/start, offers), /documents, /healthz, admin API.

Every route except /healthz resolves a verified identity through the auth
seam (REQUIREMENTS.md §4.8 / FR-23); /healthz stays open — it's an
infrastructure liveness probe carrying no user data.

Signaling follows the Pipecat client contract:
  POST  /start      — session bootstrap (returns sessionId + optional ICE config)
  POST  /sessions/{id}/api/offer  — SDP offer/answer (handles renegotiation)
  PATCH /sessions/{id}/api/offer  — trickle ICE candidates
Only the session-scoped paths exist: every offer is bound to a session the
verified user owns (the sessionless /api/offer variant was removed with the
prebuilt debug client — it had no ownership to enforce).

Session-establishment endpoints verify with check_revoked=True (FR-29), so a
disabled account cannot open or complete a new session.
"""

import asyncio
import json
import signal
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from obs.logging import setup_logging

setup_logging()  # before anything logs — every line on stdout is JSON

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Path,
    Request,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from loguru import logger
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from agent.companion import CompanionAgent, drain_live_sessions
from agent.registry import notify_document_deleted, notify_document_updated
from app import admin, auth
from app.auth import AuthedUser, get_current_user_checked, get_current_user_id
from app.config import load_settings
from app.documents import (
    EXTRACT_SEMAPHORE,
    MAX_DOC_CHARS,
    MAX_FILE_BYTES,
    MAX_TOTAL_CHARS,
    DocumentError,
    extract_text,
)
from app.ratelimit import (
    DOCUMENT_SAVE_RATE_LIMIT,
    DOCUMENTS_RATE_LIMIT,
    rate_limited,
    user_rate_limited,
)
from db.documents_repo import (
    MAX_DOCUMENTS_PER_USER,
    DocumentQuotaError,
    delete_document_row,
    get_document_row,
    insert_upload_row,
    list_workspace_rows,
    resolve_attach_rows,
    save_document_content,
)
from db.engine import init_db, retire_bootstrap_engine
from db.migrations import migrate_artifacts_to_documents
from db.sessions_repo import session_is_active, sweep_orphaned_sessions
from db.users_repo import provision_user

settings = load_settings()  # fail fast at boot, naming any missing env vars


def _install_sigterm_goodbye() -> None:
    """Graceful-shutdown goodbye. Uvicorn's own SIGTERM handling cancels the
    agent background tasks before lifespan shutdown runs, so a goodbye there
    would be too late. Instead: intercept SIGTERM, tell live sessions the
    server is going away (drain_live_sessions), then hand control back to
    uvicorn's untouched SIGINT handler for its normal graceful exit."""
    loop = asyncio.get_running_loop()
    # asyncio holds tasks by WEAK reference — without a hard one the drain
    # task could be garbage-collected mid-await, and the SIGINT handoff
    # would never fire (the process would sit deaf until SIGKILL).
    retained: list = []

    def _on_sigterm():
        async def _drain_then_exit():
            # The handoff to uvicorn's shutdown must happen no matter what —
            # a raising drain would otherwise leave the process deaf to
            # SIGTERM until Docker SIGKILLs it, the exact hard death the
            # drain exists to avoid.
            try:
                await drain_live_sessions()
            except Exception:
                logger.bind(component="app.main", event="session.drain_failed").exception(
                    "drain failed; shutting down anyway"
                )
            finally:
                signal.raise_signal(signal.SIGINT)

        retained.append(asyncio.ensure_future(_drain_then_exit()))

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    except (NotImplementedError, RuntimeError):
        pass  # non-unix host (local Windows runs); deploys are Linux containers


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.configure(settings.firebase_service_account_path)
    await init_db(settings.database_url)
    # FR-50: run-once artifacts→documents copy, pre-serve in the
    # RLS-bootstrap slot — before the sweep so a failed copy fails boot
    # before anything else writes.
    await migrate_artifacts_to_documents()
    # FR-32 boot sweep: close sessions orphaned by the previous process's
    # death and emit their inferred STT usage — before serving traffic.
    await sweep_orphaned_sessions()
    # The sweep was the bootstrap engine's last job: retire the RLS-exempt
    # escape hatch so any later use fails loudly instead of silently
    # bypassing every policy.
    await retire_bootstrap_engine()
    _install_sigterm_goodbye()
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(admin.router)

ICE_SERVERS = [IceServer(urls=["stun:stun.l.google.com:19302"])]

webrtc_handler = SmallWebRTCRequestHandler(ice_servers=ICE_SERVERS)

# Sessions minted by /start; cleared on process restart. Each maps the
# unguessable session id -> {"user_id": verified uid, "body": start payload,
# "created_at": wall time}. Entries are purged after a TTL regardless of
# consumption, bounding growth. Accepted consequence: a session older than
# the TTL that needs to renegotiate (ICE restart) gets a 404 and the user
# re-taps Talk — the same UX as the descoped session-resume (FR-19), and the
# baked-in Bearer token would have expired by then anyway.
active_sessions: dict[str, dict] = {}
SESSION_ENTRY_TTL_S = 3600


def _purge_stale_sessions() -> None:
    cutoff = time.time() - SESSION_ENTRY_TTL_S
    for sid in [s for s, v in active_sessions.items() if v["created_at"] < cutoff]:
        del active_sessions[sid]


def _owned_session(session_id: str, user_id: str) -> dict:
    """The session must exist and belong to the requesting user — a valid
    token for user B must never operate user A's session."""
    session = active_sessions.get(session_id)
    if session is None or session["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Invalid or not-yet-ready session_id")
    return session


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post(
    "/api/auth/email-check",
    dependencies=[Depends(rate_limited(max_requests=5, window_s=60.0))],
)
async def email_check(request: Request):
    """Signup pre-check (FR-30): is this email already registered?

    Unauthenticated by necessity (the caller has no account yet) — which is
    why it's rate-limited: it's a deliberate, bounded enumeration exception
    (FR-26; signup's email-already-in-use reveals the same fact anyway).
    Path lives under /api/ so Caddy routes it to the backend."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        # A valid-JSON non-object body ([], "x", 42, null) must be a clean
        # 400 on this public route, never an AttributeError 500.
        body = {}
    email = str(body.get("email", "")).strip()
    if not email:
        raise HTTPException(status_code=400, detail="email is required")
    try:
        registered = await run_in_threadpool(auth.email_exists, email)
    except Exception as e:
        # Transient Firebase failure on an unauthenticated route: a clean
        # retryable signal, not a 500 — with the cause distinguishable in
        # the server-side log.
        logger.bind(
            component="app.auth",
            event="auth.email_check_failed",
            error_type=type(e).__name__,
        ).warning("email pre-check failed upstream")
        raise HTTPException(status_code=503, detail="Try again shortly")
    logger.bind(
        component="app.auth", event="auth.email_check", registered=registered
    ).info("signup availability pre-check")
    return {"registered": registered}


# FR-56: 2 MiB comfortably covers a MAX_DOC_CHARS document even in 4-byte
# UTF-8 plus JSON escaping and the title. A legacy over-ceiling row larger
# than this 413s and cannot be saved through this route — accepted: the
# growth gate exists because over-ceiling rows exist, but one past ~500k
# chars is not plausible. An unbounded request.json() would materialize an
# authed 1 GB POST the same way the upload route's bounded read prevents.
MAX_SAVE_BODY_BYTES = 2 * 1024 * 1024

# documents.id is a SQLAlchemy Integer -> int32; a value beyond it is an
# unencodable bind -> 500 where FR-56's precedence says not-found (the
# same class FR-59 bounded for offset). Annotated, NOT a shared Path()
# default: FastAPI writes back onto a FieldInfo handed in as a default,
# so one instance shared across routes is a latent footgun (round 6).
INT32_MAX = 2_147_483_647
DocId = Annotated[int, Path(ge=1, le=INT32_MAX)]


async def _bounded_json(
    request: Request, cap: int
) -> tuple[dict | None, str | None]:
    """Read at most cap+1 bytes of body and parse JSON. Returns
    (body, None) or (None, "too_large" | "malformed") — a typed refusal
    like EditDiagnosis/SaveOutcome, not an in-band sentinel key a client
    body could legitimately contain (round 6)."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            return None, "too_large"
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
    except Exception:
        return None, "malformed"
    return (body, None) if isinstance(body, dict) else (None, "malformed")


# FR-51: per-USER limiter, declared with the route's own auth callable so
# FastAPI's per-request dependency cache resolves identity once.
_documents_limiter = user_rate_limited(
    DOCUMENTS_RATE_LIMIT, 60.0, get_current_user_checked
)


@app.post("/documents", dependencies=[Depends(_documents_limiter)])
async def upload_document(
    file: UploadFile = File(...),
    user: AuthedUser = Depends(get_current_user_checked),
):
    """Accept a .txt/.md/.pdf upload, extract its text, and persist it as a
    documents row (FR-51). Uploads precede /start, so this endpoint
    provisions too (FR-24) — without it, documents.user_id's FK makes a new
    account's first action a foreign-key 500 — and because it provisions and
    writes persistent rows it verifies with check_revoked (FR-29)."""
    await provision_user(user.user_id, user.name)
    # Bounded read: never materialize more than the cap + 1 byte — an
    # unbounded read() of an authed 1 GB POST would take a 2 GB VM (and
    # every live session) with it. One extra byte lets extract_text's
    # size check fire with its normal message.
    data = await file.read(MAX_FILE_BYTES + 1)
    filename = file.filename or "document"
    try:
        # Threadpool: pypdf is synchronous CPU work that must not occupy the
        # event loop driving live pipelines; the semaphore keeps permitted
        # uploads from parsing concurrently on 2 shared vCPUs (FR-51).
        async with EXTRACT_SEMAPHORE:
            text, format = await run_in_threadpool(
                extract_text, filename, file.content_type, data
            )
    except DocumentError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        doc = await insert_upload_row(user.user_id, filename, format, text)
    except DocumentQuotaError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # No filename in the log: it's user-supplied text describing private
    # material, and INFO lines ship to Cloud Logging (FR-39/NFR-9) — the
    # same rule that keeps artifact titles out of agent/tools.py's log.
    logger.bind(
        component="app.documents",
        event="document.uploaded",
        char_count=len(text),
    ).info(f"Document uploaded ({len(text)} chars)")
    return {
        "id": doc.id,
        "title": doc.title,
        "format": doc.format,
        "char_count": len(text),
    }


@app.get("/documents")
async def list_documents_route(
    offset: int = 0,
    user_id: str = Depends(get_current_user_id),
):
    """FR-52: the user's workspace — both sources, newest-activity-first,
    metadata plus title and char_count, never content, with a total and a
    minimal offset continuation (the repo clamps offset)."""
    items, total = await list_workspace_rows(user_id, offset=offset)
    return {"total": total, "documents": items}


@app.get("/documents/{document_id}")
async def get_document_route(
    document_id: DocId,
    user_id: str = Depends(get_current_user_id),
):
    """FR-52: one owned document with full content — preview, download, and
    FR-55's oversized-announce refetch. Another user's id is not-found."""
    doc = await get_document_row(user_id, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    return {
        "id": doc.id,
        "source": doc.source,
        "format": doc.format,
        "kind": doc.kind,
        "title": doc.title,
        "content": doc.content,
        "char_count": len(doc.content),
        "created_at": doc.created_at,
        "updated_at": doc.updated_at,
    }


# §4.12 FR-56: the editor's autosave target. TITLE_MAX_CHARS caps a
# rename — uploads are bounded incidentally by filenames, a rename by
# nothing. Verification is local (get_current_user_id), a stated choice:
# a save writes only rows the account already owns, quota-bounded — a
# disabled account can overwrite its own documents for its residual token
# hour exactly as its live session's edit tools allow, and cannot grow
# anything (FR-29 deliberately unchanged).
TITLE_MAX_CHARS = 200  # mirrored in frontend/lib/workspace.ts — change BOTH

_saves_limiter = user_rate_limited(
    DOCUMENT_SAVE_RATE_LIMIT, 60.0, get_current_user_id
)


@app.put("/documents/{document_id}", dependencies=[Depends(_saves_limiter)])
async def save_document_route(
    request: Request,
    document_id: DocId,
    user_id: str = Depends(get_current_user_id),
):
    """FR-56: save (and optionally rename) a document — the NULL-safe CAS
    is the concurrency seam the whole §4.12 editor rides. The base token
    is OPAQUE to the client (echoed byte-for-byte); the server parses it
    at full precision."""
    body, body_err = await _bounded_json(request, MAX_SAVE_BODY_BYTES)
    if body_err == "too_large":
        raise HTTPException(
            status_code=413,
            detail=f"save body too large (limit {MAX_SAVE_BODY_BYTES} bytes)",
        )
    if body is None:
        # distinct from the content-validation 400: the client treats
        # validation text literally when classifying failures
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    if not isinstance(body.get("content"), str):
        raise HTTPException(status_code=400, detail="content (string) is required")
    content = body["content"]

    title = body.get("title")
    if title is not None:
        # code, not prose: the client classifies this as the ONE
        # recoverable 4xx (revert the rename, keep saving the content) —
        # branching on the detail text would make a copy edit data loss
        if not isinstance(title, str) or not title.strip():
            # the one field FR-57.1 leaves on screen must identify the row
            return JSONResponse(
                status_code=400,
                content={"detail": "title can't be empty", "code": "title_invalid"},
            )
        if len(title) > TITLE_MAX_CHARS:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": f"title is too long (limit {TITLE_MAX_CHARS} characters)",
                    "code": "title_invalid",
                },
            )

    raw_base = body.get("base_updated_at")
    base = None
    if raw_base is not None:
        try:
            base = datetime.fromisoformat(raw_base)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=(
                    "base_updated_at must be the exact updated_at string "
                    "from the last response"
                ),
            )

    outcome = await save_document_content(
        user_id,
        document_id,
        content,
        base_updated_at=base,
        title=title,
        ceiling=MAX_DOC_CHARS,
    )
    if outcome.reason == "not_found":
        raise HTTPException(status_code=404, detail="document not found")
    if outcome.reason == "not_editable":
        raise HTTPException(
            status_code=400,
            detail="this document is a PDF (extracted text) and is read-only",
        )
    if outcome.reason == "over_ceiling":
        raise HTTPException(
            status_code=400,
            detail=f"this document is at its size limit ({MAX_DOC_CHARS} characters)",
        )
    if outcome.reason == "stale":
        cur = outcome.current
        return JSONResponse(
            status_code=409,
            content={
                "content": cur.content,
                "updated_at": cur.updated_at.isoformat() if cur.updated_at else None,
                "title": cur.title,
                "char_count": cur.char_count,
            },
        )
    row = outcome.row
    # live attach blocks reconcile exactly like agent edits — content AND
    # title (FR-53.8's header names the file)
    notify_document_updated(user_id, row.id, row.title, row.content)
    logger.bind(
        component="app.documents",
        event="document.saved",
        document_id=row.id,
        char_count=len(row.content),
    ).info(f"document saved ({len(row.content)} chars)")
    return {
        "updated_at": row.updated_at.isoformat(),
        "char_count": len(row.content),
    }


@app.delete("/documents/{document_id}")
async def delete_document_route(
    document_id: DocId,
    user_id: str = Depends(get_current_user_id),
):
    """FR-52: hard delete behind the client's confirming affordance; the
    repo removes the paired legacy row in the same transaction and logs the
    structured event. Live sessions of this user reconcile through the
    registry — the block must stop shipping destroyed 🔒 content."""
    if not await delete_document_row(user_id, document_id):
        raise HTTPException(status_code=404, detail="document not found")
    notify_document_deleted(user_id, document_id)
    return {"deleted": True}


@app.post("/start")
async def start(
    request: Request,
    user: AuthedUser = Depends(get_current_user_checked),
):
    """Session bootstrap. Provisions the users row (FR-24: /start is the
    entry point to user-owned rows) and binds the minted session id to the
    verified user."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    await provision_user(user.user_id, user.name)
    _purge_stale_sessions()
    session_id = str(uuid.uuid4())
    active_sessions[session_id] = {
        "user_id": user.user_id,
        "body": body.get("body", {}),
        "created_at": time.time(),
    }
    result: dict = {"sessionId": session_id}
    if body.get("enableDefaultIceServers"):
        result["iceConfig"] = {
            "iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]
        }
    return result


async def _handle_offer(
    request: SmallWebRTCRequest,
    background_tasks: BackgroundTasks,
    user_id: str,
    session_id: str,
    documents=None,
):
    async def webrtc_connection_callback(connection: SmallWebRTCConnection):
        logger.bind(
            session_id=session_id,
            pc_id=connection.pc_id,
            component="app.signaling",
            event="signaling.offer",
            document_count=len(documents) if documents else 0,
        ).info("New WebRTC connection; launching agent")
        background_tasks.add_task(
            CompanionAgent(
                settings, documents, user_id=user_id, session_id=session_id
            ).run,
            connection,
        )

    return await webrtc_handler.handle_web_request(
        request=request,
        webrtc_connection_callback=webrtc_connection_callback,
    )


@app.get("/sessions/{session_id}/alive")
async def session_alive(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Session-scoped liveness for the client's while-active poll: the DB
    row is the shared truth, so this answers correctly for every way a
    session dies (crash → this request itself fails; restart → boot sweep
    closed the row; media timeout → the pipeline closed it) and would keep
    working unchanged behind a load balancer."""
    return {"alive": await session_is_active(session_id, user_id)}


def _coerce_attach_ids(raw) -> list[int]:
    """FR-51's defensive coercion, in one place: the id space is integer row
    ids now, and a tab left open across the deploy sends the old UUID shape —
    a non-integer id is silently skipped exactly like an unknown one, never
    handed to the DB to raise a type error out of session establishment.
    Deduped (first occurrence wins — duplicates would double-count the
    budget and collide in the id-keyed per-document sections) and capped
    (a list-length bound; 50,000 ids have no business reaching the lengths
    query)."""
    ids: list[int] = []
    seen: set[int] = set()
    for value in raw if isinstance(raw, list) else []:
        # FR-59: a NON-INTEGRAL id is skipped like an unknown one — int()
        # would truncate 14.9 onto a NEIGHBORING document's id
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            doc_id = value
        elif isinstance(value, float) and value.is_integer():
            doc_id = int(value)
        elif isinstance(value, str):
            try:
                doc_id = int(value)
            except ValueError:
                continue
        else:
            continue
        if not (1 <= doc_id <= INT32_MAX):
            # the offset/path-id bound, applied to the attach list too:
            # 10**19 against an int4 column is an asyncpg DataError that
            # 500s session establishment
            continue
        if doc_id in seen:
            continue
        seen.add(doc_id)
        ids.append(doc_id)
        if len(ids) >= MAX_DOCUMENTS_PER_USER:
            break
    return ids


@app.post("/sessions/{session_id}/api/offer")
async def session_offer(
    session_id: str,
    request: SmallWebRTCRequest,
    background_tasks: BackgroundTasks,
    user: AuthedUser = Depends(get_current_user_checked),
):
    session = _owned_session(session_id, user.user_id)
    document_ids = _coerce_attach_ids(session["body"].get("document_ids") or [])
    # Bounded in the repo (FR-51): lengths-first, budget-walked, boundary
    # row sliced SQL-side — never fetch-everything-then-cut.
    documents = await resolve_attach_rows(
        user.user_id, document_ids, MAX_TOTAL_CHARS
    )
    return await _handle_offer(
        request, background_tasks, user.user_id, session_id, documents
    )


@app.patch("/sessions/{session_id}/api/offer")
async def session_ice_candidate(
    session_id: str,
    request: SmallWebRTCPatchRequest,
    user: AuthedUser = Depends(get_current_user_checked),
):
    _owned_session(session_id, user.user_id)
    await webrtc_handler.handle_patch_request(request)
    return {"status": "success"}
