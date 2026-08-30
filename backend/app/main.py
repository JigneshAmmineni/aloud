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
import signal
import time
import uuid
from contextlib import asynccontextmanager

from obs.logging import setup_logging

setup_logging()  # before anything logs — every line on stdout is JSON

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from loguru import logger
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from agent.companion import CompanionAgent, drain_live_sessions
from app import admin, auth
from app.auth import AuthedUser, get_current_user_checked, get_current_user_id
from app.config import load_settings
from app.documents import DocumentError, document_store, extract_text
from app.ratelimit import rate_limited
from db.engine import init_db
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

    def _on_sigterm():
        async def _drain_then_exit():
            await drain_live_sessions()
            signal.raise_signal(signal.SIGINT)

        asyncio.ensure_future(_drain_then_exit())

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    except (NotImplementedError, RuntimeError):
        pass  # non-unix host (local Windows runs); deploys are Linux containers


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.configure(settings.firebase_service_account_path)
    await init_db(settings.database_url)
    # FR-32 boot sweep: close sessions orphaned by the previous process's
    # death and emit their inferred STT usage — before serving traffic.
    await sweep_orphaned_sessions()
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


@app.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
):
    """Accept a .txt/.md/.pdf upload, extract its text, and stash it in the
    ephemeral store under the requesting user. Returns metadata (id + char
    count); the chosen ids are later passed in the /start body."""
    data = await file.read()
    filename = file.filename or "document"
    try:
        text = extract_text(filename, file.content_type, data)
    except DocumentError as e:
        raise HTTPException(status_code=400, detail=str(e))
    doc = document_store.add(user_id, filename, file.content_type or "", text)
    logger.bind(
        component="app.documents",
        event="document.uploaded",
        char_count=doc.char_count,
    ).info(f"Document uploaded: {filename!r}")
    return {
        "id": doc.id,
        "filename": doc.filename,
        "mime_type": doc.mime_type,
        "char_count": doc.char_count,
    }


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
    documents=None,
):
    async def webrtc_connection_callback(connection: SmallWebRTCConnection):
        logger.bind(
            session_id=connection.pc_id,
            component="app.signaling",
            event="signaling.offer",
            document_count=len(documents) if documents else 0,
        ).info("New WebRTC connection; launching agent")
        background_tasks.add_task(
            CompanionAgent(settings, documents, user_id=user_id).run, connection
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


@app.post("/sessions/{session_id}/api/offer")
async def session_offer(
    session_id: str,
    request: SmallWebRTCRequest,
    background_tasks: BackgroundTasks,
    user: AuthedUser = Depends(get_current_user_checked),
):
    session = _owned_session(session_id, user.user_id)
    document_ids = session["body"].get("document_ids") or []
    documents = document_store.get(user.user_id, document_ids)
    return await _handle_offer(request, background_tasks, user.user_id, documents)


@app.patch("/sessions/{session_id}/api/offer")
async def session_ice_candidate(
    session_id: str,
    request: SmallWebRTCPatchRequest,
    user: AuthedUser = Depends(get_current_user_checked),
):
    _owned_session(session_id, user.user_id)
    await webrtc_handler.handle_patch_request(request)
    return {"status": "success"}
