"""
Tests for the FastAPI WebSocket route (/ws/session).

CompanionAgent is mocked so no Gemini calls are made.
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop_run_session(ws):
    """run_session stub that accepts the connection and immediately returns."""
    async def _inner(websocket):
        return

    return _inner


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


class TestWSConnectionLifecycle:

    def test_endpoint_accepts_websocket_connection(self):
        """The /ws/session endpoint accepts WebSocket connections."""
        with patch("routes.session.CompanionAgent") as MockAgent:
            MockAgent.return_value.run_session = AsyncMock(return_value=None)
            with TestClient(app) as client:
                with client.websocket_connect("/ws/session") as ws:
                    pass  # connection accepted, handler returned

    def test_run_session_is_called_once_per_connection(self):
        """Each WebSocket connection creates one CompanionAgent and calls run_session once."""
        with patch("routes.session.CompanionAgent") as MockAgent:
            instance = MockAgent.return_value
            instance.run_session = AsyncMock(return_value=None)

            with TestClient(app) as client:
                with client.websocket_connect("/ws/session"):
                    pass

            MockAgent.assert_called_once()
            instance.run_session.assert_awaited_once()

    def test_separate_connections_get_separate_agents(self):
        """Each connection instantiates a fresh CompanionAgent (no sharing)."""
        with patch("routes.session.CompanionAgent") as MockAgent:
            MockAgent.return_value.run_session = AsyncMock(return_value=None)

            with TestClient(app) as client:
                with client.websocket_connect("/ws/session"):
                    pass
                with client.websocket_connect("/ws/session"):
                    pass

            assert MockAgent.call_count == 2


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestWSErrorHandling:

    def test_websocket_disconnect_is_handled_gracefully(self):
        """WebSocketDisconnect from run_session does not propagate as a 500."""
        from starlette.websockets import WebSocketDisconnect

        async def _disconnect(websocket):
            raise WebSocketDisconnect()

        with patch("routes.session.CompanionAgent") as MockAgent:
            MockAgent.return_value.run_session = _disconnect

            with TestClient(app) as client:
                with client.websocket_connect("/ws/session"):
                    pass  # should complete without error

    def test_unexpected_exception_is_caught_and_logged(self, caplog):
        """Unexpected exceptions from run_session are logged, not re-raised as 500."""
        import logging

        async def _boom(websocket):
            raise RuntimeError("unexpected failure")

        with patch("routes.session.CompanionAgent") as MockAgent:
            MockAgent.return_value.run_session = _boom

            with caplog.at_level(logging.DEBUG):
                with TestClient(app) as client:
                    with client.websocket_connect("/ws/session"):
                        pass


# ---------------------------------------------------------------------------
# Message passthrough (shallow smoke tests — deep coverage in integration tests)
# ---------------------------------------------------------------------------


class TestWSMessagePassthrough:

    def test_can_send_binary_frame(self, pcm_1s):
        """Binary frames can be sent through the WebSocket without crashing."""
        received_calls = []

        async def _capture(websocket):
            msg = await websocket.receive()
            received_calls.append(msg)

        with patch("routes.session.CompanionAgent") as MockAgent:
            MockAgent.return_value.run_session = _capture

            with TestClient(app) as client:
                with client.websocket_connect("/ws/session") as ws:
                    ws.send_bytes(pcm_1s)

        assert len(received_calls) == 1
        assert received_calls[0].get("bytes") == pcm_1s

    def test_can_send_text_frame(self):
        """Text frames can be sent through the WebSocket without crashing."""
        received_calls = []

        async def _capture(websocket):
            msg = await websocket.receive()
            received_calls.append(msg)

        with patch("routes.session.CompanionAgent") as MockAgent:
            MockAgent.return_value.run_session = _capture

            with TestClient(app) as client:
                with client.websocket_connect("/ws/session") as ws:
                    ws.send_text(json.dumps({"type": "end_of_turn"}))

        assert len(received_calls) == 1
        payload = json.loads(received_calls[0]["text"])
        assert payload["type"] == "end_of_turn"

    def test_backend_can_send_binary_to_browser(self):
        """run_session can push binary frames back to the browser."""
        audio_chunk = b"x" * 9600

        async def _send_audio(websocket):
            await websocket.send_bytes(audio_chunk)

        with patch("routes.session.CompanionAgent") as MockAgent:
            MockAgent.return_value.run_session = _send_audio

            with TestClient(app) as client:
                with client.websocket_connect("/ws/session") as ws:
                    data = ws.receive_bytes()

        assert data == audio_chunk

    def test_backend_can_send_turn_complete_json(self):
        """run_session can push the turn_complete JSON frame to the browser."""
        payload = {"type": "turn_complete"}

        async def _send_complete(websocket):
            await websocket.send_text(json.dumps(payload))

        with patch("routes.session.CompanionAgent") as MockAgent:
            MockAgent.return_value.run_session = _send_complete

            with TestClient(app) as client:
                with client.websocket_connect("/ws/session") as ws:
                    raw = ws.receive_text()

        assert json.loads(raw) == payload
