"""Tiny in-memory, per-caller rate limiter (FastAPI dependency).

Single-process by design — this backend runs one uvicorn worker, so shared
state needs no Redis; a Redis-backed limiter arrives if the backend ever
scales horizontally (CURRENT-ARCHITECTURE.md scaling notes). State resets on
restart, which is fine for abuse throttling.

Caller identity: behind Caddy every TCP peer is localhost, so keying on the
socket address would throttle all users as one bucket. Caddy APPENDS the
real peer to any X-Forwarded-For the client already sent — so the LAST entry
is the only one our proxy vouches for; earlier entries are attacker-
controlled (trusting the first would let a caller mint a fresh identity per
request and bypass the limit entirely). In dev (no proxy) the socket address
is the fallback; the backend port is never internet-reachable in prod.
"""

import time
from collections import deque

from fastapi import Depends, HTTPException, Request
from loguru import logger

from app.auth import AuthedUser

_SWEEP_THRESHOLD = 1024  # drop stale buckets once the dict grows past this

# §4.11 FR-51: POST /documents per-user limit (requests per minute).
DOCUMENTS_RATE_LIMIT = 20

# §4.12 FR-56: PUT /documents/{id} per-user limit — sized above FR-58.1's
# autosave cadence (max-wait saves are ~20-30/min per open document); a
# 429 lands in the client's retryable class.
DOCUMENT_SAVE_RATE_LIMIT = 120


def _caller_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Last entry = the hop our own proxy appended. Never the first.
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _check(
    buckets: dict[str, deque], key: str, max_requests: int, window_s: float, path: str
) -> None:
    now = time.monotonic()
    bucket = buckets.setdefault(key, deque())
    while bucket and now - bucket[0] > window_s:
        bucket.popleft()
    if len(bucket) >= max_requests:
        logger.bind(
            component="app.ratelimit",
            event="ratelimit.rejected",
            path=path,
        ).warning("rate limit exceeded")
        raise HTTPException(
            status_code=429, detail="Too many requests — try again shortly"
        )
    bucket.append(now)
    if len(buckets) > _SWEEP_THRESHOLD:
        for stale in [
            k for k, b in buckets.items() if not b or now - b[-1] > window_s
        ]:
            if stale != key:
                del buckets[stale]


def rate_limited(max_requests: int, window_s: float):
    """Dependency factory: at most `max_requests` per caller per window."""
    buckets: dict[str, deque] = {}

    async def dependency(request: Request) -> None:
        _check(buckets, _caller_key(request), max_requests, window_s, request.url.path)

    return dependency


def user_rate_limited(max_requests: int, window_s: float, auth_dep):
    """Per-USER limiter for authed routes (§4.11 FR-51): keyed by verified
    user_id, never IP (the email-check pattern predates having an identity
    to key on). `auth_dep` must be THE SAME callable the route declares —
    FastAPI's per-request dependency cache then resolves identity once; a
    different callable is a cache miss and a second (local) verify."""
    buckets: dict[str, deque] = {}

    async def dependency(
        request: Request, user: AuthedUser | str = Depends(auth_dep)
    ) -> None:
        # the dep may yield AuthedUser (checked routes) or the plain uid
        # string (get_current_user_id routes) — key on the uid either way
        key = user if isinstance(user, str) else user.user_id
        _check(buckets, key, max_requests, window_s, request.url.path)

    return dependency
