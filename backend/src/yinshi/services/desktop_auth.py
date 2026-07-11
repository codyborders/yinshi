"""Hosted account authorization records for desktop clients."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from yinshi.db import get_control_db

_AUTHORIZATION_TTL = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class DesktopAuthorizationRequest:
    """One newly stored opaque desktop authorization request."""

    request_id: str
    authorize_url: str
    expires_at: datetime


def create_desktop_authorization_request(
    *,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    frontend_url: str,
) -> DesktopAuthorizationRequest:
    """Store and return one short-lived PKCE-bound desktop request."""
    for name, value in (
        ("redirect_uri", redirect_uri),
        ("code_challenge", code_challenge),
        ("state", state),
        ("frontend_url", frontend_url),
    ):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        if not value:
            raise ValueError(f"{name} must not be empty")

    request_id = secrets.token_urlsafe(32)
    if len(request_id) < 32:
        raise RuntimeError("generated desktop request id was unexpectedly short")
    created_at = datetime.now(timezone.utc)
    expires_at = created_at + _AUTHORIZATION_TTL
    request_id_hash = hashlib.sha256(request_id.encode("utf-8")).hexdigest()

    with get_control_db() as database:
        cursor = database.execute(
            """
            INSERT INTO desktop_authorization_requests (
                request_id_hash, created_at, expires_at,
                redirect_uri, code_challenge, state
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                request_id_hash,
                int(created_at.timestamp()),
                int(expires_at.timestamp()),
                redirect_uri,
                code_challenge,
                state,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("desktop authorization request was not stored")
        database.commit()

    authorize_url = f"{frontend_url.rstrip('/')}/auth/desktop/authorize/{request_id}"
    return DesktopAuthorizationRequest(
        request_id=request_id,
        authorize_url=authorize_url,
        expires_at=expires_at,
    )
