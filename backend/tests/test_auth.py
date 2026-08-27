"""Auth boundary contracts (FR-23, FR-26(d) server side, FR-28, FR-29).

Verification itself is faked at the narrowest seam (app.auth._verify_token);
everything else — bearer parsing, error mapping, admin gating, the
check_revoked flag — runs for real through the routes.
"""

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as fb_auth

import app.auth as auth_mod
import app.main as main


@pytest.fixture
def client():
    with TestClient(main.app) as c:  # runs lifespan → init_db on sqlite
        yield c


def _fake_verify(monkeypatch, claims=None, error=None):
    calls = []

    def fake(token, check_revoked):
        calls.append({"token": token, "check_revoked": check_revoked})
        if error is not None:
            raise error
        return claims

    monkeypatch.setattr(auth_mod, "_verify_token", fake)
    return calls


PROTECTED = [
    ("post", "/documents"),
    ("post", "/start"),
    ("post", "/sessions/any/api/offer"),
    ("patch", "/sessions/any/api/offer"),
    ("get", "/api/admin/users"),
    ("post", "/api/admin/users/some-uid/disable"),
    ("post", "/api/admin/users/some-uid/enable"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_every_route_401s_without_a_token(client, method, path):
    resp = getattr(client, method)(path)
    assert resp.status_code == 401


def test_non_bearer_scheme_is_rejected(client):
    resp = client.post("/start", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


def test_invalid_token_maps_to_one_generic_401(client, monkeypatch):
    _fake_verify(monkeypatch, error=ValueError("bad token"))
    resp = client.post("/start", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"


def test_disabled_account_is_locked_out_of_session_start(client, monkeypatch):
    _fake_verify(
        monkeypatch, error=fb_auth.UserDisabledError("disabled", cause=None, http_response=None)
    )
    resp = client.post("/start", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 401
    assert "disabled" in resp.json()["detail"].lower()


def test_start_verifies_with_check_revoked_and_binds_session_to_uid(
    client, monkeypatch
):
    """FR-29: session establishment pays the revocation check; the minted
    session belongs to the token's uid, provisioned with the name claim."""
    calls = _fake_verify(
        monkeypatch,
        claims={
            "sub": "uid-42",
            "email": "u@example.com",
            "email_verified": True,
            "name": "Jig",
        },
    )
    resp = client.post("/start", json={}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert calls[0]["check_revoked"] is True
    session_id = resp.json()["sessionId"]
    assert main.active_sessions[session_id]["user_id"] == "uid-42"


def test_documents_route_uses_cheap_verification(client, monkeypatch):
    calls = _fake_verify(
        monkeypatch,
        claims={"sub": "uid-42", "email_verified": True},
    )
    client.post(
        "/documents",
        files={"file": ("a.txt", b"hi", "text/plain")},
        headers={"Authorization": "Bearer t"},
    )
    assert calls[0]["check_revoked"] is False


def test_admin_route_403s_without_admin_claim(client, monkeypatch):
    _fake_verify(monkeypatch, claims={"sub": "uid-1", "email_verified": True})
    resp = client.get("/api/admin/users", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403


def test_admin_write_routes_403_without_admin_claim(client, monkeypatch):
    """The privileged, state-mutating actions get their own negative test."""
    _fake_verify(monkeypatch, claims={"sub": "uid-1", "email_verified": True})
    for action in ("disable", "enable"):
        resp = client.post(
            f"/api/admin/users/uid-2/{action}",
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 403


def test_admin_route_allows_admin_claim(client, monkeypatch):
    _fake_verify(
        monkeypatch,
        claims={"sub": "uid-1", "email_verified": True, "admin": True},
    )
    monkeypatch.setattr(main.admin, "list_accounts", lambda: [{"uid": "uid-1"}])
    resp = client.get("/api/admin/users", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert resp.json()["users"] == [{"uid": "uid-1"}]


def test_admin_disable_route_invokes_account_op(client, monkeypatch):
    _fake_verify(
        monkeypatch,
        claims={"sub": "uid-1", "email_verified": True, "admin": True},
    )
    calls = []
    monkeypatch.setattr(
        main.admin, "set_account_disabled", lambda uid, disabled: calls.append((uid, disabled))
    )
    resp = client.post(
        "/api/admin/users/uid-2/disable", headers={"Authorization": "Bearer t"}
    )
    assert resp.status_code == 200
    assert calls == [("uid-2", True)]


def test_admin_claim_must_be_exactly_true(client, monkeypatch):
    """A truthy-but-wrong claim value ("yes", 1) must not grant admin."""
    _fake_verify(
        monkeypatch,
        claims={"sub": "uid-1", "email_verified": True, "admin": "yes"},
    )
    resp = client.get("/api/admin/users", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 403
