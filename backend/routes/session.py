from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from agent.companion import CompanionAgent

router = APIRouter()


@router.websocket("/ws/session")
async def session_endpoint(websocket: WebSocket):
    await websocket.accept()
    agent = CompanionAgent()
    try:
        await agent.run_session(websocket)
    except WebSocketDisconnect:
        pass
