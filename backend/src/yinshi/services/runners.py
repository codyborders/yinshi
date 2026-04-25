"""Control-plane records and tokens for bring-your-own-cloud runners."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from yinshi.db import get_control_db
from yinshi.exceptions import RunnerAuthenticationError, RunnerRegistrationError

_REGISTRATION_TOKEN_TTL_MINUTES = 60
_HEARTBEAT_ONLINE_WINDOW_SECONDS = 120
_DEFAULT_RUNNER_DATA_DIR = "/var/lib/yinshi"
_DEFAULT_CAPABILITIES = {
    "posix_storage": True,
    "sqlite": True,
    "git_worktrees": True,
    "pi_sidecar": True,
}


def _require_user_id(user_id: str) -> str:
    """Return a normalized user id or raise for invalid caller state."""
    if not isinstance(user_id, str):
        raise TypeError("user_id must be a string")
    normalized_user_id = user_id.strip()
    if not normalized_user_id:
        raise ValueError("user_id must not be empty")
    return normalized_user_id


def _require_non_empty_text(value: str, name: str) -> str:
    """Return normalized text for fields that must be present."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"{name} must not be empty")
    return normalized_value


def _utc_now() -> datetime:
    """Return the current UTC time with stable second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def _datetime_to_storage(value: datetime) -> str:
    """Serialize a timezone-aware timestamp for lexical SQLite comparisons."""
    if not isinstance(value, datetime):
        raise TypeError("value must be a datetime")
    if value.tzinfo is None:
        raise ValueError("value must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _datetime_from_storage(value: object) -> datetime | None:
    """Parse SQLite timestamp strings produced by Yinshi schemas."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("stored datetime values must be strings or None")
    normalized_value = value.strip()
    if not normalized_value:
        return None
    parsed_value = datetime.fromisoformat(normalized_value.replace(" ", "T"))
    if parsed_value.tzinfo is None:
        parsed_value = parsed_value.replace(tzinfo=timezone.utc)
    return parsed_value.astimezone(timezone.utc)


def _hash_token(token: str) -> str:
    """Hash a token before storage so bearer values are never persisted."""
    normalized_token = _require_non_empty_text(token, "token")
    return hashlib.sha256(normalized_token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    """Generate one URL-safe high-entropy bearer token."""
    return secrets.token_urlsafe(32)


def _capabilities_json(capabilities: dict[str, Any]) -> str:
    """Serialize runner capabilities after rejecting non-object payloads."""
    if not isinstance(capabilities, dict):
        raise TypeError("capabilities must be a dictionary")
    try:
        serialized = json.dumps(capabilities, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("capabilities must be JSON serializable") from exc
    if len(serialized.encode("utf-8")) > 16_384:
        raise ValueError("capabilities payload is too large")
    return serialized


def _decode_capabilities(capabilities_json: object) -> dict[str, Any]:
    """Decode a capabilities JSON object from the control database."""
    if not isinstance(capabilities_json, str):
        raise TypeError("capabilities_json must be a string")
    payload = json.loads(capabilities_json or "{}")
    if not isinstance(payload, dict):
        raise ValueError("capabilities_json must decode to an object")
    return cast(dict[str, Any], payload)


def _display_status(row: Any, *, now: datetime | None = None) -> str:
    """Compute the user-facing runner status from registration and heartbeat fields."""
    assert row is not None, "row must not be None"
    current_time = now or _utc_now()
    if row["revoked_at"] is not None:
        return "revoked"
    if row["registered_at"] is None:
        return "pending"

    last_heartbeat_at = _datetime_from_storage(row["last_heartbeat_at"])
    if last_heartbeat_at is None:
        return "offline"
    heartbeat_age = (current_time - last_heartbeat_at).total_seconds()
    if heartbeat_age <= _HEARTBEAT_ONLINE_WINDOW_SECONDS:
        return "online"
    return "offline"


def _serialize_runner(row: Any) -> dict[str, Any]:
    """Return the safe API representation for one runner row."""
    assert row is not None, "row must not be None"
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "name": row["name"],
        "cloud_provider": row["cloud_provider"],
        "region": row["region"],
        "status": _display_status(row),
        "registered_at": row["registered_at"],
        "last_heartbeat_at": row["last_heartbeat_at"],
        "runner_version": row["runner_version"],
        "capabilities": _decode_capabilities(row["capabilities_json"]),
        "data_dir": row["data_dir"],
    }


def get_runner_for_user(user_id: str) -> dict[str, Any] | None:
    """Return the current runner status for a user, including revoked state."""
    normalized_user_id = _require_user_id(user_id)
    with get_control_db() as db:
        row = db.execute(
            "SELECT * FROM user_runners WHERE user_id = ?",
            (normalized_user_id,),
        ).fetchone()
    if row is None:
        return None
    return _serialize_runner(row)


def create_runner_registration(
    user_id: str,
    *,
    name: str,
    cloud_provider: str,
    region: str,
    control_url: str,
) -> dict[str, Any]:
    """Create or rotate a one-time runner registration token for a user."""
    normalized_user_id = _require_user_id(user_id)
    normalized_name = _require_non_empty_text(name, "name")
    normalized_provider = _require_non_empty_text(cloud_provider, "cloud_provider")
    normalized_region = _require_non_empty_text(region, "region")
    normalized_control_url = _require_non_empty_text(control_url, "control_url").rstrip("/")
    if normalized_provider != "aws":
        raise ValueError("Only AWS runners are supported")

    registration_token = _new_token()
    registration_token_hash = _hash_token(registration_token)
    expires_at = _utc_now() + timedelta(minutes=_REGISTRATION_TOKEN_TTL_MINUTES)
    expires_at_text = _datetime_to_storage(expires_at)
    empty_capabilities = _capabilities_json({})

    with get_control_db() as db:
        db.execute(
            """
            INSERT INTO user_runners (
                user_id, name, cloud_provider, region, status,
                registration_token_hash, registration_token_expires_at,
                runner_token_hash, registered_at, last_heartbeat_at,
                runner_version, capabilities_json, data_dir, revoked_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, NULL, NULL, ?, NULL, NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                name = excluded.name,
                cloud_provider = excluded.cloud_provider,
                region = excluded.region,
                status = 'pending',
                registration_token_hash = excluded.registration_token_hash,
                registration_token_expires_at = excluded.registration_token_expires_at,
                runner_token_hash = NULL,
                registered_at = NULL,
                last_heartbeat_at = NULL,
                runner_version = NULL,
                capabilities_json = excluded.capabilities_json,
                data_dir = NULL,
                revoked_at = NULL
            """,
            (
                normalized_user_id,
                normalized_name,
                normalized_provider,
                normalized_region,
                registration_token_hash,
                expires_at_text,
                empty_capabilities,
            ),
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM user_runners WHERE user_id = ?",
            (normalized_user_id,),
        ).fetchone()

    assert row is not None, "runner row must exist after registration upsert"
    environment = {
        "YINSHI_CONTROL_URL": normalized_control_url,
        "YINSHI_REGISTRATION_TOKEN": registration_token,
        "YINSHI_RUNNER_DATA_DIR": _DEFAULT_RUNNER_DATA_DIR,
        "YINSHI_RUNNER_TOKEN_FILE": f"{_DEFAULT_RUNNER_DATA_DIR}/runner-token",
        "YINSHI_RUNNER_ENV_FILE": "/etc/yinshi-runner.env",
    }
    return {
        "runner": _serialize_runner(row),
        "registration_token": registration_token,
        "registration_token_expires_at": expires_at_text,
        "control_url": normalized_control_url,
        "environment": environment,
    }


def revoke_runner_for_user(user_id: str) -> bool:
    """Revoke the current runner and all of its outstanding tokens."""
    normalized_user_id = _require_user_id(user_id)
    revoked_at = _datetime_to_storage(_utc_now())
    with get_control_db() as db:
        result = db.execute(
            """
            UPDATE user_runners
            SET status = 'revoked',
                revoked_at = ?,
                registration_token_hash = NULL,
                registration_token_expires_at = NULL,
                runner_token_hash = NULL
            WHERE user_id = ? AND revoked_at IS NULL
            """,
            (revoked_at, normalized_user_id),
        )
        db.commit()
    return result.rowcount > 0


def register_runner(
    registration_token: str,
    *,
    runner_version: str,
    capabilities: dict[str, Any],
    data_dir: str,
) -> dict[str, Any]:
    """Consume a one-time registration token and issue a runner bearer token."""
    token_hash = _hash_token(registration_token)
    normalized_version = _require_non_empty_text(runner_version, "runner_version")
    normalized_data_dir = _require_non_empty_text(data_dir, "data_dir")
    capabilities_text = _capabilities_json({**_DEFAULT_CAPABILITIES, **capabilities})
    now_text = _datetime_to_storage(_utc_now())
    runner_token = _new_token()
    runner_token_hash = _hash_token(runner_token)

    with get_control_db() as db:
        row = db.execute(
            """
            SELECT * FROM user_runners
            WHERE registration_token_hash = ? AND revoked_at IS NULL
            """,
            (token_hash,),
        ).fetchone()
        if row is None:
            raise RunnerRegistrationError("Runner registration token is invalid")

        expires_at = _datetime_from_storage(row["registration_token_expires_at"])
        if expires_at is None:
            raise RunnerRegistrationError("Runner registration token has expired")
        if expires_at <= _utc_now():
            raise RunnerRegistrationError("Runner registration token has expired")

        db.execute(
            """
            UPDATE user_runners
            SET status = 'online',
                registration_token_hash = NULL,
                registration_token_expires_at = NULL,
                runner_token_hash = ?,
                registered_at = ?,
                last_heartbeat_at = ?,
                runner_version = ?,
                capabilities_json = ?,
                data_dir = ?
            WHERE id = ?
            """,
            (
                runner_token_hash,
                now_text,
                now_text,
                normalized_version,
                capabilities_text,
                normalized_data_dir,
                row["id"],
            ),
        )
        db.commit()

    return {
        "runner_id": row["id"],
        "runner_token": runner_token,
        "status": "online",
    }


def record_runner_heartbeat(
    runner_token: str,
    *,
    runner_version: str,
    capabilities: dict[str, Any],
    data_dir: str,
) -> dict[str, Any]:
    """Record a heartbeat from a registered runner bearer token."""
    token_hash = _hash_token(runner_token)
    normalized_version = _require_non_empty_text(runner_version, "runner_version")
    normalized_data_dir = _require_non_empty_text(data_dir, "data_dir")
    capabilities_text = _capabilities_json({**_DEFAULT_CAPABILITIES, **capabilities})
    now_text = _datetime_to_storage(_utc_now())

    with get_control_db() as db:
        row = db.execute(
            """
            SELECT * FROM user_runners
            WHERE runner_token_hash = ? AND revoked_at IS NULL
            """,
            (token_hash,),
        ).fetchone()
        if row is None:
            raise RunnerAuthenticationError("Runner token is invalid")

        db.execute(
            """
            UPDATE user_runners
            SET status = 'online',
                last_heartbeat_at = ?,
                runner_version = ?,
                capabilities_json = ?,
                data_dir = ?
            WHERE id = ?
            """,
            (
                now_text,
                normalized_version,
                capabilities_text,
                normalized_data_dir,
                row["id"],
            ),
        )
        db.commit()

    return {"runner_id": row["id"], "status": "online"}
