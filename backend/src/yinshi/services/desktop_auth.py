"""Hosted account authorization records for desktop clients."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from yinshi.db import get_control_db

_AUTHORIZATION_TTL = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class DesktopAuthorizationNotFoundError(Exception):
    """Raised when an opaque request id has no matching stored record."""


class DesktopAuthorizationExpiredError(Exception):
    """Raised when a stored request is past its authorization window."""


class DesktopAuthorizationUsedError(Exception):
    """Raised when a stored request has already issued its one-time code."""


@dataclass(frozen=True, slots=True)
class ApprovedDesktopAuthorization:
    """One callback URL containing a newly issued authorization code."""

    callback_url: str


@dataclass(frozen=True, slots=True)
class DesktopAuthorizationRequest:
    """One newly stored opaque desktop authorization request."""

    request_id: str
    authorize_url: str
    expires_at: datetime


def approve_desktop_authorization_request(
    *,
    request_id: str,
    user_id: str,
) -> ApprovedDesktopAuthorization:
    """Bind one pending request to a user and issue its one-time callback code."""
    if not isinstance(request_id, str) or len(request_id) < 32 or len(request_id) > 128:
        raise DesktopAuthorizationNotFoundError
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id must not be empty")

    request_id_hash = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    with get_control_db() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            """
            SELECT redirect_uri, state, expires_at, approved_at,
                   authorization_code_hash
            FROM desktop_authorization_requests
            WHERE request_id_hash = ?
            """,
            (request_id_hash,),
        ).fetchone()
        if row is None:
            raise DesktopAuthorizationNotFoundError
        if row["approved_at"] is not None or row["authorization_code_hash"] is not None:
            raise DesktopAuthorizationUsedError
        current_time = int(time.time())
        if int(row["expires_at"]) <= current_time:
            raise DesktopAuthorizationExpiredError

        authorization_code = secrets.token_urlsafe(32)
        authorization_code_hash = hashlib.sha256(authorization_code.encode("utf-8")).hexdigest()
        result = database.execute(
            """
            UPDATE desktop_authorization_requests
            SET user_id = ?, authorization_code_hash = ?, approved_at = ?
            WHERE request_id_hash = ?
              AND approved_at IS NULL
              AND authorization_code_hash IS NULL
            """,
            (user_id, authorization_code_hash, current_time, request_id_hash),
        )
        if result.rowcount != 1:
            raise DesktopAuthorizationUsedError
        database.commit()

    callback_query = urlencode({"code": authorization_code, "state": row["state"]})
    return ApprovedDesktopAuthorization(
        callback_url=f"{row['redirect_uri']}?{callback_query}",
    )


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
