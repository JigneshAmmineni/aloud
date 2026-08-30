"""Admin observability API contracts (FR-35/36/37): admin gating, the
Firebase↔DB merge, search/sort/pagination, cost injection. The DB-facing
repo layer is tested for real in test_admin_repo.py / test_rls.py — here it
is faked so these tests pin the ROUTE behavior only."""

import pytest
from fastapi.testclient import TestClient

import app.admin as admin_mod
import app.main as main
import db.admin_repo as admin_repo_mod


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


_ACCOUNTS = [
    {
        "uid": "uid-a", "email": "ada@example.com", "display_name": "Ada",
        "email_verified": True, "disabled": False, "providers": ["google.com"],
        "created_at": 1, "last_sign_in": 2,
    },
    {
        "uid": "uid-b", "email": "bob@example.com", "display_name": None,
        "email_verified": True, "disabled": True, "providers": ["password"],
        "created_at": 1, "last_sign_in": 2,
    },
    {
        "uid": "uid-c", "email": "cleo@example.com", "display_name": "Cleo",
        "email_verified": False, "disabled": False, "providers": ["password"],
        "created_at": 1, "last_sign_in": None,
    },
]

_AGGREGATES = {
    "uid-a": {
        "sessions": 4,
        "last_active": "2026-08-27T10:00:00+00:00",
        "usage": {"llm.tokens_in": 1000.0, "stt.seconds": 300.0},
    },
    "uid-b": {
        "sessions": 9,
        "last_active": "2026-08-20T10:00:00+00:00",
        "usage": {"llm.tokens_in": 50.0},
    },
    # uid-c: no DB rows at all — must still appear with zeros
}


@pytest.fixture
def fake_admin_data(monkeypatch):
    monkeypatch.setattr(admin_mod, "list_accounts", lambda: list(_ACCOUNTS))

    async def fake_aggregates(admin):
        return dict(_AGGREGATES)

    monkeypatch.setattr(admin_repo_mod, "user_aggregates", fake_aggregates)


def test_admin_routes_require_admin(client, auth_as):
    auth_as("uid-plain", admin=False)
    for path in (
        "/api/admin/users",
        "/api/admin/users/uid-a/sessions",
        "/api/admin/sessions/s-1",
        "/api/admin/overview",
    ):
        assert client.get(path).status_code == 403
    assert client.post("/api/admin/users/uid-a/disable").status_code == 403


def test_users_merges_firebase_with_db_aggregates(client, auth_as, fake_admin_data):
    auth_as("admin-1", admin=True)
    body = client.get("/api/admin/users").json()
    assert body["total"] == 3
    by_uid = {u["uid"]: u for u in body["users"]}
    assert by_uid["uid-a"]["sessions"] == 4
    assert by_uid["uid-a"]["email"] == "ada@example.com"  # Firebase half
    assert by_uid["uid-a"]["usage"]["llm.tokens_in"] == 1000.0  # DB half
    assert "estimated_cost" in by_uid["uid-a"]  # cost injected, FR-34
    # an account with no DB rows still lists, zeroed
    assert by_uid["uid-c"]["sessions"] == 0
    assert by_uid["uid-c"]["last_active"] is None


def test_users_search_sort_pagination(client, auth_as, fake_admin_data):
    auth_as("admin-1", admin=True)

    # search: substring over email/name/uid (FR-35)
    body = client.get("/api/admin/users", params={"q": "ada"}).json()
    assert [u["uid"] for u in body["users"]] == ["uid-a"]
    body = client.get("/api/admin/users", params={"q": "CLEO"}).json()
    assert [u["uid"] for u in body["users"]] == ["uid-c"]

    # sort by sessions, both directions
    body = client.get("/api/admin/users", params={"sort": "sessions"}).json()
    assert [u["sessions"] for u in body["users"]] == [9, 4, 0]
    body = client.get(
        "/api/admin/users", params={"sort": "sessions", "order": "asc"}
    ).json()
    assert [u["sessions"] for u in body["users"]] == [0, 4, 9]

    # pagination: page 2 of size 1 is the middle row; total still reports 3
    body = client.get(
        "/api/admin/users",
        params={"sort": "sessions", "page": "2", "page_size": "1"},
    ).json()
    assert body["total"] == 3
    assert [u["sessions"] for u in body["users"]] == [4]

    # unknown sort key / order are a clean 400, not a 500 or silent default
    assert client.get("/api/admin/users", params={"sort": "nope"}).status_code == 400
    assert (
        client.get("/api/admin/users", params={"order": "sideways"}).status_code
        == 400
    )


def test_users_includes_db_only_uids(client, auth_as, monkeypatch):
    """A uid with usage in the DB but no Firebase account (deleted account)
    must still appear — silent under-reporting is the worst failure mode
    for a cost view."""
    auth_as("admin-1", admin=True)
    monkeypatch.setattr(admin_mod, "list_accounts", lambda: [dict(_ACCOUNTS[0])])

    async def fake_aggregates(admin):
        return {
            "uid-a": _AGGREGATES["uid-a"],
            "uid-ghost": {
                "sessions": 2,
                "last_active": "2026-08-28T10:00:00+00:00",
                "usage": {"stt.seconds": 60.0},
            },
        }

    monkeypatch.setattr(admin_repo_mod, "user_aggregates", fake_aggregates)
    body = client.get("/api/admin/users").json()
    assert body["total"] == 2
    ghost = next(u for u in body["users"] if u["uid"] == "uid-ghost")
    assert ghost["email"] is None
    assert ghost["sessions"] == 2


def test_user_sessions_route_merges_account_and_costs(
    client, auth_as, monkeypatch
):
    auth_as("admin-1", admin=True)
    # single-uid lookup, never the full list_accounts traversal
    monkeypatch.setattr(
        admin_mod,
        "get_account",
        lambda uid: _ACCOUNTS[0] if uid == "uid-a" else None,
    )

    async def fake_sessions(admin, user_id):
        assert user_id == "uid-a"
        return {
            "sessions": [
                {
                    "session_id": "s-1",
                    "started_at": "2026-08-27T10:00:00+00:00",
                    "duration_s": 60.0,
                    "status": "ended",
                    "end_reason": "user",
                    "usage": {"llm.tokens_in": 100.0},
                    "artifact_count": 1,
                    "median_turn_ms": 1000,
                    "worst_turn_ms": 2000,
                }
            ],
            "total_sessions": 700,  # capped list, true count passes through
        }

    monkeypatch.setattr(admin_repo_mod, "sessions_for_user", fake_sessions)
    body = client.get("/api/admin/users/uid-a/sessions").json()
    assert body["account"]["email"] == "ada@example.com"
    assert body["sessions"][0]["estimated_cost"]["total"] >= 0
    assert body["total_sessions"] == 700


def test_session_detail_route_404_and_costs(client, auth_as, monkeypatch):
    auth_as("admin-1", admin=True)

    async def fake_detail(admin, session_id):
        if session_id != "s-1":
            return None
        return {
            "session_id": "s-1",
            "user_id": "uid-a",
            "started_at": "2026-08-27T10:00:00+00:00",
            "ended_at": None,
            "status": "ended",
            "end_reason": "user",
            "duration_s": None,
            "usage": {"llm.tokens_in": 100.0},
            "turns": [
                {
                    "turn_id": 1,
                    "eot_to_first_audio_ms": 1200,
                    "stages_ms": None,
                    "usage": {"llm.tokens_in": 100.0},
                }
            ],
        }

    monkeypatch.setattr(admin_repo_mod, "session_detail", fake_detail)
    # FR-41 breadcrumb: the owner's email is resolved server-side (never a
    # URL param)
    monkeypatch.setattr(
        admin_mod,
        "get_account",
        lambda uid: {"email": "ada@example.com"} if uid == "uid-a" else None,
    )
    assert client.get("/api/admin/sessions/unknown").status_code == 404
    body = client.get("/api/admin/sessions/s-1").json()
    assert "estimated_cost" in body
    assert "estimated_cost" in body["turns"][0]  # per-turn cost, FR-36
    assert body["user_email"] == "ada@example.com"


def test_overview_route_adds_live_sessions_and_costs(client, auth_as, monkeypatch):
    auth_as("admin-1", admin=True)

    async def fake_overview(admin):
        return {
            "last_24h": {"sessions": 1, "unique_users": 1},
            "last_7d": {"sessions": 2, "unique_users": 2},
            "usage_7d": {"llm.tokens_in": 100.0},
            "usage_30d": {"llm.tokens_in": 200.0},
            "turn_latency_24h": {
                "p50_ms": 900, "p95_ms": 2000, "turns": 5, "nfr1_breaches": 0
            },
            "error_sessions_24h": 0,
        }

    monkeypatch.setattr(admin_repo_mod, "overview", fake_overview)
    body = client.get("/api/admin/overview").json()
    assert isinstance(body["live_sessions"], int)  # in-process counter, FR-37
    assert "estimated_cost_7d" in body
    assert "estimated_cost_30d" in body
