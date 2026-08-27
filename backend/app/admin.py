"""Admin API (FR-29): list accounts, disable/enable an account.

Mounted at /api/admin (the /api/* path Caddy and the Next.js dev proxy
already route to the backend). Every route requires the admin custom claim,
verified server-side per request (FR-28) — the frontend's Admin nav link is
cosmetic, this dependency is the enforcement.
"""

from fastapi import APIRouter, Depends
from loguru import logger

from app.auth import AuthedUser, get_current_admin, list_accounts, set_account_disabled

router = APIRouter(prefix="/api/admin")


@router.get("/users")
async def admin_list_users(admin: AuthedUser = Depends(get_current_admin)):
    return {"users": list_accounts()}


@router.post("/users/{uid}/disable")
async def admin_disable_user(
    uid: str, admin: AuthedUser = Depends(get_current_admin)
):
    set_account_disabled(uid, disabled=True)
    logger.bind(
        component="app.admin", event="admin.user_disabled", target_uid=uid
    ).info(f"user disabled by admin {admin.user_id}")
    return {"uid": uid, "disabled": True}


@router.post("/users/{uid}/enable")
async def admin_enable_user(
    uid: str, admin: AuthedUser = Depends(get_current_admin)
):
    set_account_disabled(uid, disabled=False)
    logger.bind(
        component="app.admin", event="admin.user_enabled", target_uid=uid
    ).info(f"user re-enabled by admin {admin.user_id}")
    return {"uid": uid, "disabled": False}
