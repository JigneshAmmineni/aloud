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

from fastapi import HTTPException, Request
from loguru import logger

_SWEEP_THRESHOLD = 1024  # drop stale buckets once the dict grows past this


def _caller_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Last entry = the hop our own proxy appended. Never the first.
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def rate_limited(max_requests: int, window_s: float):
    """Dependency factory: at most `max_requests` per caller per window."""
    buckets: dict[str, deque] = {}

    async def dependency(request: Request) -> None:
        now = time.monotonic()
        key = _caller_key(request)
        bucket = buckets.setdefault(key, deque())
        while bucket and now - bucket[0] > window_s:
            bucket.popleft()
        if len(bucket) >= max_requests:
            logger.bind(
                component="app.ratelimit",
                event="ratelimit.rejected",
                path=request.url.path,
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

    return dependency
