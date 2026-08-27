"""Rate limiter contracts (used by the signup pre-check, FR-30)."""

import time

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.ratelimit import rate_limited


def _app(max_requests: int, window_s: float) -> TestClient:
    app = FastAPI()

    @app.get("/limited", dependencies=[Depends(rate_limited(max_requests, window_s))])
    async def limited():
        return {"ok": True}

    return TestClient(app)


def test_allows_up_to_limit_then_429():
    client = _app(3, 60.0)
    for _ in range(3):
        assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 429


def test_buckets_are_per_caller():
    """X-Forwarded-For (set by Caddy) is the caller key — one abuser must
    not throttle everyone else."""
    client = _app(1, 60.0)
    assert client.get("/limited", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.get("/limited", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 429
    assert client.get("/limited", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200


def test_window_expiry_frees_the_bucket():
    client = _app(1, 0.2)
    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 429
    time.sleep(0.25)
    assert client.get("/limited").status_code == 200


def test_last_forwarded_hop_wins_and_spoofed_prefixes_cannot_mint_identities():
    """Caddy appends the true peer to client-supplied X-Forwarded-For, so
    only the LAST entry is trustworthy. A caller varying the spoofable
    prefix must keep hitting the same bucket."""
    client = _app(1, 60.0)
    assert (
        client.get(
            "/limited", headers={"X-Forwarded-For": "fake-1, 9.9.9.9"}
        ).status_code
        == 200
    )
    # Different spoofed prefix, same real (last) hop: still rate-limited.
    assert (
        client.get(
            "/limited", headers={"X-Forwarded-For": "fake-2, 9.9.9.9"}
        ).status_code
        == 429
    )
