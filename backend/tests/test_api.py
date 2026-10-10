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


def test_lifespan_retires_bootstrap_engine(client):
    """Pins the startup ordering: after the lifespan's sweep, the RLS-exempt
    bootstrap engine is retired — dropping the retire call in a refactor
    would leave the superuser engine live for the whole process."""
    import db.engine as engine_mod

    assert engine_mod._bootstrap_engine is None
    # ...and not vacuously: init_db really ran (None is also the
    # never-initialized state)
    assert engine_mod._session_factory is not None


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
    assert body["title"] == "notes.md"
    assert body["format"] == "markdown"  # FR-50's three-way split
    assert body["char_count"] > 0
    assert isinstance(body["id"], int)  # FR-51: the integer row id
    assert "mime_type" not in body  # retired with the store


def test_upload_goes_through_the_threadpool(client, auth_as, monkeypatch):
    """FR-51's structural test (not timing — timing flakes): the route's
    extraction call site goes through the threadpool wrapper, keeping pypdf
    parses off the event loop that drives live pipelines."""
    calls = []
    real = main.run_in_threadpool

    async def spying(fn, *args, **kwargs):
        calls.append(fn)
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(main, "run_in_threadpool", spying)
    auth_as()
    resp = client.post(
        "/documents", files={"file": ("a.txt", b"hello", "text/plain")}
    )
    assert resp.status_code == 200
    assert main.extract_text in calls


def test_upload_quota_maps_to_400_naming_the_limit(client, auth_as, monkeypatch):
    """FR-51: the typed quota refusal is a clear 400, never a generic 500."""
    import db.documents_repo as repo

    monkeypatch.setattr(repo, "MAX_DOCUMENTS_PER_USER", 1)
    auth_as("uid-quota")
    assert (
        client.post(
            "/documents", files={"file": ("a.txt", b"one", "text/plain")}
        ).status_code
        == 200
    )
    resp = client.post(
        "/documents", files={"file": ("b.txt", b"two", "text/plain")}
    )
    assert resp.status_code == 400
    assert "limit" in resp.json()["detail"]


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


def test_workspace_routes_crud_and_isolation(client, auth_as):
    """FR-52: list/get/delete with the NFR-8 negatives on all three routes —
    another user's id never appears in the list, GETs not-found, DELETEs
    not-found and deletes nothing; the list never serializes content."""
    auth_as("uid-a")
    up = client.post(
        "/documents", files={"file": ("a.md", b"# alpha", "text/markdown")}
    ).json()

    listing = client.get("/documents").json()
    assert listing["total"] >= 1
    item = next(d for d in listing["documents"] if d["id"] == up["id"])
    assert item["title"] == "a.md"
    assert "content" not in item
    assert item["char_count"] == len("# alpha")

    doc = client.get(f"/documents/{up['id']}").json()
    assert doc["content"] == "# alpha"
    assert doc["source"] == "uploaded"

    auth_as("uid-b")
    assert all(
        d["id"] != up["id"] for d in client.get("/documents").json()["documents"]
    )
    assert client.get(f"/documents/{up['id']}").status_code == 404
    assert client.delete(f"/documents/{up['id']}").status_code == 404

    auth_as("uid-a")
    assert client.get(f"/documents/{up['id']}").status_code == 200  # B deleted nothing
    assert client.delete(f"/documents/{up['id']}").json() == {"deleted": True}
    assert client.get(f"/documents/{up['id']}").status_code == 404


def test_upload_survives_restart_and_still_attaches(tmp_path, auth_as, monkeypatch):
    """FR-51: upload -> restart -> the document still resolves (the
    persistence the in-memory store lacked)."""
    import dataclasses

    monkeypatch.setattr(
        main,
        "settings",
        dataclasses.replace(
            main.settings, database_url=f"sqlite+aiosqlite:///{tmp_path}/restart.db"
        ),
    )
    with TestClient(main.app) as c:
        auth_as("uid-a")
        up = c.post(
            "/documents", files={"file": ("a.txt", b"persist me", "text/plain")}
        ).json()
    with TestClient(main.app) as c:  # a fresh lifespan = process restart
        auth_as("uid-a")
        doc = c.get(f"/documents/{up['id']}").json()
        assert doc["content"] == "persist me"


def test_attach_id_coercion_skips_dedupes_and_caps(monkeypatch):
    """FR-51: non-integer ids (the old UUID shape from a stale tab) are
    silently skipped, duplicates collapse first-wins, and the list is
    length-capped before any query."""

    monkeypatch.setattr(main, "MAX_DOCUMENTS_PER_USER", 3)
    assert main._coerce_attach_ids(["12", 13, 13, "old-uuid", None, 14.9]) == [
        12,
        13,
        14,
    ]
    assert main._coerce_attach_ids("not-a-list") == []
    assert main._coerce_attach_ids([1, 2, 3, 4, 5]) == [1, 2, 3]


def test_documents_rate_limit_is_per_user(client, auth_as):
    """FR-51's limiter proof: user A rate-limited while user B succeeds in
    the same window — the half that proves it is not keyed per-IP."""
    from app.ratelimit import DOCUMENTS_RATE_LIMIT

    auth_as("uid-rl-a")
    statuses = [
        client.post(
            "/documents", files={"file": (f"f{i}.txt", b"x", "text/plain")}
        ).status_code
        for i in range(DOCUMENTS_RATE_LIMIT + 1)
    ]
    assert statuses[-1] == 429
    assert all(s == 200 for s in statuses[:-1])

    auth_as("uid-rl-b")
    resp = client.post(
        "/documents", files={"file": ("b.txt", b"y", "text/plain")}
    )
    assert resp.status_code == 200


def test_upload_provisions_the_users_row(client, auth_as):
    """FR-51/24: uploads precede /start, so POST /documents must provision —
    SQLite doesn't enforce the FK, so without this assert removing
    provision_user keeps CI green while prod 500s a new account's first
    action."""
    import asyncio

    from sqlalchemy import select

    from db.engine import session_factory
    from db.models import User

    auth_as("uid-fresh-upload")
    assert (
        client.post(
            "/documents", files={"file": ("a.txt", b"hi", "text/plain")}
        ).status_code
        == 200
    )

    async def check():
        async with session_factory()() as db:
            row = (
                await db.execute(select(User).where(User.id == "uid-fresh-upload"))
            ).scalar_one_or_none()
        assert row is not None

    asyncio.run(check())


def test_delete_route_reconciles_live_sessions(client, auth_as):
    """FR-52's seam end-to-end: DELETE /documents/{id} drives
    notify_document_deleted into every registered provider of the owner —
    the destroyed row must stop shipping from live attach blocks."""
    import agent.registry as registry_mod

    class FakeProvider:
        def __init__(self):
            self.removed = []

        def remove_document_section(self, doc_id):
            self.removed.append(doc_id)
            return True

        def update_document_section(self, *a):
            return True

    auth_as("uid-del-live")
    up = client.post(
        "/documents", files={"file": ("gone.txt", b"bye", "text/plain")}
    ).json()

    mine, other_user = FakeProvider(), FakeProvider()
    registry_mod.register_provider("s-live-1", "uid-del-live", mine)
    registry_mod.register_provider("s-live-2", "uid-other", other_user)
    try:
        assert client.delete(f"/documents/{up['id']}").json() == {"deleted": True}
        assert mine.removed == [up["id"]]
        assert other_user.removed == []  # never another user's sessions
    finally:
        registry_mod.unregister_provider("s-live-1")
        registry_mod.unregister_provider("s-live-2")
