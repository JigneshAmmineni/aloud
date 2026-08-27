"""Grant or revoke the admin custom claim (FR-28).

The ONLY way admin status is minted — run locally, with the Firebase
service-account key. Contains no identities: the email is an argument, so
nothing secret or admin-shaped ever lives in the repo.

Usage (from backend/, with FIREBASE_SERVICE_ACCOUNT_PATH set or the key
file present in this directory):

    python scripts/grant_admin.py you@example.com            # grant
    python scripts/grant_admin.py you@example.com --revoke   # revoke

Refuses to grant to an unverified email (FR-28): an attacker who squatted
an address with an unverified password signup must never become admin.
Revokes the target's refresh tokens afterward so the change lands at their
next token refresh (≤1h) or next sign-in, not just eventually.
"""

import argparse
import glob
import os
import sys

import firebase_admin
from firebase_admin import auth, credentials


def _find_key() -> str:
    path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH")
    if path and os.path.exists(path):
        return path
    matches = glob.glob(
        os.path.join(os.path.dirname(__file__), "..", "*firebase-adminsdk*.json")
    )
    if len(matches) == 1:
        return matches[0]
    sys.exit(
        "Set FIREBASE_SERVICE_ACCOUNT_PATH to the service-account JSON "
        "(see .env.example), or place exactly one *firebase-adminsdk*.json "
        "in backend/."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="email of the target account")
    parser.add_argument(
        "--revoke", action="store_true", help="remove admin instead of granting"
    )
    args = parser.parse_args()

    firebase_admin.initialize_app(credentials.Certificate(_find_key()))

    try:
        user = auth.get_user_by_email(args.email)
    except auth.UserNotFoundError:
        sys.exit(f"No account exists for {args.email} — they must sign up first.")

    if not args.revoke and not user.email_verified:
        sys.exit(
            f"Refusing: {args.email} is not verified (FR-28). A squatted, "
            "unverified account must never become admin. Have the real owner "
            "verify the address (or sign in with Google) first."
        )

    claims = dict(user.custom_claims or {})
    if args.revoke:
        claims.pop("admin", None)
    else:
        claims["admin"] = True
    auth.set_custom_user_claims(user.uid, claims or None)
    # Old tokens don't carry the change; force a refresh so it lands promptly.
    auth.revoke_refresh_tokens(user.uid)

    action = "revoked from" if args.revoke else "granted to"
    print(
        f"admin {action} {args.email} (uid {user.uid}). Takes effect at their "
        "next token refresh or sign-in."
    )


if __name__ == "__main__":
    main()
