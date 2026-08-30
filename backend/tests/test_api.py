"""Signaling API contracts: /start, offer routes, /documents, ownership.

WebRTC handling itself is stubbed — these tests pin the HTTP contract that
the frontend and the Pipecat client SDK depend on. Identity comes from the
conftest `auth_as` override (the documented FastAPI seam); the raw
token-verification paths are covered in test_auth.py.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

import app.main as main


class StubWebRTCHandler:
    def __init__(self):
        self.web_requests = []
        self.patch_requests = []

    async def handle_web_request(self, request, webrtc_connection_callback):
        self.web_requests.append(request)
        return {"sdp": "answer-sdp", "type": "answer", "pc_id": "pc-test-1"}

    async def handle_patch_request(self, request):
        self.patch_requests.append(request)


@pytest.fixture
def stub_handler(monkeypatch):
    stub = StubWebRTCHandler()
    monkeypatch.setattr(main, "webrtc_handler", stub)
    return stub


@pytest.fixture
def client():
    with TestClient(main.app) as c:  # runs lifespan → init_db on sqlite
        yield c


def test_healthz_is_open(client):
    """The one unauthenticated route: an infra liveness probe, no user data."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_start_returns_session_id_and_registers_it(client, auth_as):
    auth_as("uid-a")
    resp = client.post("/start", json={"transport": "webrtc"})
    assert resp.status_code == 200
    session_id = resp.json()["sessionId"]
    uuid.UUID(session_id)  # well-formed
    assert main.active_sessions[session_id]["user_id"] == "uid-a"


def test_start_with_default_ice_servers(client, auth_as):
    auth_as()
    resp = client.post("/start", json={"enableDefaultIceServers": True})
    ice = resp.json()["iceConfig"]["iceServers"]
    assert ice and "stun:" in ice[0]["urls"][0]


def test_start_without_ice_request_omits_ice_config(client, auth_as):
    auth_as()
    resp = client.post("/start", json={})
    assert "iceConfig" not in resp.json()


def test_start_tolerates_missing_body(client, auth_as):
    auth_as()
    resp = client.post("/start")
    assert resp.status_code == 200
    assert "sessionId" in resp.json()


def test_session_scoped_offer_requires_known_session(client, stub_handler, auth_as):
    auth_as()
    resp = client.post(
        "/sessions/not-a-real-session/api/offer",
        json={"sdp": "v=0...", "type": "offer"},
    )
    assert resp.status_code == 404
    assert stub_handler.web_requests == []


def test_session_scoped_offer_with_valid_session(client, stub_handler, auth_as):
    auth_as("uid-a")
    session_id = client.post("/start", json={}).json()["sessionId"]
    resp = client.post(
        f"/sessions/{session_id}/api/offer",
        json={"sdp": "v=0...", "type": "offer"},
    )
    assert resp.status_code == 200
    assert resp.json()["pc_id"] == "pc-test-1"
    assert stub_handler.web_requests[0].sdp == "v=0..."


def test_session_scoped_patch_requires_known_session(client, stub_handler, auth_as):
    auth_as()
    resp = client.patch(
        "/sessions/nope/api/offer", json={"pc_id": "x", "candidates": []}
    )
    assert resp.status_code == 404
    assert stub_handler.patch_requests == []


def test_session_alive_route(client, auth_as, monkeypatch):
    """The while-active liveness poll: verified identity + session id go to
    the repo check (covered in test_db); the route just relays the answer."""
    calls = []

    async def fake_is_active(session_id, user_id):
        calls.append((session_id, user_id))
        return session_id == "live-1"

    monkeypatch.setattr(main, "session_is_active", fake_is_active)

    assert client.get("/sessions/live-1/alive").status_code == 401  # no token

    auth_as("uid-a")
    assert client.get("/sessions/live-1/alive").json() == {"alive": True}
    assert client.get("/sessions/dead-1/alive").json() == {"alive": False}
    assert calls == [("live-1", "uid-a"), ("dead-1", "uid-a")]


def test_users_cannot_operate_each_others_sessions(client, stub_handler, auth_as):
    """NFR-8 negative test at the session layer: a valid token for user B
    must not open, offer into, or patch user A's session."""
    auth_as("uid-a")
    session_id = client.post("/start", json={}).json()["sessionId"]

    auth_as("uid-b")
    offer = client.post(
        f"/sessions/{session_id}/api/offer", json={"sdp": "v=0...", "type": "offer"}
    )
    patch = client.patch(
        f"/sessions/{session_id}/api/offer", json={"pc_id": "x", "candidates": []}
    )
    assert offer.status_code == 404  # indistinguishable from nonexistent
    assert patch.status_code == 404
    assert stub_handler.web_requests == []
    assert stub_handler.patch_requests == []


def test_ice_candidate_patch_on_owned_session(client, stub_handler, auth_as):
    auth_as("uid-a")
    session_id = client.post("/start", json={}).json()["sessionId"]
    resp = client.patch(
        f"/sessions/{session_id}/api/offer",
        json={"pc_id": "pc-test-1", "candidates": []},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "success"}
    assert len(stub_handler.patch_requests) == 1


def test_upload_document_returns_metadata(client, auth_as):
    auth_as()
    resp = client.post(
        "/documents",
        files={"file": ("notes.md", b"# Title\nhello there", "text/markdown")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["filename"] == "notes.md"
    assert body["char_count"] > 0
    assert "id" in body


def test_upload_document_rejects_unsupported_type(client, auth_as):
    auth_as()
    resp = client.post(
        "/documents",
        files={"file": ("pic.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )
    assert resp.status_code == 400


def test_documents_are_isolated_per_user(client, auth_as, stub_handler, monkeypatch):
    """NFR-8 negative test at the document layer: user B's session cannot
    resolve user A's uploaded document ids."""
    auth_as("uid-a")
    uploaded = client.post(
        "/documents", files={"file": ("a.txt", b"private notes", "text/plain")}
    ).json()

    auth_as("uid-b")
    session_id = client.post(
        "/start", json={"body": {"document_ids": [uploaded["id"]]}}
    ).json()["sessionId"]

    captured = {}

    class FakeAgent:
        def __init__(self, settings, documents=None, *, user_id, session_id):
            captured["documents"] = documents
            captured["user_id"] = user_id
            captured["session_id"] = session_id

        async def run(self, connection):
            pass

    class InvokingHandler:
        async def handle_web_request(self, request, webrtc_connection_callback):
            class Conn:
                pc_id = "pc-iso-test"

            await webrtc_connection_callback(Conn())
            return {"type": "answer", "pc_id": "pc-iso-test"}

    monkeypatch.setattr(main, "CompanionAgent", FakeAgent)
    monkeypatch.setattr(main, "webrtc_handler", InvokingHandler())

    resp = client.post(
        f"/sessions/{session_id}/api/offer", json={"sdp": "v=0...", "type": "offer"}
    )
    assert resp.status_code == 200
    assert captured["documents"] == []  # A's doc invisible to B
    assert captured["user_id"] == "uid-b"


def test_documents_reach_the_agent_via_session_start(client, auth_as, monkeypatch):
    """End-to-end wiring: /documents -> /start(document_ids) -> session offer
    resolves the docs and hands them, with the verified user, to the agent."""
    auth_as("uid-a")
    uploaded = client.post(
        "/documents", files={"file": ("a.txt", b"hello world", "text/plain")}
    ).json()
    session_id = client.post(
        "/start", json={"body": {"document_ids": [uploaded["id"]]}}
    ).json()["sessionId"]

    captured = {}

    class FakeAgent:
        def __init__(self, settings, documents=None, *, user_id, session_id):
            captured["documents"] = documents
            captured["user_id"] = user_id
            captured["session_id"] = session_id

        async def run(self, connection):
            pass

    class InvokingHandler:
        async def handle_web_request(self, request, webrtc_connection_callback):
            class Conn:
                pc_id = "pc-doc-test"

            await webrtc_connection_callback(Conn())
            return {"type": "answer", "pc_id": "pc-doc-test"}

    monkeypatch.setattr(main, "CompanionAgent", FakeAgent)
    monkeypatch.setattr(main, "webrtc_handler", InvokingHandler())

    resp = client.post(
        f"/sessions/{session_id}/api/offer", json={"sdp": "v=0...", "type": "offer"}
    )
    assert resp.status_code == 200
    assert [d.content for d in captured["documents"]] == ["hello world"]
    assert captured["user_id"] == "uid-a"
