"""The auth boundary (REQUIREMENTS.md §4.8) — the ONLY auth-aware code.

Identity: a Firebase ID token in the Authorization header, verified with
firebase-admin (FR-23). Every route resolves identity through one of the
dependencies below; everything downstream receives plain values and never
knows Firebase exists. Admin = the `admin: true` custom claim (FR-28),
granted only by scripts/grant_admin.py.

Two verification strengths (FR-29):
  get_current_user          — local signature check (microseconds).
  get_current_user_checked  — adds check_revoked=True (a network round trip
                              to Firebase), used on session-establishment
                              endpoints so disabling a user blocks new
                              sessions instantly.
"""

import os
import threading
from dataclasses import dataclass

import firebase_admin
from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from firebase_admin import auth as fb_auth
from firebase_admin import credentials
from loguru import logger

_firebase_app: firebase_admin.App | None = None
_key_path: str | None = None
_init_lock = threading.Lock()


def configure(key_path: str) -> None:
    """Called once from the app lifespan with Settings' validated path, so
    this module doesn't re-derive config from the environment."""
    global _key_path
    _key_path = key_path


def _firebase() -> firebase_admin.App:
    """Lazy init so importing this module never requires credentials
    (tests override the dependencies and must not touch Firebase).
    Runs on threadpool workers, so the double-checked lock is load-bearing:
    without it, two concurrent first requests both call initialize_app and
    the loser's ValueError surfaces as a spurious 401."""
    global _firebase_app
    if _firebase_app is None:
        with _init_lock:
            if _firebase_app is None:
                path = _key_path or os.environ["FIREBASE_SERVICE_ACCOUNT_PATH"]
                _firebase_app = firebase_admin.initialize_app(
                    credentials.Certificate(path)
                )
    return _firebase_app


@dataclass(frozen=True)
class AuthedUser:
    user_id: str  # Firebase uid — the FK for all user-owned data
    email: str | None
    email_verified: bool
    name: str | None  # token `name` claim (FR-24: the only name source)
    is_admin: bool


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return token.strip()


def _verify_token(token: str, check_revoked: bool) -> dict:
    """Isolated so tests can fake verification without Firebase."""
    return fb_auth.verify_id_token(
        token, app=_firebase(), check_revoked=check_revoked
    )


async def _decode(token: str, check_revoked: bool) -> AuthedUser:
    try:
        # firebase-admin is synchronous (check_revoked even does a network
        # round trip); run it in the threadpool so it never blocks the event
        # loop that is also processing every live voice pipeline's frames.
        claims = await run_in_threadpool(_verify_token, token, check_revoked)
    except (fb_auth.RevokedIdTokenError, fb_auth.UserDisabledError):
        raise HTTPException(status_code=401, detail="Account disabled or revoked")
    except Exception as e:
        # Expired, malformed, wrong audience/issuer, cert fetch failure — all
        # collapse to one non-enumerating 401 (FR-26 discipline server-side),
        # but the server-side log keeps the real cause distinguishable.
        logger.bind(
            component="app.auth",
            event="auth.verify_failed",
            error_type=type(e).__name__,
        ).info("token verification failed")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return AuthedUser(
        user_id=claims["sub"],
        email=claims.get("email"),
        email_verified=bool(claims.get("email_verified")),
        name=claims.get("name"),
        is_admin=claims.get("admin") is True,
    )


async def get_current_user(request: Request) -> AuthedUser:
    return await _decode(_bearer_token(request), check_revoked=False)


async def get_current_user_checked(request: Request) -> AuthedUser:
    """FR-29: session-establishment verification — instant disable lockout."""
    return await _decode(_bearer_token(request), check_revoked=True)


async def get_current_user_id(user: AuthedUser = Depends(get_current_user)) -> str:
    """The seam most routes want: just the verified user_id."""
    return user.user_id


async def get_current_admin(
    user: AuthedUser = Depends(get_current_user),
) -> AuthedUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def email_exists(email: str) -> bool:
    """Signup pre-check (FR-30). A deliberate, rate-limited enumeration
    exception per FR-26: signup unavoidably reveals existence via
    email-already-in-use anyway — this surfaces the same fact one click
    earlier, before the form expands."""
    try:
        fb_auth.get_user_by_email(email, app=_firebase())
        return True
    except fb_auth.UserNotFoundError:
        return False


# --- Admin account operations (FR-29) — provider-aware, so they live here. ---


def list_accounts() -> list[dict]:
    """All Firebase accounts, for the admin user list."""
    users = []
    for u in fb_auth.list_users(app=_firebase()).iterate_all():
        users.append(
            {
                "uid": u.uid,
                "email": u.email,
                "display_name": u.display_name,
                "email_verified": u.email_verified,
                "disabled": u.disabled,
                "providers": [p.provider_id for p in u.provider_data],
                "created_at": u.user_metadata.creation_timestamp,
                "last_sign_in": u.user_metadata.last_sign_in_timestamp,
            }
        )
    return users


def set_account_disabled(uid: str, disabled: bool) -> None:
    """FR-29: disabling also revokes refresh tokens so the lockout bites at
    the next token refresh everywhere, and instantly at session start."""
    fb_auth.update_user(uid, disabled=disabled, app=_firebase())
    if disabled:
        fb_auth.revoke_refresh_tokens(uid, app=_firebase())
