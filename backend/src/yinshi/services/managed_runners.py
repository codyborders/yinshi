"""Atomic lifecycle state for managed runner infrastructure."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast, get_args

from yinshi.db import get_control_db
from yinshi.services.runners import (
    _HEARTBEAT_ONLINE_WINDOW_SECONDS,
    _create_runner_registration_in_connection,
    _datetime_from_storage,
    _datetime_to_storage,
    _decode_capabilities,
    _require_user_id,
)

ManagedRuntimeLifecycleStatus = Literal["provisioning", "ready", "failed", "deleting"]
ManagedRuntimeErrorCode = Literal[
    "artifact_invalid",
    "provider_unavailable",
    "network_policy_failed",
    "bootstrap_failed",
    "runner_registration_failed",
    "runner_identity_changed",
    "wake_timeout",
    "checkpoint_failed",
    "delete_failed",
]

_MANAGED_RUNTIME_ERROR_CODES = frozenset(get_args(ManagedRuntimeErrorCode))
_SPRITE_PREFIX_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$")
_SPRITE_DIGEST_LENGTH = 32
_PROVISIONING_STALE_AFTER = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class ManagedRuntimeStatus:
    """Safe persisted status for one user's managed runtime."""

    user_id: str
    runner_id: str
    provider_name: Literal["fly_sprites"]
    sprite_name: str
    lifecycle_status: ManagedRuntimeLifecycleStatus
    generation: int
    artifact_version: str
    created_at: str
    updated_at: str
    last_error: ManagedRuntimeErrorCode | None


@dataclass(frozen=True, slots=True)
class ProvisioningClaimResult:
    """Provisioning ownership plus authority returned only to claim owners."""

    claimed: bool
    runtime: ManagedRuntimeStatus
    registration_token: str | None = None
    registration_token_expires_at: str | None = None
    control_url: str | None = None
    environment: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class DeletionClaimResult:
    """Deletion ownership and persisted runtime state."""

    claimed: bool
    runtime: ManagedRuntimeStatus


ManagedRuntimeProvisioningClaim = ProvisioningClaimResult


def managed_sprite_name(user_id: str, *, prefix: str, secret_key: str) -> str:
    """Return one deterministic provider-safe name without exposing user identity."""
    normalized_user_id = _require_user_id(user_id)
    if not isinstance(prefix, str) or _SPRITE_PREFIX_PATTERN.fullmatch(prefix) is None:
        raise ValueError("prefix must be a lowercase DNS label of 1 to 30 characters")
    if not isinstance(secret_key, str) or not secret_key:
        raise ValueError("secret_key must not be empty")
    digest = hmac.new(
        secret_key.encode("utf-8"),
        normalized_user_id.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    encoded_digest = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return f"{prefix}-{encoded_digest[:_SPRITE_DIGEST_LENGTH]}"


def _normalized_now(now: datetime | None) -> datetime:
    """Return injected or current UTC time at stable second precision."""
    value = now or datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime")
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _required_text(value: str, name: str) -> str:
    """Return normalized required text."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _runtime_status(row: sqlite3.Row) -> ManagedRuntimeStatus:
    """Convert one runtime row to its safe typed representation."""
    return ManagedRuntimeStatus(
        user_id=row["user_id"],
        runner_id=row["runner_id"],
        provider_name=cast(Literal["fly_sprites"], row["provider_name"]),
        sprite_name=row["sprite_external_id"],
        lifecycle_status=cast(ManagedRuntimeLifecycleStatus, row["lifecycle_status"]),
        generation=row["generation"],
        artifact_version=row["artifact_version"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_error=cast(ManagedRuntimeErrorCode | None, row["last_error"]),
    )


def activate_managed_restore_candidate(
    user_id: str,
    *,
    source_generation: int,
    candidate_runner_id: str,
    candidate_sprite_id: str,
    artifact_version: str,
    now: datetime,
    job_id: str | None = None,
    lease_token: str | None = None,
) -> bool:
    """Atomically promote one replacement runner and revoke old authority."""
    normalized_user_id = _require_user_id(user_id)
    if type(source_generation) is not int or source_generation <= 0:
        raise ValueError("source_generation must be a positive integer")
    candidate_runner = _required_text(candidate_runner_id, "candidate_runner_id")
    candidate_sprite = _required_text(candidate_sprite_id, "candidate_sprite_id")
    artifact = _required_text(artifact_version, "artifact_version")
    normalized_job_id = None if job_id is None else _required_text(job_id, "job_id")
    normalized_lease_token = (
        None if lease_token is None else _required_text(lease_token, "lease_token")
    )
    if (normalized_job_id is None) != (normalized_lease_token is None):
        raise ValueError("job_id and lease_token must be supplied together")
    now_text = _datetime_to_storage(_normalized_now(now)).replace("+00:00", "Z")
    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            runtime = database.execute(
                """SELECT runner_id FROM managed_runtimes
                   WHERE user_id = ? AND generation = ? AND lifecycle_status = 'ready'""",
                (normalized_user_id, source_generation),
            ).fetchone()
            candidate = database.execute(
                """SELECT id FROM user_runners
                   WHERE id = ? AND user_id = ? AND kind = 'managed_restore'
                     AND status = 'online' AND runner_token_hash IS NOT NULL
                     AND registered_at IS NOT NULL AND last_heartbeat_at IS NOT NULL
                     AND noise_public_key IS NOT NULL
                     AND noise_public_key_confirmed_at IS NOT NULL
                     AND revoked_at IS NULL""",
                (candidate_runner, normalized_user_id),
            ).fetchone()
            if runtime is None or candidate is None:
                database.rollback()
                return False
            if normalized_job_id is not None:
                operation = database.execute(
                    """SELECT 1 FROM managed_backup_operations
                       WHERE user_id = ? AND job_id = ? AND operation = 'restore'
                         AND status = 'running' AND runtime_generation = ?
                         AND lease_token = ? AND lease_expires_at > ?""",
                    (
                        normalized_user_id,
                        normalized_job_id,
                        source_generation,
                        normalized_lease_token,
                        now_text,
                    ),
                ).fetchone()
                if operation is None:
                    database.rollback()
                    return False
            old_runner_id = runtime["runner_id"]
            database.execute(
                """UPDATE user_runners
                   SET status = 'revoked', revoked_at = ?, registration_token_hash = NULL,
                       registration_token_expires_at = NULL, runner_token_hash = NULL
                   WHERE id = ? AND user_id = ? AND kind = 'managed'""",
                (now_text, old_runner_id, normalized_user_id),
            )
            if normalized_job_id is None or normalized_lease_token is None:
                database.rollback()
                return False
            database.execute(
                """INSERT INTO managed_runtime_activation_guards (
                       user_id, job_id, lease_token
                   ) VALUES (?, ?, ?)""",
                (normalized_user_id, normalized_job_id, normalized_lease_token),
            )
            database.execute(
                "UPDATE user_runners SET kind = 'managed_retired' WHERE id = ?",
                (old_runner_id,),
            )
            database.execute(
                "UPDATE user_runners SET kind = 'managed' WHERE id = ?",
                (candidate_runner,),
            )
            result = database.execute(
                """UPDATE managed_runtimes
                   SET runner_id = ?, sprite_external_id = ?, generation = ?,
                       artifact_version = ?, updated_at = ?, last_error = NULL
                   WHERE user_id = ? AND generation = ? AND lifecycle_status = 'ready'""",
                (
                    candidate_runner,
                    candidate_sprite,
                    source_generation + 1,
                    artifact,
                    now_text,
                    normalized_user_id,
                    source_generation,
                ),
            )
            if result.rowcount != 1:
                database.rollback()
                return False
            operation_result = database.execute(
                """UPDATE managed_backup_operations
                   SET phase = 'activated', activation_generation = ?, updated_at = ?
                   WHERE user_id = ? AND job_id = ? AND operation = 'restore'
                     AND status = 'running' AND runtime_generation = ?
                     AND lease_token = ? AND lease_expires_at > ?""",
                (
                    source_generation + 1,
                    now_text,
                    normalized_user_id,
                    normalized_job_id,
                    source_generation,
                    normalized_lease_token,
                    now_text,
                ),
            )
            if operation_result.rowcount != 1:
                database.rollback()
                return False
            database.execute(
                "DELETE FROM managed_runtime_activation_guards WHERE user_id = ?",
                (normalized_user_id,),
            )
            database.commit()
            return True
        except sqlite3.IntegrityError:
            database.rollback()
            return False
        except Exception:
            database.rollback()
            raise


def get_managed_runtime_status(user_id: str) -> ManagedRuntimeStatus | None:
    """Return safe persisted state without registration authority."""
    normalized_user_id = _require_user_id(user_id)
    with get_control_db() as database:
        row = database.execute(
            "SELECT * FROM managed_runtimes WHERE user_id = ?",
            (normalized_user_id,),
        ).fetchone()
    if row is None:
        return None
    return _runtime_status(row)


def claim_managed_runtime_deletion(
    user_id: str,
    now: datetime,
) -> DeletionClaimResult | None:
    """Claim deletion and revoke linked managed runner authority atomically."""
    normalized_user_id = _require_user_id(user_id)
    current_time = _normalized_now(now)
    now_text = _datetime_to_storage(current_time).replace("+00:00", "Z")

    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            maintenance = database.execute(
                """SELECT 1 FROM managed_backup_operations
                   WHERE user_id = ? AND status = 'running'""",
                (normalized_user_id,),
            ).fetchone()
            if maintenance is not None:
                raise RuntimeError("managed runtime maintenance is active")
            row = database.execute(
                """
                SELECT runtime.*, runner.kind AS runner_kind
                FROM managed_runtimes AS runtime
                JOIN user_runners AS runner ON runner.id = runtime.runner_id
                WHERE runtime.user_id = ?
                """,
                (normalized_user_id,),
            ).fetchone()
            if row is None:
                database.commit()
                return None
            if row["lifecycle_status"] == "deleting":
                database.commit()
                return DeletionClaimResult(claimed=False, runtime=_runtime_status(row))

            runtime_result = database.execute(
                """
                INSERT OR REPLACE INTO managed_runtimes (
                    user_id, runner_id, provider_name, sprite_external_id,
                    lifecycle_status, generation, artifact_version,
                    created_at, updated_at, last_error
                )
                SELECT user_id, runner_id, provider_name, sprite_external_id,
                       'deleting', generation + 1, artifact_version,
                       created_at, ?, NULL
                FROM managed_runtimes
                WHERE user_id = ? AND lifecycle_status != 'deleting'
                """,
                (now_text, normalized_user_id),
            )
            runner_result = database.execute(
                """
                UPDATE user_runners
                SET status = 'revoked', revoked_at = ?,
                    registration_token_hash = NULL,
                    registration_token_expires_at = NULL,
                    runner_token_hash = NULL
                WHERE id = ? AND user_id = ? AND kind = 'managed'
                """,
                (now_text, row["runner_id"], normalized_user_id),
            )
            if runtime_result.rowcount != 1 or runner_result.rowcount != 1:
                raise RuntimeError("managed runtime deletion claim was not stored")
            claimed_row = database.execute(
                "SELECT * FROM managed_runtimes WHERE user_id = ?",
                (normalized_user_id,),
            ).fetchone()
            assert claimed_row is not None, "managed runtime deletion claim must exist"
            database.commit()
            return DeletionClaimResult(claimed=True, runtime=_runtime_status(claimed_row))
        except Exception:
            database.rollback()
            raise


def finalize_managed_runtime_deletion(user_id: str, generation: int) -> bool:
    """Delete one matching deleting runtime followed by its managed runner."""
    normalized_user_id = _require_user_id(user_id)
    if type(generation) is not int or generation <= 0:
        raise ValueError("generation must be a positive integer")

    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                """
                SELECT runner_id
                FROM managed_runtimes
                WHERE user_id = ? AND generation = ?
                  AND lifecycle_status = 'deleting'
                """,
                (normalized_user_id, generation),
            ).fetchone()
            if row is None:
                database.rollback()
                return False
            runtime_result = database.execute(
                """
                DELETE FROM managed_runtimes
                WHERE user_id = ? AND generation = ?
                  AND lifecycle_status = 'deleting'
                """,
                (normalized_user_id, generation),
            )
            runner_result = database.execute(
                """
                DELETE FROM user_runners
                WHERE id = ? AND user_id = ? AND kind = 'managed'
                """,
                (row["runner_id"], normalized_user_id),
            )
            if runtime_result.rowcount != 1 or runner_result.rowcount != 1:
                raise RuntimeError("managed runtime deletion was not finalized")
            database.commit()
            return True
        except Exception:
            database.rollback()
            raise


def refresh_managed_runtime_provisioning(
    user_id: str,
    generation: int,
    now: datetime,
) -> bool:
    """Refresh one current provisioning generation's ownership timestamp."""
    normalized_user_id = _require_user_id(user_id)
    if type(generation) is not int or generation <= 0:
        raise ValueError("generation must be a positive integer")
    current_time = _normalized_now(now)
    now_text = _datetime_to_storage(current_time).replace("+00:00", "Z")

    with get_control_db() as database:
        result = database.execute(
            """
            INSERT OR REPLACE INTO managed_runtimes (
                user_id, runner_id, provider_name, sprite_external_id,
                lifecycle_status, generation, artifact_version,
                created_at, updated_at, last_error
            )
            SELECT user_id, runner_id, provider_name, sprite_external_id,
                   lifecycle_status, generation, artifact_version,
                   created_at, ?, last_error
            FROM managed_runtimes
            WHERE user_id = ? AND generation = ?
              AND lifecycle_status = 'provisioning'
            """,
            (now_text, normalized_user_id, generation),
        )
        database.commit()
        return result.rowcount == 1


def mark_managed_runtime_ready(
    user_id: str,
    generation: int,
    now: datetime,
) -> bool:
    """Mark one current provisioning generation ready after runner validation."""
    normalized_user_id = _require_user_id(user_id)
    if type(generation) is not int or generation <= 0:
        raise ValueError("generation must be a positive integer")
    current_time = _normalized_now(now)
    now_text = _datetime_to_storage(current_time).replace("+00:00", "Z")

    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                """
                SELECT runtime.lifecycle_status, runtime.generation,
                       runtime.provider_name, runner.kind,
                       runner.cloud_provider, runner.status AS runner_status,
                       runner.last_heartbeat_at, runner.capabilities_json,
                       runner.noise_public_key,
                       runner.noise_public_key_confirmed_at
                FROM managed_runtimes AS runtime
                JOIN user_runners AS runner ON runner.id = runtime.runner_id
                WHERE runtime.user_id = ?
                """,
                (normalized_user_id,),
            ).fetchone()
            heartbeat_at = None if row is None else _datetime_from_storage(row["last_heartbeat_at"])
            capabilities = {} if row is None else _decode_capabilities(row["capabilities_json"])
            heartbeat_age = (
                None if heartbeat_at is None else (current_time - heartbeat_at).total_seconds()
            )
            heartbeat_is_current = (
                heartbeat_age is not None and 0 <= heartbeat_age <= _HEARTBEAT_ONLINE_WINDOW_SECONDS
            )
            if (
                row is None
                or row["lifecycle_status"] != "provisioning"
                or row["generation"] != generation
                or row["provider_name"] != "fly_sprites"
                or row["kind"] != "managed"
                or row["cloud_provider"] != "fly_sprites"
                or row["runner_status"] != "online"
                or not heartbeat_is_current
                or capabilities.get("storage_profile") != "fly_sprites_posix"
                or not row["noise_public_key"]
                or row["noise_public_key_confirmed_at"] is None
            ):
                database.rollback()
                return False
            result = database.execute(
                """
                INSERT OR REPLACE INTO managed_runtimes (
                    user_id, runner_id, provider_name, sprite_external_id,
                    lifecycle_status, generation, artifact_version,
                    created_at, updated_at, last_error
                )
                SELECT user_id, runner_id, provider_name, sprite_external_id,
                       'ready', generation, artifact_version,
                       created_at, ?, NULL
                FROM managed_runtimes
                WHERE user_id = ? AND generation = ?
                  AND lifecycle_status = 'provisioning'
                """,
                (now_text, normalized_user_id, generation),
            )
            database.commit()
            return result.rowcount == 1
        except Exception:
            database.rollback()
            raise


def reconcile_managed_runtime_provisioning(
    active_owners: set[tuple[str, int]],
    now: datetime,
) -> int:
    """Fail abandoned provisioning while preserving current process ownership."""
    current_time = _normalized_now(now)
    now_text = _datetime_to_storage(current_time).replace("+00:00", "Z")

    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute("""
                SELECT user_id, runner_id, generation
                FROM managed_runtimes
                WHERE lifecycle_status = 'provisioning'
                """).fetchall()
            changed = 0
            for row in rows:
                owner = (row["user_id"], row["generation"])
                if owner in active_owners:
                    continue
                runtime_result = database.execute(
                    """
                    UPDATE managed_runtimes
                    SET lifecycle_status = 'failed', updated_at = ?,
                        last_error = 'provider_unavailable'
                    WHERE user_id = ? AND generation = ?
                      AND lifecycle_status = 'provisioning'
                    """,
                    (now_text, row["user_id"], row["generation"]),
                )
                if runtime_result.rowcount != 1:
                    continue
                runner_result = database.execute(
                    """
                    UPDATE user_runners
                    SET status = 'revoked', revoked_at = ?,
                        registration_token_hash = NULL,
                        registration_token_expires_at = NULL,
                        runner_token_hash = NULL
                    WHERE id = ? AND user_id = ? AND kind = 'managed'
                    """,
                    (now_text, row["runner_id"], row["user_id"]),
                )
                if runner_result.rowcount != 1:
                    raise RuntimeError("managed runtime reconciliation was not stored")
                changed += 1
            database.commit()
            return changed
        except Exception:
            database.rollback()
            raise


def mark_managed_runtime_failed(
    user_id: str,
    generation: int,
    error_code: str,
    now: datetime,
) -> bool:
    """Fail one current provisioning generation and revoke its runner authority."""
    normalized_user_id = _require_user_id(user_id)
    if type(generation) is not int or generation <= 0:
        raise ValueError("generation must be a positive integer")
    if error_code not in _MANAGED_RUNTIME_ERROR_CODES:
        raise ValueError("error_code is not an allowed managed runtime failure code")
    current_time = _normalized_now(now)
    now_text = _datetime_to_storage(current_time).replace("+00:00", "Z")

    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                """
                SELECT runtime.*, runner.kind AS runner_kind
                FROM managed_runtimes AS runtime
                JOIN user_runners AS runner ON runner.id = runtime.runner_id
                WHERE runtime.user_id = ?
                """,
                (normalized_user_id,),
            ).fetchone()
            if (
                row is None
                or row["lifecycle_status"] != "provisioning"
                or row["generation"] != generation
                or row["runner_kind"] != "managed"
            ):
                database.rollback()
                return False
            runtime_result = database.execute(
                """
                INSERT OR REPLACE INTO managed_runtimes (
                    user_id, runner_id, provider_name, sprite_external_id,
                    lifecycle_status, generation, artifact_version,
                    created_at, updated_at, last_error
                )
                SELECT user_id, runner_id, provider_name, sprite_external_id,
                       'failed', generation, artifact_version,
                       created_at, ?, ?
                FROM managed_runtimes
                WHERE user_id = ? AND generation = ?
                  AND lifecycle_status = 'provisioning'
                """,
                (now_text, error_code, normalized_user_id, generation),
            )
            runner_result = database.execute(
                """
                UPDATE user_runners
                SET status = 'revoked', revoked_at = ?,
                    registration_token_hash = NULL,
                    registration_token_expires_at = NULL,
                    runner_token_hash = NULL
                WHERE id = ? AND user_id = ? AND kind = 'managed'
                """,
                (now_text, row["runner_id"], normalized_user_id),
            )
            if runtime_result.rowcount != 1 or runner_result.rowcount != 1:
                raise RuntimeError("managed runtime failure transition was not stored")
            database.commit()
            return True
        except Exception:
            database.rollback()
            raise


def _observer(row: sqlite3.Row) -> ProvisioningClaimResult:
    """Return runtime state without bearer authority."""
    return ProvisioningClaimResult(claimed=False, runtime=_runtime_status(row))


def _owner(row: sqlite3.Row, registration: dict[str, Any]) -> ProvisioningClaimResult:
    """Return runtime state plus newly generated registration authority."""
    return ProvisioningClaimResult(
        claimed=True,
        runtime=_runtime_status(row),
        registration_token=registration["registration_token"],
        registration_token_expires_at=registration["registration_token_expires_at"],
        control_url=registration["control_url"],
        environment=registration["environment"],
    )


def claim_managed_runtime_provisioning(
    user_id: str,
    *,
    name_prefix: str,
    name_key: str,
    artifact_version: str,
    region: str,
    control_url: str,
    allow_upgrade: bool = False,
    now: datetime | None = None,
    stale_after: timedelta = _PROVISIONING_STALE_AFTER,
) -> ProvisioningClaimResult:
    """Claim provisioning with registration state in one immediate transaction."""
    if type(allow_upgrade) is not bool:
        raise TypeError("allow_upgrade must be a boolean")
    normalized_user_id = _require_user_id(user_id)
    normalized_artifact = _required_text(artifact_version, "artifact_version")
    normalized_region = _required_text(region, "region")
    current_time = _normalized_now(now)
    if not isinstance(stale_after, timedelta) or stale_after.total_seconds() <= 0:
        raise ValueError("stale_after must be a positive timedelta")
    generated_name = managed_sprite_name(
        normalized_user_id,
        prefix=name_prefix,
        secret_key=name_key,
    )
    now_text = _datetime_to_storage(current_time)

    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT * FROM managed_runtimes WHERE user_id = ?",
                (normalized_user_id,),
            ).fetchone()
            if row is not None:
                row_time = _datetime_from_storage(row["updated_at"])
                assert row_time is not None, "managed runtime timestamp must exist"
                is_stale = current_time - row_time >= stale_after
                should_claim = row["lifecycle_status"] == "failed" or (
                    row["lifecycle_status"] == "provisioning" and is_stale
                )
                should_claim = should_claim or (
                    allow_upgrade
                    and row["lifecycle_status"] == "ready"
                    and row["artifact_version"] != normalized_artifact
                )
                if not should_claim:
                    database.commit()
                    return _observer(row)
                sprite_name = row["sprite_external_id"]
                generation = int(row["generation"]) + 1
            else:
                sprite_name = generated_name
                generation = 1

            registration = _create_runner_registration_in_connection(
                database,
                normalized_user_id,
                name="Managed Fly Sprite",
                cloud_provider="fly_sprites",
                region=normalized_region,
                storage_profile="fly_sprites_posix",
                control_url=control_url,
                runner_kind="managed",
                now=current_time,
            )
            runner_id = registration["runner"]["id"]
            if row is None:
                database.execute(
                    """
                    INSERT INTO managed_runtimes (
                        user_id, runner_id, provider_name, sprite_external_id,
                        lifecycle_status, generation, artifact_version,
                        created_at, updated_at, last_error
                    ) VALUES (?, ?, 'fly_sprites', ?, 'provisioning', 1, ?, ?, ?, NULL)
                    """,
                    (
                        normalized_user_id,
                        runner_id,
                        sprite_name,
                        normalized_artifact,
                        now_text,
                        now_text,
                    ),
                )
            else:
                database.execute(
                    """
                    UPDATE managed_runtimes
                    SET runner_id = ?, lifecycle_status = 'provisioning',
                        generation = ?, artifact_version = ?, updated_at = ?,
                        last_error = NULL
                    WHERE user_id = ?
                    """,
                    (
                        runner_id,
                        generation,
                        normalized_artifact,
                        now_text,
                        normalized_user_id,
                    ),
                )
            claimed_row = database.execute(
                "SELECT * FROM managed_runtimes WHERE user_id = ?",
                (normalized_user_id,),
            ).fetchone()
            assert claimed_row is not None, "managed runtime claim must exist"
            database.commit()
            return _owner(claimed_row, registration)
        except Exception:
            database.rollback()
            raise
