"""Hosted account authorization records for desktop clients."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from yinshi.db import get_control_db
from yinshi.services.desktop_tokens import (
    create_desktop_token,
    desktop_signing_public_key,
)
from yinshi.services.live_auth_sessions import signal_desktop_device_revoked

_AUTHORIZATION_TTL = timedelta(minutes=10)


class DesktopAuthorizationNotFoundError(Exception):
    """Raised when an opaque request id has no matching stored record."""


class DesktopAuthorizationExpiredError(Exception):
    """Raised when a stored request is past its authorization window."""


class DesktopAuthorizationUsedError(Exception):
    """Raised when a stored request or code has already been consumed."""


class DesktopCodeInvalidError(Exception):
    """Raised when a desktop authorization code cannot identify an account."""


class DesktopPkceMismatchError(Exception):
    """Raised when the submitted verifier does not match the stored challenge."""


class DesktopAccountUnavailableError(Exception):
    """Raised when the approved account can no longer receive credentials."""


class DesktopRefreshInvalidError(Exception):
    """Raised after an invalid, expired, revoked, or replayed refresh credential."""


@dataclass(frozen=True, slots=True)
class DesktopTokenExchange:
    """Initial account and credential material for one registered desktop."""

    access_token: str
    access_token_expires_at: int
    refresh_token: str
    refresh_token_expires_at: int
    account_lease: str
    account_lease_expires_at: int
    device_id: str
    signing_public_key: str
    user_id: str
    user_email: str


def _issue_desktop_tokens(
    *,
    user_id: str,
    user_email: str,
    device_id: str,
    issued_at: int,
) -> DesktopTokenExchange:
    """Create one internally consistent access, refresh, and lease credential set."""
    if not user_id or not user_email or not device_id:
        raise ValueError("desktop token identity fields must not be empty")
    if not isinstance(issued_at, int) or issued_at < 1:
        raise ValueError("issued_at must be a positive integer")

    access_expires_at = issued_at + 15 * 60
    lease_expires_at = issued_at + 30 * 24 * 60 * 60
    refresh_expires_at = issued_at + 90 * 24 * 60 * 60
    return DesktopTokenExchange(
        access_token=create_desktop_token(
            token_type="access",
            user_id=user_id,
            device_id=device_id,
            issued_at=issued_at,
            expires_at=access_expires_at,
        ),
        access_token_expires_at=access_expires_at,
        refresh_token=secrets.token_urlsafe(48),
        refresh_token_expires_at=refresh_expires_at,
        account_lease=create_desktop_token(
            token_type="lease",
            user_id=user_id,
            device_id=device_id,
            issued_at=issued_at,
            expires_at=lease_expires_at,
        ),
        account_lease_expires_at=lease_expires_at,
        device_id=device_id,
        signing_public_key=desktop_signing_public_key(),
        user_id=user_id,
        user_email=user_email,
    )


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


def exchange_desktop_authorization_code(
    *,
    authorization_code: str,
    code_verifier: str,
    device_name: str,
) -> DesktopTokenExchange:
    """Atomically consume a PKCE code and store one desktop refresh credential."""
    for name, value in (
        ("authorization_code", authorization_code),
        ("code_verifier", code_verifier),
        ("device_name", device_name),
    ):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        if not value.strip():
            raise ValueError(f"{name} must not be empty")

    code_hash = hashlib.sha256(authorization_code.encode("utf-8")).hexdigest()
    verifier_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    current_time = int(time.time())

    with get_control_db() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            """
            SELECT request_id_hash, user_id, code_challenge, expires_at, consumed_at
            FROM desktop_authorization_requests
            WHERE authorization_code_hash = ?
            """,
            (code_hash,),
        ).fetchone()
        if row is None or row["user_id"] is None:
            raise DesktopCodeInvalidError
        if row["consumed_at"] is not None:
            raise DesktopAuthorizationUsedError
        if int(row["expires_at"]) <= current_time:
            raise DesktopAuthorizationExpiredError
        if not secrets.compare_digest(row["code_challenge"], verifier_challenge):
            raise DesktopPkceMismatchError
        user = database.execute(
            "SELECT id, email FROM users WHERE id = ? AND status = 'active'",
            (row["user_id"],),
        ).fetchone()
        if user is None:
            raise DesktopAccountUnavailableError

        credentials = _issue_desktop_tokens(
            user_id=user["id"],
            user_email=user["email"],
            device_id=secrets.token_hex(16),
            issued_at=current_time,
        )
        refresh_token_hash = hashlib.sha256(credentials.refresh_token.encode("utf-8")).hexdigest()
        device_result = database.execute(
            """
            INSERT INTO desktop_devices (
                id, user_id, name, created_at,
                refresh_token_hash, refresh_token_expires_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                credentials.device_id,
                user["id"],
                device_name.strip(),
                current_time,
                refresh_token_hash,
                credentials.refresh_token_expires_at,
                current_time,
            ),
        )
        if device_result.rowcount != 1:
            raise RuntimeError("desktop device was not stored")
        consume_result = database.execute(
            """
            UPDATE desktop_authorization_requests
            SET consumed_at = ?
            WHERE request_id_hash = ? AND consumed_at IS NULL
            """,
            (current_time, row["request_id_hash"]),
        )
        if consume_result.rowcount != 1:
            raise DesktopAuthorizationUsedError
        database.commit()

    return credentials


def rotate_desktop_refresh_token(*, refresh_token: str) -> DesktopTokenExchange:
    """Rotate a current refresh token or revoke its device when an old token reappears."""
    if not isinstance(refresh_token, str):
        raise TypeError("refresh_token must be a string")
    if len(refresh_token) < 32 or len(refresh_token) > 256:
        raise DesktopRefreshInvalidError

    refresh_token_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    current_time = int(time.time())
    with get_control_db() as database:
        database.execute("BEGIN IMMEDIATE")
        device = database.execute(
            """
            SELECT d.*, u.email, u.status AS user_status
            FROM desktop_devices d
            JOIN users u ON u.id = d.user_id
            WHERE d.refresh_token_hash = ?
            """,
            (refresh_token_hash,),
        ).fetchone()
        if device is None:
            used_token = database.execute(
                """
                SELECT device_id FROM desktop_used_refresh_tokens
                WHERE token_hash = ?
                """,
                (refresh_token_hash,),
            ).fetchone()
            if used_token is not None:
                database.execute(
                    """
                    UPDATE desktop_devices
                    SET revoked_at = ?
                    WHERE id = ? AND revoked_at IS NULL
                    """,
                    (current_time, used_token["device_id"]),
                )
                database.commit()
                signal_desktop_device_revoked(used_token["device_id"])
            raise DesktopRefreshInvalidError
        if device["user_status"] != "active" or device["revoked_at"] is not None:
            raise DesktopRefreshInvalidError
        if int(device["refresh_token_expires_at"]) <= current_time:
            raise DesktopRefreshInvalidError

        credentials = _issue_desktop_tokens(
            user_id=device["user_id"],
            user_email=device["email"],
            device_id=device["id"],
            issued_at=current_time,
        )
        new_refresh_token_hash = hashlib.sha256(
            credentials.refresh_token.encode("utf-8")
        ).hexdigest()
        database.execute(
            """
            INSERT INTO desktop_used_refresh_tokens (token_hash, device_id, rotated_at)
            VALUES (?, ?, ?)
            """,
            (refresh_token_hash, device["id"], current_time),
        )
        result = database.execute(
            """
            UPDATE desktop_devices
            SET refresh_token_hash = ?, refresh_token_expires_at = ?, last_seen_at = ?
            WHERE id = ? AND refresh_token_hash = ? AND revoked_at IS NULL
            """,
            (
                new_refresh_token_hash,
                credentials.refresh_token_expires_at,
                current_time,
                device["id"],
                refresh_token_hash,
            ),
        )
        if result.rowcount != 1:
            raise DesktopRefreshInvalidError
        database.commit()
    return credentials


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
