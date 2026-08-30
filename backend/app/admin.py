"""Admin API (FR-29 account controls + FR-35/36/37 observability).

Mounted at /api/admin (the /api/* path Caddy and the Next.js dev proxy
already route to the backend). Every route requires the admin custom claim,
verified server-side per request (FR-28); cross-user data reads go through
db.admin_repo's admin_scoped_session (FR-38) — read-only, transaction-local,
and blind to content tables by construction. Responses carry usage and
metadata only, never conversation content (NFR-9).
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from loguru import logger

from app.auth import (
    AuthedUser,
    get_account,
    get_current_admin,
    list_accounts,
    set_account_disabled,
)
from app.config import load_settings
from app.costs import estimate_cost
from db import admin_repo

router = APIRouter(prefix="/api/admin")

_settings = load_settings()

_SORT_KEYS = ("last_active", "sessions", "cost", "email")


@router.get("/users")
async def admin_list_users(
    q: str = Query("", max_length=200),
    sort: str = Query("last_active"),
    order: str = Query("desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    admin: AuthedUser = Depends(get_current_admin),
):
    """FR-35: one paginated Firebase traversal + one DB aggregate pass per
    request, merged in memory (search runs over the merged set — Postgres
    has no email column; fine at current account counts)."""
    if sort not in _SORT_KEYS:
        raise HTTPException(status_code=400, detail=f"sort must be one of {_SORT_KEYS}")
    if order not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail="order must be asc or desc")
    # Independent lookups (Firebase over the network, DB aggregates):
    # concurrent, so the page waits for the slower of the two, not the sum.
    accounts, aggregates = await asyncio.gather(
        run_in_threadpool(list_accounts), admin_repo.user_aggregates(admin)
    )

    # A uid with DB rows but no Firebase account (deleted account, test
    # data) must still appear — silent UNDER-reporting is the worst failure
    # mode for a cost view. Synthesize a stub account row for those.
    known = {a["uid"] for a in accounts}
    merged = list(accounts) + [
        {
            "uid": uid,
            "email": None,
            "display_name": None,
            "email_verified": False,
            "disabled": False,
            "providers": [],
            "created_at": None,
            "last_sign_in": None,
        }
        for uid in aggregates
        if uid not in known
    ]

    users = []
    needle = q.strip().lower()
    for account in merged:
        agg = aggregates.get(
            account["uid"], {"sessions": 0, "last_active": None, "usage": {}}
        )
        if needle and not any(
            needle in (account.get(field) or "").lower()
            for field in ("email", "display_name", "uid")
        ):
            continue
        users.append(
            {
                **account,
                "sessions": agg["sessions"],
                "last_active": agg["last_active"],
                "usage": agg["usage"],
                "estimated_cost": estimate_cost(agg["usage"], _settings),
            }
        )

    def sort_key(u):
        if sort == "sessions":
            return u["sessions"]
        if sort == "cost":
            return u["estimated_cost"]["total"]
        if sort == "email":
            return (u.get("email") or "").lower()
        return u["last_active"] or ""

    users.sort(key=sort_key, reverse=(order != "asc"))
    start = (page - 1) * page_size
    return {
        "users": users[start : start + page_size],
        "total": len(users),
        "page": page,
        "page_size": page_size,
    }


@router.get("/users/{uid}/sessions")
async def admin_user_sessions(uid: str, admin: AuthedUser = Depends(get_current_admin)):
    """FR-36: session history. Account identity comes from Firebase so the
    page can show who this is (a single-uid lookup — the full traversal is
    for the list view only); usage/latency from the DB aggregates."""
    sessions = await admin_repo.sessions_for_user(admin, uid)
    for s in sessions:
        s["estimated_cost"] = estimate_cost(s["usage"], _settings)
    try:
        account = await run_in_threadpool(get_account, uid)
    except Exception as e:
        # A transient Firebase failure must not 500 the page and discard
        # the DB half that already succeeded — the page renders account:
        # null as its "no Firebase account" state.
        logger.bind(
            component="app.admin",
            event="admin.account_lookup_failed",
            error_type=type(e).__name__,
        ).warning("account lookup failed; serving sessions without identity")
        account = None
    return {"account": account, "sessions": sessions}


@router.get("/sessions/{session_id}")
async def admin_session_detail(
    session_id: str, admin: AuthedUser = Depends(get_current_admin)
):
    """FR-36 drill-down: per-turn latency joined with per-turn cost."""
    detail = await admin_repo.session_detail(admin, session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="unknown session")
    detail["estimated_cost"] = estimate_cost(detail["usage"], _settings)
    for turn in detail["turns"]:
        turn["estimated_cost"] = estimate_cost(turn["usage"], _settings)
    return detail


@router.get("/overview")
async def admin_overview(admin: AuthedUser = Depends(get_current_admin)):
    """FR-37: the at-a-glance tab."""
    # Function-local on purpose: agent.companion imports the provider stack;
    # importing it at module load would make the admin API depend on (and
    # pay the import cost of) the whole pipeline. This reads one process
    # counter, nothing more.
    from agent.companion import live_session_count

    data = await admin_repo.overview(admin)
    data["live_sessions"] = live_session_count()
    data["estimated_cost_7d"] = estimate_cost(data["usage_7d"], _settings)
    data["estimated_cost_30d"] = estimate_cost(data["usage_30d"], _settings)
    return data


@router.post("/users/{uid}/disable")
async def admin_disable_user(
    uid: str, admin: AuthedUser = Depends(get_current_admin)
):
    await run_in_threadpool(set_account_disabled, uid, True)
    logger.bind(
        component="app.admin", event="admin.user_disabled", target_uid=uid
    ).info(f"user disabled by admin {admin.user_id}")
    return {"uid": uid, "disabled": True}


@router.post("/users/{uid}/enable")
async def admin_enable_user(
    uid: str, admin: AuthedUser = Depends(get_current_admin)
):
    await run_in_threadpool(set_account_disabled, uid, False)
    logger.bind(
        component="app.admin", event="admin.user_enabled", target_uid=uid
    ).info(f"user re-enabled by admin {admin.user_id}")
    return {"uid": uid, "disabled": False}
