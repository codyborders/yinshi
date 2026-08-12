"""Control-plane records and tokens for bring-your-own-cloud runners."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Literal, cast

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)

from yinshi.db import get_control_db
from yinshi.exceptions import RunnerAuthenticationError, RunnerRegistrationError
from yinshi.services.runner_capabilities import runner_capability_signing_public_key

RunnerKind = Literal["byoc", "managed", "managed_restore", "managed_retired"]
RunnerStorageProfile = Literal[
    "aws_ebs_s3_files",
    "archil_shared_files",
    "archil_all_posix",
    "fly_sprites_posix",
]

_REGISTRATION_TOKEN_TTL_MINUTES = 60
_HEARTBEAT_ONLINE_WINDOW_SECONDS = 120
_DEFAULT_RUNNER_DATA_DIR = "/var/lib/yinshi"
_DEFAULT_SQLITE_DIR = f"{_DEFAULT_RUNNER_DATA_DIR}/sqlite"
_DEFAULT_SHARED_FILES_DIR = "/mnt/yinshi-s3-files"
_DEFAULT_FLY_SPRITES_SHARED_FILES_DIR = f"{_DEFAULT_RUNNER_DATA_DIR}/files"
_DEFAULT_ARCHIL_SHARED_FILES_DIR = "/mnt/archil/yinshi"
_DEFAULT_ARCHIL_SQLITE_DIR = f"{_DEFAULT_ARCHIL_SHARED_FILES_DIR}/sqlite"
_DEFAULT_RUNNER_TOKEN_FILE = f"{_DEFAULT_RUNNER_DATA_DIR}/runner-token"
_DEFAULT_RUNNER_ENV_FILE = "/etc/yinshi-runner.env"
_REGISTRATION_TOKEN_EXPIRED = "Runner registration token has expired"
_AWS_STORAGE_PROFILE: RunnerStorageProfile = "aws_ebs_s3_files"
_ARCHIL_SHARED_FILES_PROFILE: RunnerStorageProfile = "archil_shared_files"
_ARCHIL_ALL_POSIX_PROFILE: RunnerStorageProfile = "archil_all_posix"
_FLY_SPRITES_POSIX_PROFILE: RunnerStorageProfile = "fly_sprites_posix"
_STORAGE_ARCHIL = "archil"
_STORAGE_RUNNER_EBS = "runner_ebs"
_STORAGE_S3_FILES_OR_LOCAL_POSIX = "s3_files_or_local_posix"
_STORAGE_S3_FILES_MOUNT = "s3_files_mount"
_STORAGE_LOCAL_POSIX = "local_posix"
_RUNNER_NOISE_PUBLIC_KEY_LENGTH = 32
_RUNNER_NOISE_VALIDATION_PRIVATE_KEY = X25519PrivateKey.from_private_bytes(b"\x01" * 32)
_BASE_CAPABILITIES = {
    "posix_storage": True,
    "sqlite": True,
    "git_worktrees": True,
    "pi_sidecar": True,
}


@dataclass(frozen=True, slots=True)
class RunnerStorageProfileSpec:
    """Validation and default path facts for one runner storage profile."""

    value: RunnerStorageProfile
    sqlite_storage: str
    shared_files_storage: str
    requires_explicit_storage: bool
    default_sqlite_dir: str
    default_shared_files_dir: str
    live_sqlite_on_shared_files: bool
    experimental: bool
    allow_sqlite_under_shared_files: bool
    allowed_sqlite_storage: frozenset[str]
    allowed_shared_files_storage: frozenset[str]


_STORAGE_PROFILES: dict[RunnerStorageProfile, RunnerStorageProfileSpec] = {
    _AWS_STORAGE_PROFILE: RunnerStorageProfileSpec(
        value=_AWS_STORAGE_PROFILE,
        sqlite_storage=_STORAGE_RUNNER_EBS,
        shared_files_storage=_STORAGE_S3_FILES_OR_LOCAL_POSIX,
        requires_explicit_storage=False,
        default_sqlite_dir=_DEFAULT_SQLITE_DIR,
        default_shared_files_dir=_DEFAULT_SHARED_FILES_DIR,
        live_sqlite_on_shared_files=False,
        experimental=False,
        allow_sqlite_under_shared_files=False,
        allowed_sqlite_storage=frozenset({_STORAGE_RUNNER_EBS}),
        allowed_shared_files_storage=frozenset(
            {
                _STORAGE_S3_FILES_OR_LOCAL_POSIX,
                _STORAGE_S3_FILES_MOUNT,
                _STORAGE_LOCAL_POSIX,
            }
        ),
    ),
    _ARCHIL_SHARED_FILES_PROFILE: RunnerStorageProfileSpec(
        value=_ARCHIL_SHARED_FILES_PROFILE,
        sqlite_storage=_STORAGE_RUNNER_EBS,
        shared_files_storage=_STORAGE_ARCHIL,
        requires_explicit_storage=True,
        default_sqlite_dir=_DEFAULT_SQLITE_DIR,
        default_shared_files_dir=_DEFAULT_ARCHIL_SHARED_FILES_DIR,
        live_sqlite_on_shared_files=False,
        experimental=True,
        allow_sqlite_under_shared_files=False,
        allowed_sqlite_storage=frozenset({_STORAGE_RUNNER_EBS}),
        allowed_shared_files_storage=frozenset({_STORAGE_ARCHIL}),
    ),
    _ARCHIL_ALL_POSIX_PROFILE: RunnerStorageProfileSpec(
        value=_ARCHIL_ALL_POSIX_PROFILE,
        sqlite_storage=_STORAGE_ARCHIL,
        shared_files_storage=_STORAGE_ARCHIL,
        requires_explicit_storage=True,
        default_sqlite_dir=_DEFAULT_ARCHIL_SQLITE_DIR,
        default_shared_files_dir=_DEFAULT_ARCHIL_SHARED_FILES_DIR,
        live_sqlite_on_shared_files=True,
        experimental=True,
        allow_sqlite_under_shared_files=True,
        allowed_sqlite_storage=frozenset({_STORAGE_ARCHIL}),
        allowed_shared_files_storage=frozenset({_STORAGE_ARCHIL}),
    ),
    _FLY_SPRITES_POSIX_PROFILE: RunnerStorageProfileSpec(
        value=_FLY_SPRITES_POSIX_PROFILE,
        sqlite_storage=_STORAGE_LOCAL_POSIX,
        shared_files_storage=_STORAGE_LOCAL_POSIX,
        requires_explicit_storage=False,
        default_sqlite_dir=_DEFAULT_SQLITE_DIR,
        default_shared_files_dir=_DEFAULT_FLY_SPRITES_SHARED_FILES_DIR,
        live_sqlite_on_shared_files=False,
        experimental=False,
        allow_sqlite_under_shared_files=False,
        allowed_sqlite_storage=frozenset({_STORAGE_LOCAL_POSIX}),
        allowed_shared_files_storage=frozenset({_STORAGE_LOCAL_POSIX}),
    ),
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


def _optional_capability_text(capabilities: dict[str, Any], key: str) -> str | None:
    """Return one optional string capability after rejecting malformed values."""
    value = capabilities.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} capability must be a string")
    normalized_value = value.strip()
    if not normalized_value:
        return None
    return normalized_value


def _storage_profile_spec(storage_profile: str) -> RunnerStorageProfileSpec:
    """Return profile metadata after validating the stable profile value."""
    normalized_profile = _require_non_empty_text(storage_profile, "storage_profile")
    if normalized_profile not in _STORAGE_PROFILES:
        raise ValueError(f"Unsupported runner storage_profile: {normalized_profile}")
    return _STORAGE_PROFILES[normalized_profile]


def _storage_profile_from_capabilities(capabilities: dict[str, Any]) -> RunnerStorageProfile:
    """Read a persisted storage profile, defaulting prerelease rows to AWS BYOC."""
    storage_profile = _optional_capability_text(capabilities, "storage_profile")
    if storage_profile is None:
        return _AWS_STORAGE_PROFILE
    return _storage_profile_spec(storage_profile).value


def _require_storage_path(value: str, name: str) -> PurePosixPath:
    """Return a normalized absolute runner storage path."""
    normalized_value = _require_non_empty_text(value, name)
    path = PurePosixPath(normalized_value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    if ".." in path.parts:
        raise ValueError(f"{name} must not contain parent directory references")
    return path


def _path_contains(parent: PurePosixPath, child: PurePosixPath) -> bool:
    """Return whether child is equal to or nested under parent."""
    if child == parent:
        return True
    return parent in child.parents


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


def _validate_noise_public_key(value: str | None) -> str | None:
    """Return one canonical usable X25519 public key from runner input."""
    if value is None:
        return None
    normalized_value = _require_non_empty_text(value, "noise_public_key")
    try:
        public_key_bytes = base64.b64decode(
            normalized_value + "=",
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError("noise_public_key must be canonical base64url") from exc
    canonical_value = base64.urlsafe_b64encode(public_key_bytes).rstrip(b"=").decode("ascii")
    if canonical_value != normalized_value:
        raise ValueError("noise_public_key must be canonical base64url")
    if len(public_key_bytes) != _RUNNER_NOISE_PUBLIC_KEY_LENGTH:
        raise ValueError("noise_public_key must contain exactly 32 bytes")
    try:
        public_key = X25519PublicKey.from_public_bytes(public_key_bytes)
        _RUNNER_NOISE_VALIDATION_PRIVATE_KEY.exchange(public_key)
    except ValueError as exc:
        raise ValueError("noise_public_key is not a usable X25519 key") from exc
    return canonical_value


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


def _validated_storage_class(
    capabilities: dict[str, Any],
    *,
    key: str,
    profile: RunnerStorageProfileSpec,
    expected_value: str,
    allowed_values: frozenset[str],
    required: bool,
) -> str:
    """Return a canonical storage-class string for one profile-bound capability."""
    requested_value = _optional_capability_text(capabilities, key)
    if requested_value is None:
        if required:
            raise ValueError(f"{key} must be {expected_value} for {profile.value}")
        return expected_value
    if requested_value not in allowed_values:
        allowed_text = ", ".join(sorted(allowed_values))
        raise ValueError(f"{key} must be one of {allowed_text} for {profile.value}")
    return requested_value


def _storage_capabilities(
    capabilities: dict[str, Any],
    *,
    data_dir: str,
    sqlite_dir: str | None,
    shared_files_dir: str | None,
    storage_profile: str,
) -> dict[str, Any]:
    """Merge runner facts with profile defaults and profile-aware path checks."""
    if not isinstance(capabilities, dict):
        raise TypeError("capabilities must be a dictionary")

    profile = _storage_profile_spec(storage_profile)
    normalized_data_dir = _require_storage_path(data_dir, "data_dir")
    normalized_sqlite_dir = _require_storage_path(
        sqlite_dir or profile.default_sqlite_dir,
        "sqlite_dir",
    )
    normalized_shared_files_dir = _require_storage_path(
        shared_files_dir or profile.default_shared_files_dir,
        "shared_files_dir",
    )
    if not profile.allow_sqlite_under_shared_files:
        if _path_contains(normalized_shared_files_dir, normalized_sqlite_dir):
            raise ValueError("sqlite_dir must not live under shared_files_dir")

    sqlite_storage = _validated_storage_class(
        capabilities,
        key="sqlite_storage",
        profile=profile,
        expected_value=profile.sqlite_storage,
        allowed_values=profile.allowed_sqlite_storage,
        required=profile.requires_explicit_storage,
    )
    shared_files_storage = _validated_storage_class(
        capabilities,
        key="shared_files_storage",
        profile=profile,
        expected_value=profile.shared_files_storage,
        allowed_values=profile.allowed_shared_files_storage,
        required=profile.requires_explicit_storage,
    )

    merged_capabilities = {**_BASE_CAPABILITIES, **capabilities}
    merged_capabilities.update(
        {
            "data_dir": str(normalized_data_dir),
            "sqlite_dir": str(normalized_sqlite_dir),
            "shared_files_dir": str(normalized_shared_files_dir),
            "storage_profile": profile.value,
            "storage_profile_experimental": profile.experimental,
            "sqlite_storage": sqlite_storage,
            "shared_files_storage": shared_files_storage,
            "live_sqlite_on_shared_files": profile.live_sqlite_on_shared_files,
        }
    )
    return merged_capabilities


def _serialized_capabilities(capabilities_json: object) -> dict[str, Any]:
    """Return capabilities with profile defaults filled for legacy rows."""
    capabilities = _decode_capabilities(capabilities_json)
    storage_profile = _storage_profile_from_capabilities(capabilities)
    profile = _storage_profile_spec(storage_profile)
    data_dir = _optional_capability_text(capabilities, "data_dir") or _DEFAULT_RUNNER_DATA_DIR
    sqlite_dir = _optional_capability_text(capabilities, "sqlite_dir") or profile.default_sqlite_dir
    shared_files_dir = (
        _optional_capability_text(capabilities, "shared_files_dir")
        or profile.default_shared_files_dir
    )
    return _storage_capabilities(
        capabilities,
        data_dir=data_dir,
        sqlite_dir=sqlite_dir,
        shared_files_dir=shared_files_dir,
        storage_profile=storage_profile,
    )


def _requested_profile_matches(
    *,
    requested_profile: str,
    stored_capabilities: dict[str, Any],
) -> RunnerStorageProfile:
    """Return the stored profile, rejecting runner attempts to change it."""
    stored_profile = _storage_profile_from_capabilities(stored_capabilities)
    supplied_profile = _storage_profile_spec(requested_profile).value
    if supplied_profile != stored_profile:
        raise ValueError("storage_profile must match requested runner profile")
    return stored_profile


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


def _noise_key_fingerprint(noise_public_key: str | None) -> str | None:
    """Return a full SHA-256 fingerprint suitable for explicit user pairing."""
    if noise_public_key is None:
        return None
    validated_key = _validate_noise_public_key(noise_public_key)
    assert validated_key is not None
    key_bytes = base64.urlsafe_b64decode(validated_key + "=")
    return f"SHA256:{hashlib.sha256(key_bytes).hexdigest()}"


def _serialize_runner(row: Any) -> dict[str, Any]:
    """Return the safe API representation for one runner row."""
    assert row is not None, "row must not be None"
    noise_public_key = row["noise_public_key"]
    return {
        "id": row["id"],
        "kind": row["kind"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "name": row["name"],
        "cloud_provider": row["cloud_provider"],
        "region": row["region"],
        "status": _display_status(row),
        "registered_at": row["registered_at"],
        "last_heartbeat_at": row["last_heartbeat_at"],
        "runner_version": row["runner_version"],
        "capabilities": _serialized_capabilities(row["capabilities_json"]),
        "data_dir": row["data_dir"],
        "noise_public_key": noise_public_key,
        "noise_key_fingerprint": _noise_key_fingerprint(noise_public_key),
        "noise_key_confirmed": bool(noise_public_key and row["noise_public_key_confirmed_at"]),
        "restore_job_id": row["restore_job_id"] if "restore_job_id" in row.keys() else None,
    }


def get_runner_for_user(user_id: str) -> dict[str, Any] | None:
    """Return the current BYOC runner status for a user, including revoked state."""
    normalized_user_id = _require_user_id(user_id)
    with get_control_db() as db:
        row = db.execute(
            "SELECT * FROM user_runners WHERE user_id = ? AND kind = 'byoc'",
            (normalized_user_id,),
        ).fetchone()
    if row is None:
        return None
    return _serialize_runner(row)


def get_managed_runner_for_user(user_id: str) -> dict[str, Any] | None:
    """Return the internal managed runner status for a user, including revoked state."""
    normalized_user_id = _require_user_id(user_id)
    with get_control_db() as db:
        row = db.execute(
            "SELECT * FROM user_runners WHERE user_id = ? AND kind = 'managed'",
            (normalized_user_id,),
        ).fetchone()
    if row is None:
        return None
    return _serialize_runner(row)


def confirm_runner_noise_key(user_id: str, noise_public_key: str) -> dict[str, Any]:
    """Confirm the exact current runner identity after an out-of-band fingerprint check."""
    normalized_user_id = _require_user_id(user_id)
    normalized_public_key = _validate_noise_public_key(noise_public_key)
    assert normalized_public_key is not None
    confirmed_at = _datetime_to_storage(_utc_now())
    with get_control_db() as db:
        row = db.execute(
            "SELECT * FROM user_runners "
            "WHERE user_id = ? AND kind = 'byoc' AND revoked_at IS NULL",
            (normalized_user_id,),
        ).fetchone()
        if row is None or row["noise_public_key"] is None:
            raise RunnerRegistrationError("Runner Noise key is not available")
        if not secrets.compare_digest(row["noise_public_key"], normalized_public_key):
            raise RunnerRegistrationError("Runner Noise key does not match")
        db.execute(
            "UPDATE user_runners SET noise_public_key_confirmed_at = ? WHERE id = ?",
            (confirmed_at, row["id"]),
        )
        db.commit()
        confirmed_row = db.execute(
            "SELECT * FROM user_runners WHERE id = ?",
            (row["id"],),
        ).fetchone()
    assert confirmed_row is not None, "confirmed runner row must still exist"
    return _serialize_runner(confirmed_row)


def _runner_environment(
    *,
    control_url: str,
    registration_token: str,
    profile: RunnerStorageProfileSpec,
) -> dict[str, str]:
    """Return the systemd environment values needed to bootstrap one runner."""
    return {
        "YINSHI_CONTROL_URL": control_url,
        "YINSHI_REGISTRATION_TOKEN": registration_token,
        "YINSHI_RUNNER_STORAGE_PROFILE": profile.value,
        "YINSHI_RUNNER_SQLITE_STORAGE": profile.sqlite_storage,
        "YINSHI_RUNNER_SHARED_FILES_STORAGE": profile.shared_files_storage,
        "YINSHI_RUNNER_DATA_DIR": _DEFAULT_RUNNER_DATA_DIR,
        "YINSHI_RUNNER_SQLITE_DIR": profile.default_sqlite_dir,
        "YINSHI_RUNNER_SHARED_FILES_DIR": profile.default_shared_files_dir,
        "YINSHI_RUNNER_TOKEN_FILE": _DEFAULT_RUNNER_TOKEN_FILE,
        "YINSHI_RUNNER_NOISE_KEY_FILE": f"{_DEFAULT_RUNNER_DATA_DIR}/runner-noise.key",
        "YINSHI_RUNNER_CAPABILITY_SIGNING_KEY_FILE": (
            f"{_DEFAULT_RUNNER_DATA_DIR}/control-capability-signing.pub"
        ),
        "YINSHI_RUNNER_REPLAY_DATABASE_FILE": (
            f"{_DEFAULT_RUNNER_DATA_DIR}/runner-capability-replay.sqlite3"
        ),
        "YINSHI_RUNNER_ENV_FILE": _DEFAULT_RUNNER_ENV_FILE,
    }


def _create_runner_registration_in_connection(
    db: sqlite3.Connection,
    user_id: str,
    *,
    name: str,
    cloud_provider: str,
    region: str,
    storage_profile: str,
    control_url: str,
    runner_kind: RunnerKind = "byoc",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Upsert registration state without committing the caller-owned transaction."""
    normalized_user_id = _require_user_id(user_id)
    normalized_name = _require_non_empty_text(name, "name")
    normalized_provider = _require_non_empty_text(cloud_provider, "cloud_provider")
    normalized_region = _require_non_empty_text(region, "region")
    normalized_control_url = _require_non_empty_text(control_url, "control_url").rstrip("/")
    normalized_kind = _require_non_empty_text(runner_kind, "runner_kind")
    profile = _storage_profile_spec(storage_profile)
    if normalized_kind == "byoc":
        if normalized_provider != "aws":
            raise ValueError("Only AWS runners are supported")
    elif normalized_kind in {"managed", "managed_restore"}:
        if normalized_provider != "fly_sprites":
            raise ValueError("Managed runners require fly_sprites")
    else:
        raise ValueError(f"Unsupported runner kind: {normalized_kind}")

    registration_token = _new_token()
    registration_token_hash = _hash_token(registration_token)
    expires_at = (now or _utc_now()) + timedelta(minutes=_REGISTRATION_TOKEN_TTL_MINUTES)
    expires_at_text = _datetime_to_storage(expires_at)
    pending_capabilities = _capabilities_json(
        _storage_capabilities(
            {
                "sqlite_storage": profile.sqlite_storage,
                "shared_files_storage": profile.shared_files_storage,
            },
            data_dir=_DEFAULT_RUNNER_DATA_DIR,
            sqlite_dir=profile.default_sqlite_dir,
            shared_files_dir=profile.default_shared_files_dir,
            storage_profile=profile.value,
        )
    )
    db.execute(
        """
        INSERT INTO user_runners (
            user_id, kind, name, cloud_provider, region, status,
            registration_token_hash, registration_token_expires_at,
            runner_token_hash, registered_at, last_heartbeat_at,
            runner_version, capabilities_json, data_dir, revoked_at
        )
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, NULL, NULL, ?, NULL, NULL)
        ON CONFLICT(user_id, kind) DO UPDATE SET
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
            revoked_at = NULL,
            noise_public_key = CASE
                WHEN excluded.kind = 'managed' THEN user_runners.noise_public_key
                ELSE NULL
            END,
            noise_public_key_confirmed_at = CASE
                WHEN excluded.kind = 'managed'
                THEN user_runners.noise_public_key_confirmed_at
                ELSE NULL
            END
        """,
        (
            normalized_user_id,
            normalized_kind,
            normalized_name,
            normalized_provider,
            normalized_region,
            registration_token_hash,
            expires_at_text,
            pending_capabilities,
        ),
    )
    row = db.execute(
        "SELECT * FROM user_runners WHERE user_id = ? AND kind = ?",
        (normalized_user_id, normalized_kind),
    ).fetchone()
    assert row is not None, "runner row must exist after registration upsert"
    return {
        "runner": _serialize_runner(row),
        "registration_token": registration_token,
        "registration_token_expires_at": expires_at_text,
        "control_url": normalized_control_url,
        "environment": _runner_environment(
            control_url=normalized_control_url,
            registration_token=registration_token,
            profile=profile,
        ),
    }


def create_managed_restore_registration(
    user_id: str,
    *,
    job_id: str,
    name: str,
    region: str,
    control_url: str,
) -> dict[str, Any]:
    """Create replacement authority bound to one exact restore job."""
    normalized_user_id = _require_user_id(user_id)
    normalized_job_id = _require_non_empty_text(job_id, "job_id")
    with get_control_db() as database:
        registration = _create_runner_registration_in_connection(
            database,
            normalized_user_id,
            name=name,
            cloud_provider="fly_sprites",
            region=region,
            storage_profile="fly_sprites_posix",
            control_url=control_url,
            runner_kind="managed_restore",
        )
        result = database.execute(
            "UPDATE user_runners SET restore_job_id = ? WHERE id = ?",
            (normalized_job_id, registration["runner"]["id"]),
        )
        if result.rowcount != 1:
            database.rollback()
            raise RunnerRegistrationError("Restore runner registration could not be bound")
        database.commit()
    return registration


def get_managed_restore_runner_for_job(
    user_id: str,
    job_id: str,
) -> dict[str, Any] | None:
    """Return only replacement authority bound to one exact restore job."""
    normalized_user_id = _require_user_id(user_id)
    normalized_job_id = _require_non_empty_text(job_id, "job_id")
    with get_control_db() as database:
        row = database.execute(
            """SELECT * FROM user_runners
               WHERE user_id = ? AND kind = 'managed_restore' AND restore_job_id = ?""",
            (normalized_user_id, normalized_job_id),
        ).fetchone()
    return _serialize_runner(row) if row is not None else None


def revoke_managed_restore_runner_for_job(user_id: str, job_id: str) -> bool:
    """Revoke only replacement authority bound to one exact restore job."""
    normalized_user_id = _require_user_id(user_id)
    normalized_job_id = _require_non_empty_text(job_id, "job_id")
    revoked_at = _datetime_to_storage(_utc_now())
    with get_control_db() as database:
        result = database.execute(
            """UPDATE user_runners
               SET status = 'revoked', revoked_at = ?, registration_token_hash = NULL,
                   registration_token_expires_at = NULL, runner_token_hash = NULL
               WHERE user_id = ? AND kind = 'managed_restore'
                 AND restore_job_id = ? AND revoked_at IS NULL""",
            (revoked_at, normalized_user_id, normalized_job_id),
        )
        database.commit()
    return result.rowcount == 1


def get_managed_restore_runner_for_user(user_id: str) -> dict[str, Any] | None:
    """Return the non-active replacement runner for one managed restore."""
    normalized_user_id = _require_user_id(user_id)
    with get_control_db() as database:
        row = database.execute(
            "SELECT * FROM user_runners WHERE user_id = ? AND kind = 'managed_restore'",
            (normalized_user_id,),
        ).fetchone()
    return _serialize_runner(row) if row is not None else None


def create_runner_registration(
    user_id: str,
    *,
    name: str,
    cloud_provider: str,
    region: str,
    storage_profile: str,
    control_url: str,
    runner_kind: RunnerKind = "byoc",
) -> dict[str, Any]:
    """Create or rotate a one-time BYOC or internal managed registration token."""
    with get_control_db() as db:
        registration = _create_runner_registration_in_connection(
            db,
            user_id,
            name=name,
            cloud_provider=cloud_provider,
            region=region,
            storage_profile=storage_profile,
            control_url=control_url,
            runner_kind=runner_kind,
        )
        db.commit()
    return registration


def revoke_managed_restore_runner_for_user(user_id: str) -> bool:
    """Revoke the non-active replacement runner and all of its credentials."""
    normalized_user_id = _require_user_id(user_id)
    revoked_at = _datetime_to_storage(_utc_now())
    with get_control_db() as database:
        result = database.execute(
            """UPDATE user_runners
               SET status = 'revoked', revoked_at = ?,
                   registration_token_hash = NULL,
                   registration_token_expires_at = NULL,
                   runner_token_hash = NULL
               WHERE user_id = ? AND kind = 'managed_restore' AND revoked_at IS NULL""",
            (revoked_at, normalized_user_id),
        )
        database.commit()
    return result.rowcount == 1


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
            WHERE user_id = ? AND kind = 'byoc' AND revoked_at IS NULL
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
    sqlite_dir: str | None,
    shared_files_dir: str | None,
    storage_profile: str,
    noise_public_key: str | None,
) -> dict[str, Any]:
    """Consume a one-time registration token and issue a runner bearer token."""
    token_hash = _hash_token(registration_token)
    normalized_version = _require_non_empty_text(runner_version, "runner_version")
    normalized_noise_public_key = _validate_noise_public_key(noise_public_key)
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
            raise RunnerRegistrationError(_REGISTRATION_TOKEN_EXPIRED)
        if expires_at <= _utc_now():
            raise RunnerRegistrationError(_REGISTRATION_TOKEN_EXPIRED)

        stored_noise_public_key = row["noise_public_key"]
        if row["kind"] in {"managed", "managed_restore"} and stored_noise_public_key is not None:
            if normalized_noise_public_key is None or not secrets.compare_digest(
                stored_noise_public_key,
                normalized_noise_public_key,
            ):
                raise RunnerRegistrationError("Managed runner Noise identity does not match")

        stored_capabilities = _decode_capabilities(row["capabilities_json"])
        stored_profile = _requested_profile_matches(
            requested_profile=storage_profile,
            stored_capabilities=stored_capabilities,
        )
        merged_capabilities = _storage_capabilities(
            capabilities,
            data_dir=data_dir,
            sqlite_dir=sqlite_dir,
            shared_files_dir=shared_files_dir,
            storage_profile=stored_profile,
        )
        capabilities_text = _capabilities_json(merged_capabilities)
        normalized_data_dir = str(merged_capabilities["data_dir"])

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
                data_dir = ?,
                noise_public_key = ?,
                noise_public_key_confirmed_at = CASE
                    WHEN kind IN ('managed', 'managed_restore') THEN ?
                    WHEN noise_public_key = ? THEN noise_public_key_confirmed_at
                    ELSE NULL
                END
            WHERE id = ?
            """,
            (
                runner_token_hash,
                now_text,
                now_text,
                normalized_version,
                capabilities_text,
                normalized_data_dir,
                normalized_noise_public_key,
                now_text,
                normalized_noise_public_key,
                row["id"],
            ),
        )
        db.commit()

    return {
        "runner_id": row["id"],
        "runner_token": runner_token,
        "capability_signing_public_key": runner_capability_signing_public_key(),
        "status": "online",
    }


def authenticate_runner_token(runner_token: str) -> dict[str, str]:
    """Return relay identity for one current non-revoked runner bearer token."""
    token_hash = _hash_token(runner_token)
    with get_control_db() as database:
        row = database.execute(
            """
            SELECT id, user_id, noise_public_key
            FROM user_runners
            WHERE runner_token_hash = ? AND revoked_at IS NULL
            """,
            (token_hash,),
        ).fetchone()
    if row is None:
        raise RunnerAuthenticationError("Runner token is invalid")
    noise_public_key = row["noise_public_key"]
    if not isinstance(noise_public_key, str) or not noise_public_key:
        raise RunnerAuthenticationError("Runner Noise identity is unavailable")
    return {
        "runner_id": row["id"],
        "user_id": row["user_id"],
        "noise_public_key": noise_public_key,
    }


def record_runner_heartbeat(
    runner_token: str,
    *,
    runner_version: str,
    capabilities: dict[str, Any],
    data_dir: str,
    sqlite_dir: str | None,
    shared_files_dir: str | None,
    storage_profile: str,
) -> dict[str, Any]:
    """Record a heartbeat from a registered runner bearer token."""
    token_hash = _hash_token(runner_token)
    normalized_version = _require_non_empty_text(runner_version, "runner_version")
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

        stored_capabilities = _decode_capabilities(row["capabilities_json"])
        stored_profile = _requested_profile_matches(
            requested_profile=storage_profile,
            stored_capabilities=stored_capabilities,
        )
        merged_capabilities = _storage_capabilities(
            capabilities,
            data_dir=data_dir,
            sqlite_dir=sqlite_dir,
            shared_files_dir=shared_files_dir,
            storage_profile=stored_profile,
        )
        capabilities_text = _capabilities_json(merged_capabilities)
        normalized_data_dir = str(merged_capabilities["data_dir"])

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

    return {
        "runner_id": row["id"],
        "capability_signing_public_key": runner_capability_signing_public_key(),
        "status": "online",
    }
