"""Cloud runner agent for registration and encrypted restricted worker RPC."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.typing import Origin

from yinshi.runner_worker import (
    RunnerUserDataEncryptionMode,
    RunnerWorkerManager,
    validate_user_data_encryption_mode,
)
from yinshi.services.runner_agent_relay import (
    RunnerAgentRelayRuntime,
    RunnerRelaySessionError,
)
from yinshi.services.runner_data_key import load_or_create_runner_data_key
from yinshi.services.runner_noise import load_or_create_runner_noise_keypair
from yinshi.services.sprite_task_lease import SpriteTaskLease

logger = logging.getLogger(__name__)
RUNNER_VERSION = "0.2.0"
RunnerStorageProfile = Literal[
    "aws_ebs_s3_files",
    "archil_shared_files",
    "archil_all_posix",
    "fly_sprites_posix",
]
_DEFAULT_CONTROL_URL = "http://localhost:8000"
_DEFAULT_DATA_DIR = "/var/lib/yinshi"
_DEFAULT_SQLITE_DIR = f"{_DEFAULT_DATA_DIR}/sqlite"
_DEFAULT_SHARED_FILES_DIR = "/mnt/yinshi-s3-files"
_DEFAULT_FLY_SPRITES_SHARED_FILES_DIR = f"{_DEFAULT_DATA_DIR}/files"
_DEFAULT_ARCHIL_SHARED_FILES_DIR = "/mnt/archil/yinshi"
_DEFAULT_ARCHIL_SQLITE_DIR = f"{_DEFAULT_ARCHIL_SHARED_FILES_DIR}/sqlite"
_DEFAULT_TOKEN_FILE = "/var/lib/yinshi/runner-token"
_DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
_REQUEST_TIMEOUT_S = 15.0
_REGISTRATION_TOKEN_ENV_PREFIX = "YINSHI_REGISTRATION_TOKEN="
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_AWS_STORAGE_PROFILE: RunnerStorageProfile = "aws_ebs_s3_files"
_ARCHIL_SHARED_FILES_PROFILE: RunnerStorageProfile = "archil_shared_files"
_ARCHIL_ALL_POSIX_PROFILE: RunnerStorageProfile = "archil_all_posix"
_FLY_SPRITES_POSIX_PROFILE: RunnerStorageProfile = "fly_sprites_posix"
_STORAGE_ARCHIL = "archil"
_STORAGE_RUNNER_EBS = "runner_ebs"
_STORAGE_S3_FILES_OR_LOCAL_POSIX = "s3_files_or_local_posix"
_STORAGE_S3_FILES_MOUNT = "s3_files_mount"
_STORAGE_LOCAL_POSIX = "local_posix"


@dataclass(frozen=True, slots=True)
class RunnerStorageProfileSpec:
    """Environment defaults and validation rules for one runner storage profile."""

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


@dataclass(frozen=True, slots=True)
class RunnerAgentConfig:
    """Environment-derived configuration for the cloud runner agent."""

    control_url: str
    registration_token: str | None
    runner_token_file: Path
    noise_private_key_file: Path
    data_protection_key_file: Path
    capability_signing_key_file: Path
    replay_database_file: Path
    data_dir: Path
    sqlite_dir: Path
    shared_files_dir: Path
    storage_profile: RunnerStorageProfile
    sqlite_storage: str
    shared_files_storage: str | None
    user_data_encryption: RunnerUserDataEncryptionMode
    heartbeat_interval_s: float
    relay_idle_timeout_seconds: float | None
    sprite_task_lease: bool
    env_file: Path | None
    artifact_sha256: str | None = None
    artifact_attestation_file: Path | None = None


def _env_text(name: str, default: str | None = None) -> str | None:
    """Read and normalize an optional environment value."""
    value = os.environ.get(name, default)
    if value is None:
        return None
    normalized_value = value.strip()
    if not normalized_value:
        return None
    return normalized_value


def _env_user_data_encryption() -> RunnerUserDataEncryptionMode:
    """Read the dedicated runner user-data encryption setting."""
    env_name = "YINSHI_RUNNER_USER_DATA_ENCRYPTION"
    raw_value = _env_text(env_name, "disabled")
    if raw_value is None:
        raise RuntimeError(f"{env_name} must be disabled or required")
    try:
        return validate_user_data_encryption_mode(raw_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{env_name} must be disabled or required") from exc


def _env_float(name: str, default: float) -> float:
    """Read a positive float from the environment with explicit validation."""
    raw_value = _env_text(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _env_optional_positive_float(name: str) -> float | None:
    """Read an absent or positive finite float without accepting empty text."""
    raw_value = os.environ.get(name)
    if raw_value is None:
        return None
    try:
        value = float(raw_value.strip())
    except ValueError:
        raise RuntimeError(f"{name} must be a positive finite number") from None
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be a positive finite number")
    return value


def _env_disabled_or_enabled(name: str) -> bool:
    """Read one exact disabled or enabled feature setting."""
    raw_value = os.environ.get(name, "disabled").strip()
    if raw_value == "disabled":
        return False
    if raw_value == "enabled":
        return True
    raise RuntimeError(f"{name} must be disabled or enabled")


def _env_path(name: str, default: str) -> Path:
    """Read a required absolute filesystem path from the environment."""
    path_text = _env_text(name, default)
    if path_text is None:
        raise RuntimeError(f"{name} must not be empty")
    path = Path(path_text)
    if not path.is_absolute():
        raise RuntimeError(f"{name} must be an absolute path")
    if ".." in path.parts:
        raise RuntimeError(f"{name} must not contain parent directory references")
    return path


def _storage_profile_spec(storage_profile: str) -> RunnerStorageProfileSpec:
    """Return storage profile metadata after validating the profile value."""
    normalized_profile = storage_profile.strip()
    if not normalized_profile:
        raise RuntimeError("YINSHI_RUNNER_STORAGE_PROFILE must not be empty")
    if normalized_profile not in _STORAGE_PROFILES:
        raise RuntimeError(f"Unsupported YINSHI_RUNNER_STORAGE_PROFILE: {normalized_profile}")
    return _STORAGE_PROFILES[normalized_profile]


def _validate_storage_class(
    *,
    env_name: str,
    value: str | None,
    profile: RunnerStorageProfileSpec,
    expected_value: str,
    allowed_values: frozenset[str],
    required: bool,
) -> str | None:
    """Validate one storage-class environment value against the selected profile."""
    if value is None:
        if required:
            raise RuntimeError(f"{env_name} must be {expected_value} for {profile.value}")
        return None
    if value not in allowed_values:
        allowed_text = ", ".join(sorted(allowed_values))
        raise RuntimeError(f"{env_name} must be one of {allowed_text} for {profile.value}")
    return value


def _load_storage_profile() -> RunnerStorageProfileSpec:
    """Read the selected runner storage profile from the environment."""
    storage_profile = _env_text("YINSHI_RUNNER_STORAGE_PROFILE", _AWS_STORAGE_PROFILE)
    assert storage_profile is not None, "default storage profile must be non-empty"
    return _storage_profile_spec(storage_profile)


def load_config() -> RunnerAgentConfig:
    """Build runner agent config from environment variables."""
    control_url = _env_text("YINSHI_CONTROL_URL", _DEFAULT_CONTROL_URL)
    assert control_url is not None, "default control URL must be non-empty"
    profile = _load_storage_profile()
    sqlite_storage = _validate_storage_class(
        env_name="YINSHI_RUNNER_SQLITE_STORAGE",
        value=_env_text("YINSHI_RUNNER_SQLITE_STORAGE", profile.sqlite_storage),
        profile=profile,
        expected_value=profile.sqlite_storage,
        allowed_values=profile.allowed_sqlite_storage,
        required=profile.requires_explicit_storage,
    )
    assert sqlite_storage is not None, "SQLite storage has a profile default"
    shared_files_storage_default = (
        profile.shared_files_storage if profile.value == _FLY_SPRITES_POSIX_PROFILE else None
    )
    shared_files_storage = _validate_storage_class(
        env_name="YINSHI_RUNNER_SHARED_FILES_STORAGE",
        value=_env_text(
            "YINSHI_RUNNER_SHARED_FILES_STORAGE",
            shared_files_storage_default,
        ),
        profile=profile,
        expected_value=profile.shared_files_storage,
        allowed_values=profile.allowed_shared_files_storage,
        required=profile.requires_explicit_storage,
    )
    runner_token_file = _env_path("YINSHI_RUNNER_TOKEN_FILE", _DEFAULT_TOKEN_FILE)
    data_dir = _env_path("YINSHI_RUNNER_DATA_DIR", _DEFAULT_DATA_DIR)
    noise_private_key_file = _env_path(
        "YINSHI_RUNNER_NOISE_KEY_FILE",
        str(data_dir / "runner-noise.key"),
    )
    capability_signing_key_file = _env_path(
        "YINSHI_RUNNER_CAPABILITY_SIGNING_KEY_FILE",
        str(data_dir / "control-capability-signing.pub"),
    )
    replay_database_file = _env_path(
        "YINSHI_RUNNER_REPLAY_DATABASE_FILE",
        str(data_dir / "runner-capability-replay.sqlite3"),
    )
    sqlite_dir = _env_path("YINSHI_RUNNER_SQLITE_DIR", profile.default_sqlite_dir)
    data_protection_key_file = _env_path(
        "YINSHI_RUNNER_DATA_PROTECTION_KEY_FILE",
        str(sqlite_dir / ".yinshi-data-protection-key"),
    )
    if data_protection_key_file.parent != sqlite_dir:
        raise RuntimeError(
            "YINSHI_RUNNER_DATA_PROTECTION_KEY_FILE must be inside YINSHI_RUNNER_SQLITE_DIR"
        )
    shared_files_dir = _env_path(
        "YINSHI_RUNNER_SHARED_FILES_DIR",
        profile.default_shared_files_dir,
    )
    env_file_text = _env_text("YINSHI_RUNNER_ENV_FILE")
    env_file = Path(env_file_text) if env_file_text else None
    artifact_sha256 = _env_text("YINSHI_RUNNER_ARTIFACT_SHA256")
    artifact_attestation_text = _env_text("YINSHI_RUNNER_ARTIFACT_ATTESTATION_FILE")
    artifact_attestation_file = (
        Path(artifact_attestation_text) if artifact_attestation_text is not None else None
    )
    artifact_sha256 = _verify_artifact_attestation(
        profile.value,
        artifact_sha256,
        artifact_attestation_file,
    )
    relay_idle_timeout_seconds = _env_optional_positive_float(
        "YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS"
    )
    sprite_task_lease = _env_disabled_or_enabled("YINSHI_RUNNER_SPRITE_TASK_LEASE")
    if sprite_task_lease and profile.value != _FLY_SPRITES_POSIX_PROFILE:
        raise RuntimeError(
            "YINSHI_RUNNER_SPRITE_TASK_LEASE requires fly_sprites_posix storage profile"
        )
    if sprite_task_lease and relay_idle_timeout_seconds is None:
        raise RuntimeError(
            "YINSHI_RUNNER_SPRITE_TASK_LEASE requires " "YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS"
        )
    return RunnerAgentConfig(
        control_url=control_url.rstrip("/"),
        registration_token=_env_text("YINSHI_REGISTRATION_TOKEN"),
        runner_token_file=runner_token_file,
        noise_private_key_file=noise_private_key_file,
        data_protection_key_file=data_protection_key_file,
        capability_signing_key_file=capability_signing_key_file,
        replay_database_file=replay_database_file,
        data_dir=data_dir,
        sqlite_dir=sqlite_dir,
        shared_files_dir=shared_files_dir,
        storage_profile=profile.value,
        sqlite_storage=sqlite_storage,
        shared_files_storage=shared_files_storage,
        user_data_encryption=_env_user_data_encryption(),
        heartbeat_interval_s=_env_float(
            "YINSHI_RUNNER_HEARTBEAT_INTERVAL_S",
            _DEFAULT_HEARTBEAT_INTERVAL_S,
        ),
        relay_idle_timeout_seconds=relay_idle_timeout_seconds,
        sprite_task_lease=sprite_task_lease,
        env_file=env_file,
        artifact_sha256=artifact_sha256,
        artifact_attestation_file=artifact_attestation_file,
    )


def _probe_writable_directory(directory: Path, label: str) -> None:
    """Create and probe a POSIX directory required by the runner."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        directory_descriptor = os.open(directory, directory_flags)
    except OSError:
        raise RuntimeError(f"Runner {label} path is not a directory: {directory}") from None
    probe_name = f".yinshi-runner-write-check.{secrets.token_hex(16)}"
    failure_message = f"Runner {label} directory failed read-after-write check"
    probe_descriptor: int | None = None
    primary_error: BaseException | None = None
    cleanup_failed = False
    try:
        metadata = os.fstat(directory_descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"Runner {label} path is not a directory: {directory}")
        if metadata.st_uid != os.geteuid():
            raise RuntimeError(f"Runner {label} directory is not owned by the runner user")
        os.fchmod(directory_descriptor, 0o700)
        probe_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        probe_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        probe_descriptor = os.open(
            probe_name,
            probe_flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        expected = b"ok\n"
        if os.write(probe_descriptor, expected) != len(expected):
            raise RuntimeError(failure_message)
        os.fsync(probe_descriptor)
        os.lseek(probe_descriptor, 0, os.SEEK_SET)
        if os.read(probe_descriptor, len(expected) + 1) != expected:
            raise RuntimeError(failure_message)
    except OSError:
        primary_error = RuntimeError(failure_message)
    except BaseException as error:
        primary_error = error
    finally:
        if probe_descriptor is not None:
            try:
                os.close(probe_descriptor)
            except OSError:
                cleanup_failed = True
            try:
                os.unlink(probe_name, dir_fd=directory_descriptor)
            except OSError:
                cleanup_failed = True
        try:
            os.close(directory_descriptor)
        except OSError:
            cleanup_failed = True
    if primary_error is not None:
        raise primary_error from None
    if cleanup_failed:
        raise RuntimeError(failure_message)


def _shared_files_storage(shared_files_dir: Path) -> str:
    """Describe whether the shared file path is a mounted filesystem."""
    if shared_files_dir.is_mount():
        return _STORAGE_S3_FILES_MOUNT
    return _STORAGE_LOCAL_POSIX


def _validate_storage_layout(config: RunnerAgentConfig) -> RunnerStorageProfileSpec:
    """Reject path layouts that violate the selected storage profile."""
    profile = _storage_profile_spec(config.storage_profile)
    if profile.allow_sqlite_under_shared_files:
        return profile
    try:
        config.sqlite_dir.relative_to(config.shared_files_dir)
    except ValueError:
        return profile
    raise RuntimeError(
        "YINSHI_RUNNER_SQLITE_DIR must not live under YINSHI_RUNNER_SHARED_FILES_DIR"
    )


def _resolved_shared_files_storage(config: RunnerAgentConfig) -> str:
    """Return explicit shared storage, or detect AWS mount/local storage."""
    if config.shared_files_storage is not None:
        return config.shared_files_storage
    return _shared_files_storage(config.shared_files_dir)


def _capabilities(config: RunnerAgentConfig) -> dict[str, Any]:
    """Return storage and execution capabilities advertised to the control plane."""
    profile = _validate_storage_layout(config)
    _probe_writable_directory(config.data_dir, "data")
    _probe_writable_directory(config.sqlite_dir, "sqlite")
    _probe_writable_directory(config.shared_files_dir, "shared files")
    capabilities: dict[str, Any] = {
        "posix_storage": True,
        "sqlite": True,
        "git_worktrees": True,
        "pi_sidecar": True,
        "data_dir": str(config.data_dir),
        "sqlite_dir": str(config.sqlite_dir),
        "shared_files_dir": str(config.shared_files_dir),
        "storage_profile": profile.value,
        "storage_profile_experimental": profile.experimental,
        "sqlite_storage": config.sqlite_storage,
        "shared_files_storage": _resolved_shared_files_storage(config),
        "live_sqlite_on_shared_files": profile.live_sqlite_on_shared_files,
    }
    if config.artifact_sha256 is not None:
        capabilities["artifact_sha256"] = config.artifact_sha256
    return capabilities


def _read_owner_only_text_file(path: Path, label: str) -> str | None:
    """Read a regular owner-only text file without following symlinks."""
    if not path.exists() and not path.is_symlink():
        return None
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, os.O_RDONLY | no_follow)
    except OSError as exc:
        raise RuntimeError(f"{label} file could not be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"{label} file must be regular")
        if metadata.st_uid != os.geteuid():
            raise RuntimeError(f"{label} file must be owned by the runner user")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError(f"{label} file must have owner-only permissions")
        encoded_value = os.read(descriptor, 8_193)
    finally:
        os.close(descriptor)
    if len(encoded_value) > 8_192:
        raise RuntimeError(f"{label} file is too large")
    try:
        value = encoded_value.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{label} file must contain ASCII text") from exc
    if not value:
        raise RuntimeError(f"{label} file is empty")
    return value


def _verify_artifact_attestation(
    storage_profile: str,
    expected_sha256: str | None,
    attestation_file: Path | None,
) -> str | None:
    """Return artifact digest only after validating its private local attestation."""
    required = storage_profile == _FLY_SPRITES_POSIX_PROFILE
    if expected_sha256 is None:
        if required:
            raise RuntimeError("YINSHI_RUNNER_ARTIFACT_SHA256 is required for fly_sprites_posix")
        if attestation_file is not None:
            raise RuntimeError(
                "YINSHI_RUNNER_ARTIFACT_SHA256 is required when "
                "YINSHI_RUNNER_ARTIFACT_ATTESTATION_FILE is set"
            )
        return None
    if _SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise RuntimeError(
            "YINSHI_RUNNER_ARTIFACT_SHA256 must contain exactly "
            "64 lowercase hexadecimal characters"
        )
    if attestation_file is None:
        profile_suffix = " for fly_sprites_posix" if required else ""
        raise RuntimeError("YINSHI_RUNNER_ARTIFACT_ATTESTATION_FILE is required" + profile_suffix)
    if not attestation_file.is_absolute():
        raise RuntimeError("YINSHI_RUNNER_ARTIFACT_ATTESTATION_FILE must be an absolute path")
    if ".." in attestation_file.parts:
        raise RuntimeError(
            "YINSHI_RUNNER_ARTIFACT_ATTESTATION_FILE must not contain "
            "parent directory references"
        )
    attested_sha256 = _read_owner_only_text_file(
        attestation_file,
        "Runner artifact attestation",
    )
    if attested_sha256 is None:
        raise RuntimeError("Runner artifact attestation file is missing")
    if _SHA256_PATTERN.fullmatch(attested_sha256) is None:
        raise RuntimeError(
            "Runner artifact attestation must contain exactly "
            "64 lowercase hexadecimal characters"
        )
    if not secrets.compare_digest(attested_sha256, expected_sha256):
        raise RuntimeError("Runner artifact attestation does not match expected SHA-256")
    return expected_sha256


def _write_owner_only_text_file(path: Path, value: str, label: str) -> None:
    """Atomically persist one owner-only ASCII value."""
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} must not be empty")
    try:
        encoded_value = f"{value.strip()}\n".encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError(f"{label} must contain ASCII text") from exc
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow,
        0o600,
    )
    try:
        written = os.write(descriptor, encoded_value)
        if written != len(encoded_value):
            raise RuntimeError(f"{label} write was incomplete")
        os.fsync(descriptor)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    os.replace(temporary_path, path)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor = os.open(path.parent, directory_flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _read_runner_token(token_file: Path) -> str | None:
    """Read a previously issued runner bearer token from owner-only storage."""
    return _read_owner_only_text_file(token_file, "Runner token")


def _write_runner_token(token_file: Path, runner_token: str) -> None:
    """Persist the runner bearer token with owner-only permissions."""
    _write_owner_only_text_file(token_file, runner_token, "Runner token")


def _validate_capability_signing_public_key(value: object) -> str:
    """Return a canonical raw Ed25519 public key from a control response."""
    if not isinstance(value, str) or not value:
        raise RuntimeError("Control response did not include a capability signing key")
    try:
        key_bytes = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("Control capability signing key is not valid base64url") from exc
    canonical_value = base64.urlsafe_b64encode(key_bytes).rstrip(b"=").decode("ascii")
    if canonical_value != value or len(key_bytes) != 32:
        raise RuntimeError("Control capability signing key is not a canonical 32-byte key")
    return canonical_value


def _pin_capability_signing_key(path: Path, value: object) -> str:
    """Persist first control key and reject every later key change."""
    validated_value = _validate_capability_signing_public_key(value)
    existing_value = _read_owner_only_text_file(path, "Control capability signing key")
    if existing_value is None:
        _write_owner_only_text_file(path, validated_value, "Control capability signing key")
        return validated_value
    if not secrets.compare_digest(existing_value, validated_value):
        raise RuntimeError("Control capability signing key changed; runner re-pairing is required")
    return existing_value


def _scrub_registration_token(env_file: Path | None) -> None:
    """Remove the consumed one-time token from the systemd environment file."""
    if env_file is None:
        return
    if not env_file.exists():
        return
    lines = env_file.read_text(encoding="utf-8").splitlines()
    filtered_lines = [line for line in lines if not line.startswith(_REGISTRATION_TOKEN_ENV_PREFIX)]
    if filtered_lines == lines:
        return
    env_file.write_text("\n".join(filtered_lines) + "\n", encoding="utf-8")
    env_file.chmod(0o600)


def _runner_status_payload(config: RunnerAgentConfig) -> dict[str, Any]:
    """Build the runner status fields shared by registration and heartbeats."""
    return {
        "runner_version": RUNNER_VERSION,
        "capabilities": _capabilities(config),
        "data_dir": str(config.data_dir),
        "sqlite_dir": str(config.sqlite_dir),
        "shared_files_dir": str(config.shared_files_dir),
        "storage_profile": config.storage_profile,
    }


def _runner_registration_payload(config: RunnerAgentConfig) -> dict[str, Any]:
    """Build registration fields including the persistent Noise responder identity."""
    if config.registration_token is None:
        raise RuntimeError("YINSHI_REGISTRATION_TOKEN is required until a runner token file exists")
    noise_keypair = load_or_create_runner_noise_keypair(config.noise_private_key_file)
    return {
        "registration_token": config.registration_token,
        "noise_public_key": noise_keypair.public_key_base64url,
        **_runner_status_payload(config),
    }


async def _register(config: RunnerAgentConfig, client: httpx.AsyncClient) -> str:
    """Register this runner and return the issued bearer token."""
    payload = _runner_registration_payload(config)
    response = await client.post("/runner/register", json=payload)
    response.raise_for_status()
    body = response.json()
    runner_token = body.get("runner_token")
    if not isinstance(runner_token, str) or not runner_token.strip():
        raise RuntimeError("Runner registration response did not include a bearer token")
    _pin_capability_signing_key(
        config.capability_signing_key_file,
        body.get("capability_signing_public_key"),
    )
    _write_runner_token(config.runner_token_file, runner_token)
    _scrub_registration_token(config.env_file)
    logger.info("Registered Yinshi cloud runner")
    return runner_token


async def _heartbeat(
    config: RunnerAgentConfig,
    client: httpx.AsyncClient,
    runner_token: str,
) -> None:
    """Send one heartbeat to the control plane."""
    payload = _runner_status_payload(config)
    response = await client.post(
        "/runner/heartbeat",
        json=payload,
        headers={"Authorization": f"Bearer {runner_token}"},
    )
    response.raise_for_status()
    body = response.json()
    _pin_capability_signing_key(
        config.capability_signing_key_file,
        body.get("capability_signing_public_key"),
    )
    logger.info("Heartbeat accepted for Yinshi cloud runner")


def _runner_relay_url(control_url: str) -> str:
    """Convert one validated HTTP control origin to its WebSocket relay URL."""
    parsed_url = urlsplit(control_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("YINSHI_CONTROL_URL must be an HTTP or HTTPS origin")
    if parsed_url.path not in {"", "/"} or parsed_url.query or parsed_url.fragment:
        raise ValueError("YINSHI_CONTROL_URL must not include a path, query, or fragment")
    websocket_scheme = "wss" if parsed_url.scheme == "https" else "ws"
    return urlunsplit((websocket_scheme, parsed_url.netloc, "/runner/relay", "", ""))


def _runner_relay_runtime(
    config: RunnerAgentConfig,
    worker_manager: RunnerWorkerManager,
    task_lease: SpriteTaskLease | None,
) -> RunnerAgentRelayRuntime:
    """Load pinned key material for one fresh relay connection."""
    noise_keypair = load_or_create_runner_noise_keypair(config.noise_private_key_file)
    signing_key_text = _read_owner_only_text_file(
        config.capability_signing_key_file,
        "Control capability signing key",
    )
    if signing_key_text is None:
        raise RuntimeError("Control capability signing key is missing")
    validated_signing_key = _validate_capability_signing_public_key(signing_key_text)
    signing_key_bytes = base64.urlsafe_b64decode(validated_signing_key + "=")
    assert len(signing_key_bytes) == 32
    return RunnerAgentRelayRuntime(
        runner_static_private_key=noise_keypair.private_key,
        capability_signing_public_key=signing_key_bytes,
        replay_database_path=config.replay_database_file,
        dispatcher_factory=worker_manager.dispatcher,
        task_lease=task_lease,
        maintenance_handler=worker_manager.quiesce,
    )


async def _consume_runner_relay_messages(
    runtime: RunnerAgentRelayRuntime,
    websocket: ClientConnection,
    *,
    idle_timeout_seconds: float | None = None,
) -> bool:
    """Apply relay messages until transport closure or managed idle expiry."""
    try:
        while True:
            try:
                if idle_timeout_seconds is not None and not runtime.active_transfer_ids:
                    message = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=idle_timeout_seconds,
                    )
                else:
                    message = await websocket.recv()
            except TimeoutError:
                return True
            if isinstance(message, str):
                acknowledgement = await runtime.handle_control(message)
                if acknowledgement is not None:
                    await websocket.send(acknowledgement)
                continue
            try:
                response = await runtime.handle_binary(
                    bytes(message),
                    current_time=int(time.time()),
                )
            except RunnerRelaySessionError as error:
                await websocket.send(
                    json.dumps(
                        {"transfer_id": error.transfer_id, "type": "close"},
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                continue
            await websocket.send(response)
    except (RuntimeError, TypeError, ValueError):
        await websocket.close(code=1008, reason="Runner relay frame rejected")
        raise RuntimeError("Runner relay protocol rejected a frame") from None


async def _serve_runner_relay_connection(
    config: RunnerAgentConfig,
    runner_token: str,
    worker_manager: RunnerWorkerManager,
    task_lease: SpriteTaskLease | None,
) -> bool:
    """Serve one outbound relay connection until transport closure or idle expiry."""
    runtime = _runner_relay_runtime(config, worker_manager, task_lease)
    try:
        async with connect(
            _runner_relay_url(config.control_url),
            origin=Origin(config.control_url.rstrip("/")),
            compression=None,
            additional_headers={"Authorization": f"Bearer {runner_token}"},
            proxy=True,
            open_timeout=_REQUEST_TIMEOUT_S,
            ping_interval=20.0,
            ping_timeout=20.0,
            close_timeout=5.0,
            max_size=65_551,
            max_queue=16,
        ) as websocket:
            return await _consume_runner_relay_messages(
                runtime,
                websocket,
                idle_timeout_seconds=config.relay_idle_timeout_seconds,
            )
    finally:
        await runtime.aclose()


async def _runner_relay_loop(config: RunnerAgentConfig, runner_token: str) -> None:
    """Reconnect the outbound relay with bounded backoff until cancelled."""
    task_lease = SpriteTaskLease() if config.sprite_task_lease else None
    try:
        noise_keypair = load_or_create_runner_noise_keypair(config.noise_private_key_file)
        data_protection_key = load_or_create_runner_data_key(
            config.data_protection_key_file,
            config.sqlite_dir,
            noise_keypair.private_key,
        )
        worker_manager = RunnerWorkerManager(
            data_directory=config.data_dir / "worker-runtime",
            database_directory=config.sqlite_dir,
            user_data_directory=config.shared_files_dir / "users",
            data_protection_key=data_protection_key,
            user_data_encryption=config.user_data_encryption,
        )
        reconnect_delay_seconds = 1.0
        while True:
            try:
                idle_expired = await _serve_runner_relay_connection(
                    config,
                    runner_token,
                    worker_manager,
                    task_lease,
                )
                if idle_expired:
                    return
                reconnect_delay_seconds = 1.0
            except InvalidStatus as error:
                status_code = error.response.status_code
                if status_code in {401, 403}:
                    raise RuntimeError("Runner relay authentication was rejected") from None
                logger.warning("Runner relay unavailable with HTTP status %s", status_code)
            except (ConnectionClosed, OSError, TimeoutError):
                logger.warning("Runner relay connection unavailable; retrying")
            await asyncio.sleep(reconnect_delay_seconds)
            reconnect_delay_seconds = min(reconnect_delay_seconds * 2, 30.0)
    finally:
        if task_lease is not None:
            await task_lease.aclose()


async def _heartbeat_loop(
    config: RunnerAgentConfig,
    client: httpx.AsyncClient,
    runner_token: str,
) -> None:
    """Send recurring authenticated heartbeats until cancelled or revoked."""
    while True:
        try:
            await _heartbeat(config, client, runner_token)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise RuntimeError("Runner token was rejected by the control plane") from exc
            raise
        await asyncio.sleep(config.heartbeat_interval_s)


async def run_agent(config: RunnerAgentConfig) -> None:
    """Run heartbeats and outbound encrypted relay until either fails."""
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
    async with httpx.AsyncClient(
        base_url=config.control_url,
        timeout=_REQUEST_TIMEOUT_S,
        limits=limits,
        follow_redirects=False,
    ) as client:
        runner_token = _read_runner_token(config.runner_token_file)
        if runner_token is None:
            runner_token = await _register(config, client)
        else:
            pinned_key = _read_owner_only_text_file(
                config.capability_signing_key_file,
                "Control capability signing key",
            )
            if pinned_key is None:
                raise RuntimeError(
                    "Control capability signing key is missing; runner re-registration is required"
                )
            _validate_capability_signing_public_key(pinned_key)

        logger.info("Runner Noise identity loaded")
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(config, client, runner_token),
            name="runner-heartbeat",
        )
        relay_task = asyncio.create_task(
            _runner_relay_loop(config, runner_token),
            name="runner-relay",
        )
        tasks = (heartbeat_task, relay_task)
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in tasks:
                if task in done:
                    task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    """Load configuration and run the cloud runner agent."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    config = load_config()
    logger.info(
        "Starting Yinshi cloud runner agent against %s with profile %s",
        config.control_url,
        config.storage_profile,
    )
    asyncio.run(run_agent(config))


if __name__ == "__main__":
    main()
