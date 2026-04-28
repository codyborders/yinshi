"""Provision a tenant user and emit a valid authenticated session cookie."""

from __future__ import annotations

import json
import sys

from yinshi.auth import create_session_token
from yinshi.db import init_control_db, init_db
from yinshi.services.accounts import resolve_or_create_user


def main() -> None:
    """Create or resolve a user account for Playwright and print auth JSON."""
    if len(sys.argv) < 2:
        raise SystemExit("usage: auth_cookie.py EMAIL [PROVIDER_USER_ID]")

    email = sys.argv[1]
    provider_user_id = sys.argv[2] if len(sys.argv) > 2 else email.replace("@", "-at-")

    init_db()
    init_control_db()

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id=provider_user_id,
        email=email,
        display_name="Playwright User",
    )
    token = create_session_token(tenant.user_id)

    print(
        json.dumps(
            {
                "email": tenant.email,
                "userId": tenant.user_id,
                "token": token,
            }
        )
    )


if __name__ == "__main__":
    main()
