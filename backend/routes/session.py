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
    except Exception as e:
        print(f"[session_endpoint] unhandled error: {type(e).__name__}: {e}")
