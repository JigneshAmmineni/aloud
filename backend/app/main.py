"""FastAPI app: signaling (/start, /api/offer), /healthz, and the prebuilt debug client.

Step 1 skeleton (SDD §10): every connect creates a new session; the Pipecat
prebuilt UI at /client stands in for the real frontend until build step 2.

Signaling follows the Pipecat client contract:
  POST  /start      — session bootstrap (returns sessionId + optional ICE config)
  POST  /api/offer  — SDP offer/answer (SmallWebRTCRequestHandler, handles renegotiation)
  PATCH /api/offer  — trickle ICE candidates
  /sessions/{id}/api/offer — same, on the session-scoped path Pipecat clients
                             use after /start (mirrors Pipecat Cloud's proxy)
"""

import uuid
from contextlib import asynccontextmanager

from obs.logging import setup_logging

setup_logging()  # before anything logs — every line on stdout is JSON

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from loguru import logger
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat_ai_small_webrtc_prebuilt.frontend import SmallWebRTCPrebuiltUI

from agent.companion import CompanionAgent
from app.config import load_settings
from db.engine import init_db

settings = load_settings()  # fail fast at boot, naming any missing env vars


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db(settings.database_url)
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/client", SmallWebRTCPrebuiltUI)

ICE_SERVERS = [IceServer(urls=["stun:stun.l.google.com:19302"])]

webrtc_handler = SmallWebRTCRequestHandler(ice_servers=ICE_SERVERS)

# Sessions minted by /start; cleared on process restart (fine for step 1).
active_sessions: dict[str, dict] = {}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/start")
async def start(request: Request):
    """Session bootstrap used by Pipecat clients (incl. the prebuilt UI)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = str(uuid.uuid4())
    active_sessions[session_id] = body.get("body", {})
    result: dict = {"sessionId": session_id}
    if body.get("enableDefaultIceServers"):
        result["iceConfig"] = {
            "iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]
        }
    return result


async def _handle_offer(request: SmallWebRTCRequest, background_tasks: BackgroundTasks):
    async def webrtc_connection_callback(connection: SmallWebRTCConnection):
        logger.bind(
            session_id=connection.pc_id,
            component="app.signaling",
            event="signaling.offer",
        ).info("New WebRTC connection; launching agent")
        background_tasks.add_task(CompanionAgent(settings).run, connection)

    return await webrtc_handler.handle_web_request(
        request=request,
        webrtc_connection_callback=webrtc_connection_callback,
    )


@app.post("/api/offer")
async def offer(request: SmallWebRTCRequest, background_tasks: BackgroundTasks):
    return await _handle_offer(request, background_tasks)


@app.patch("/api/offer")
async def ice_candidate(request: SmallWebRTCPatchRequest):
    await webrtc_handler.handle_patch_request(request)
    return {"status": "success"}


@app.post("/sessions/{session_id}/api/offer")
async def session_offer(
    session_id: str, request: SmallWebRTCRequest, background_tasks: BackgroundTasks
):
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Invalid or not-yet-ready session_id")
    return await _handle_offer(request, background_tasks)


@app.patch("/sessions/{session_id}/api/offer")
async def session_ice_candidate(session_id: str, request: SmallWebRTCPatchRequest):
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Invalid or not-yet-ready session_id")
    await webrtc_handler.handle_patch_request(request)
    return {"status": "success"}
