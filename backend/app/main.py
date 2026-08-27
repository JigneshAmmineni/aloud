"""FastAPI app: signaling (/start, offers), /documents, /healthz, admin API.

Every route except /healthz resolves a verified identity through the auth
seam (REQUIREMENTS.md §4.8 / FR-23); /healthz stays open — it's an
infrastructure liveness probe carrying no user data.

Signaling follows the Pipecat client contract:
  POST  /start      — session bootstrap (returns sessionId + optional ICE config)
  POST  /api/offer  — SDP offer/answer (SmallWebRTCRequestHandler, handles renegotiation)
  PATCH /api/offer  — trickle ICE candidates
  /sessions/{id}/api/offer — same, on the session-scoped path Pipecat clients
                             use after /start (mirrors Pipecat Cloud's proxy)

Session-establishment endpoints verify with check_revoked=True (FR-29), so a
disabled account cannot open or complete a new session.
"""

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
from loguru import logger
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from agent.companion import CompanionAgent
from app import admin
from app.auth import AuthedUser, get_current_user, get_current_user_checked
from app.config import load_settings
from app.documents import DocumentError, document_store, extract_text
from db.engine import init_db
from db.users_repo import provision_user

settings = load_settings()  # fail fast at boot, naming any missing env vars


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db(settings.database_url)
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(admin.router)

ICE_SERVERS = [IceServer(urls=["stun:stun.l.google.com:19302"])]

webrtc_handler = SmallWebRTCRequestHandler(ice_servers=ICE_SERVERS)

# Sessions minted by /start; cleared on process restart. Each maps the
# unguessable session id -> {"user_id": verified uid, "body": start payload}.
active_sessions: dict[str, dict] = {}


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


@app.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    user: AuthedUser = Depends(get_current_user),
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
    doc = document_store.add(user.user_id, filename, file.content_type or "", text)
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
    session_id = str(uuid.uuid4())
    active_sessions[session_id] = {
        "user_id": user.user_id,
        "body": body.get("body", {}),
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


@app.post("/api/offer")
async def offer(
    request: SmallWebRTCRequest,
    background_tasks: BackgroundTasks,
    user: AuthedUser = Depends(get_current_user_checked),
):
    return await _handle_offer(request, background_tasks, user.user_id)


@app.patch("/api/offer")
async def ice_candidate(
    request: SmallWebRTCPatchRequest,
    user: AuthedUser = Depends(get_current_user_checked),
):
    await webrtc_handler.handle_patch_request(request)
    return {"status": "success"}


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
