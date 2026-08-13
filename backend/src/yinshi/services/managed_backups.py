"""Durable managed backup catalog and operation claims."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast

from yinshi.db import get_control_db
from yinshi.services.runners import _require_user_id

ArchiveStatus = Literal["creating", "uploaded", "ready", "failed", "deleting", "deleted"]
OperationStatus = Literal["running", "failed"]
OperationKind = Literal["create", "restore", "delete"]


class ManagedBackupConflictError(RuntimeError):
    """The account already owns an active managed backup operation."""


@dataclass(frozen=True, slots=True)
class ManagedBackupArchive:
    id: str
    user_id: str
    runtime_generation: int
    status: ArchiveStatus
    object_key: str
    object_version: str | None
    size_bytes: int | None
    sha256: str | None
    wrapped_key: bytes
    key_id: str
    owner_digest: str
    created_at: str
    completed_at: str | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class ManagedBackupOperation:
    user_id: str
    job_id: str
    archive_id: str
    operation: OperationKind
    status: OperationStatus
    runtime_generation: int
    started_at: str
    updated_at: str
    last_error: str | None
    phase: str = "claimed"
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: str | None = None
    attempt_count: int = 0
    next_attempt_at: str | None = None
    source_runner_id: str | None = None
    source_sprite_id: str | None = None
    candidate_runner_id: str | None = None
    candidate_sprite_id: str | None = None
    activation_generation: int | None = None


@dataclass(frozen=True, slots=True)
class ManagedBackupCreationClaim:
    archive: ManagedBackupArchive
    operation: ManagedBackupOperation


def _timestamp(now: datetime) -> str:
    """Normalize one aware time for durable catalog comparisons."""
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _archive(row: sqlite3.Row) -> ManagedBackupArchive:
    """Convert one trusted catalog row to its public typed form."""
    return ManagedBackupArchive(
        id=row["id"],
        user_id=row["user_id"],
        runtime_generation=row["runtime_generation"],
        status=cast(ArchiveStatus, row["status"]),
        object_key=row["object_key"],
        object_version=row["object_version"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        wrapped_key=row["wrapped_key"],
        key_id=row["key_id"],
        owner_digest=row["owner_digest"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        last_error=row["last_error"],
    )


def _operation(row: sqlite3.Row) -> ManagedBackupOperation:
    """Convert one trusted operation row to its public typed form."""
    return ManagedBackupOperation(
        user_id=row["user_id"],
        job_id=row["job_id"],
        archive_id=row["archive_id"],
        operation=cast(OperationKind, row["operation"]),
        status=cast(OperationStatus, row["status"]),
        runtime_generation=row["runtime_generation"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        last_error=row["last_error"],
        phase=row["phase"],
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_expires_at=row["lease_expires_at"],
        attempt_count=row["attempt_count"],
        next_attempt_at=row["next_attempt_at"],
        source_runner_id=row["source_runner_id"],
        source_sprite_id=row["source_sprite_id"],
        candidate_runner_id=row["candidate_runner_id"],
        candidate_sprite_id=row["candidate_sprite_id"],
        activation_generation=row["activation_generation"],
    )


def claim_due_managed_backup_operation(
    *,
    worker_id: str,
    lease_token: str,
    now: datetime,
    lease_expires_at: datetime,
) -> ManagedBackupOperation | None:
    """Claim one due running operation with an exact expiring owner token."""
    for value, name in ((worker_id, "worker_id"), (lease_token, "lease_token")):
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(f"{name} must be bounded non-empty text")
    now_text = _timestamp(now)
    expiry_text = _timestamp(lease_expires_at)
    if expiry_text <= now_text:
        raise ValueError("lease_expires_at must be after now")
    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                """SELECT job_id FROM managed_backup_operations
                   WHERE status = 'running'
                     AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                     AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                   ORDER BY started_at, job_id LIMIT 1""",
                (now_text, now_text),
            ).fetchone()
            if row is None:
                database.rollback()
                return None
            result = database.execute(
                """UPDATE managed_backup_operations
                   SET lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                       attempt_count = attempt_count + 1, updated_at = ?
                   WHERE job_id = ? AND status = 'running'
                     AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
                (
                    worker_id,
                    lease_token,
                    expiry_text,
                    now_text,
                    row["job_id"],
                    now_text,
                ),
            )
            if result.rowcount != 1:
                database.rollback()
                return None
            claimed = database.execute(
                "SELECT * FROM managed_backup_operations WHERE job_id = ?",
                (row["job_id"],),
            ).fetchone()
            assert claimed is not None
            database.commit()
        except Exception:
            database.rollback()
            raise
    return _operation(claimed)


def renew_managed_backup_operation_lease(
    *,
    job_id: str,
    worker_id: str,
    lease_token: str,
    runtime_generation: int,
    now: datetime,
    lease_expires_at: datetime,
) -> bool:
    """Extend one unexpired operation lease held by its exact current owner."""
    for value, name in (
        (job_id, "job_id"),
        (worker_id, "worker_id"),
        (lease_token, "lease_token"),
    ):
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(f"{name} must be bounded non-empty text")
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")
    now_text = _timestamp(now)
    expiry_text = _timestamp(lease_expires_at)
    if expiry_text <= now_text:
        raise ValueError("lease_expires_at must be after now")
    with get_control_db() as database:
        result = database.execute(
            """UPDATE managed_backup_operations
               SET lease_expires_at = ?, updated_at = ?
               WHERE job_id = ? AND status = 'running' AND runtime_generation = ?
                 AND lease_owner = ? AND lease_token = ? AND lease_expires_at > ?""",
            (
                expiry_text,
                now_text,
                job_id,
                runtime_generation,
                worker_id,
                lease_token,
                now_text,
            ),
        )
        database.commit()
    return result.rowcount == 1


def record_managed_backup_candidate(
    *,
    job_id: str,
    lease_token: str,
    runtime_generation: int,
    candidate_runner_id: str,
    candidate_sprite_id: str,
    now: datetime,
) -> bool:
    """Persist exact restore candidate identity for the current leased job."""
    for value, name in (
        (job_id, "job_id"),
        (lease_token, "lease_token"),
        (candidate_runner_id, "candidate_runner_id"),
        (candidate_sprite_id, "candidate_sprite_id"),
    ):
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(f"{name} must be bounded non-empty text")
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")
    timestamp = _timestamp(now)
    with get_control_db() as database:
        result = database.execute(
            """UPDATE managed_backup_operations
               SET candidate_runner_id = ?, candidate_sprite_id = ?,
                   phase = 'candidate_provisioning', updated_at = ?
               WHERE job_id = ? AND lease_token = ? AND operation = 'restore'
                 AND status = 'running' AND runtime_generation = ?
                 AND (
                     phase = 'claimed'
                     OR (
                         phase = 'candidate_provisioning'
                         AND candidate_runner_id = ? AND candidate_sprite_id = ?
                     )
                 )
                 AND lease_expires_at > ?""",
            (
                candidate_runner_id,
                candidate_sprite_id,
                timestamp,
                job_id,
                lease_token,
                runtime_generation,
                candidate_runner_id,
                candidate_sprite_id,
                timestamp,
            ),
        )
        database.commit()
    return result.rowcount == 1


def clear_managed_backup_candidate(
    *,
    job_id: str,
    lease_token: str,
    runtime_generation: int,
    candidate_runner_id: str,
    candidate_sprite_id: str,
    now: datetime,
) -> bool:
    """Clear one failed replacement identity from its exact leased restore job."""
    for value, name in (
        (job_id, "job_id"),
        (lease_token, "lease_token"),
        (candidate_runner_id, "candidate_runner_id"),
        (candidate_sprite_id, "candidate_sprite_id"),
    ):
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(f"{name} must be bounded non-empty text")
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")
    timestamp = _timestamp(now)
    with get_control_db() as database:
        result = database.execute(
            """UPDATE managed_backup_operations
               SET candidate_runner_id = NULL, candidate_sprite_id = NULL,
                   phase = 'claimed', updated_at = ?
               WHERE job_id = ? AND lease_token = ? AND operation = 'restore'
                 AND status = 'running' AND runtime_generation = ?
                 AND phase = 'candidate_provisioning'
                 AND candidate_runner_id = ? AND candidate_sprite_id = ?
                 AND lease_expires_at > ?""",
            (
                timestamp,
                job_id,
                lease_token,
                runtime_generation,
                candidate_runner_id,
                candidate_sprite_id,
                timestamp,
            ),
        )
        database.commit()
    return result.rowcount == 1


def advance_managed_backup_operation(
    *,
    job_id: str,
    lease_token: str,
    runtime_generation: int,
    expected_phase: str,
    next_phase: str,
    now: datetime,
) -> bool:
    """Advance one owned job only across its expected durable boundary."""
    for value, name in (
        (job_id, "job_id"),
        (lease_token, "lease_token"),
        (expected_phase, "expected_phase"),
        (next_phase, "next_phase"),
    ):
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(f"{name} must be bounded non-empty text")
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")
    timestamp = _timestamp(now)
    with get_control_db() as database:
        result = database.execute(
            """UPDATE managed_backup_operations
               SET phase = ?, updated_at = ?
               WHERE job_id = ? AND lease_token = ? AND status = 'running'
                 AND runtime_generation = ? AND phase = ?
                 AND lease_expires_at > ?""",
            (
                next_phase,
                timestamp,
                job_id,
                lease_token,
                runtime_generation,
                expected_phase,
                timestamp,
            ),
        )
        database.commit()
    return result.rowcount == 1


def record_managed_backup_upload_intent(
    *,
    job_id: str,
    lease_token: str,
    runtime_generation: int,
    size_bytes: int,
    sha256: str,
    now: datetime,
) -> bool:
    """Persist trusted guest metadata before immutable object publication."""
    for value, name in ((job_id, "job_id"), (lease_token, "lease_token")):
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(f"{name} must be bounded non-empty text")
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")
    if type(size_bytes) is not int or size_bytes <= 0:
        raise ValueError("size_bytes must be a positive integer")
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    timestamp = _timestamp(now)
    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            operation = database.execute(
                """SELECT archive_id FROM managed_backup_operations
                   WHERE job_id = ? AND lease_token = ? AND operation = 'create'
                     AND status = 'running' AND runtime_generation = ?
                     AND phase = 'claimed' AND lease_expires_at > ?""",
                (job_id, lease_token, runtime_generation, timestamp),
            ).fetchone()
            if operation is None:
                database.rollback()
                return False
            archive_result = database.execute(
                """UPDATE managed_backup_archives
                   SET size_bytes = ?, sha256 = ?, last_error = NULL
                   WHERE id = ? AND status = 'creating'
                     AND runtime_generation = ? AND object_version IS NULL""",
                (size_bytes, sha256, operation["archive_id"], runtime_generation),
            )
            operation_result = database.execute(
                """UPDATE managed_backup_operations
                   SET phase = 'object_uploading', updated_at = ?
                   WHERE job_id = ? AND lease_token = ? AND operation = 'create'
                     AND status = 'running' AND runtime_generation = ?
                     AND phase = 'claimed' AND lease_expires_at > ?""",
                (
                    timestamp,
                    job_id,
                    lease_token,
                    runtime_generation,
                    timestamp,
                ),
            )
            if archive_result.rowcount != 1 or operation_result.rowcount != 1:
                database.rollback()
                return False
            database.commit()
            return True
        except Exception:
            database.rollback()
            raise


def complete_managed_backup_restore(
    *,
    job_id: str,
    lease_token: str,
    runtime_generation: int,
    now: datetime,
) -> bool:
    """Complete one activated restore after exact old-Sprite deletion."""
    for value, name in ((job_id, "job_id"), (lease_token, "lease_token")):
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(f"{name} must be bounded non-empty text")
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")
    timestamp = _timestamp(now)
    with get_control_db() as database:
        result = database.execute(
            """DELETE FROM managed_backup_operations
               WHERE job_id = ? AND lease_token = ? AND operation = 'restore'
                 AND status = 'running' AND runtime_generation = ?
                 AND phase = 'activated' AND lease_expires_at > ?""",
            (job_id, lease_token, runtime_generation, timestamp),
        )
        database.commit()
    return result.rowcount == 1


def start_managed_backup_creation(
    user_id: str,
    *,
    runtime_generation: int,
    archive_id: str,
    job_id: str,
    object_key: str,
    wrapped_key: bytes,
    key_id: str,
    owner_digest: str,
    now: datetime,
) -> ManagedBackupCreationClaim:
    """Atomically create one archive row and exclusive active operation."""
    normalized_user_id = _require_user_id(user_id)
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")
    for value, name, limit in (
        (archive_id, "archive_id", 128),
        (job_id, "job_id", 128),
        (object_key, "object_key", 1024),
        (key_id, "key_id", 128),
    ):
        if not isinstance(value, str) or not value or len(value) > limit:
            raise ValueError(f"{name} must be bounded non-empty text")
    if not isinstance(wrapped_key, bytes) or not wrapped_key:
        raise ValueError("wrapped_key must be non-empty bytes")
    if len(owner_digest) != 64 or any(
        character not in "0123456789abcdef" for character in owner_digest
    ):
        raise ValueError("owner_digest must be 64 lowercase hexadecimal characters")
    timestamp = _timestamp(now)
    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            active = database.execute(
                """SELECT status FROM managed_backup_operations
                   WHERE user_id = ?""",
                (normalized_user_id,),
            ).fetchone()
            if active is not None and active["status"] == "running":
                raise ManagedBackupConflictError("Managed backup operation is already active")
            if active is not None:
                database.execute(
                    "DELETE FROM managed_backup_operations WHERE user_id = ?",
                    (normalized_user_id,),
                )
            runtime = database.execute(
                """SELECT generation, lifecycle_status, runner_id, sprite_external_id
                   FROM managed_runtimes WHERE user_id = ?""",
                (normalized_user_id,),
            ).fetchone()
            if (
                runtime is None
                or runtime["generation"] != runtime_generation
                or runtime["lifecycle_status"] != "ready"
            ):
                raise ManagedBackupConflictError("Managed runtime is not ready for backup")
            database.execute(
                """INSERT INTO managed_backup_archives (
                       id, user_id, runtime_generation, status, object_key,
                       wrapped_key, key_id, owner_digest, created_at
                   ) VALUES (?, ?, ?, 'creating', ?, ?, ?, ?, ?)""",
                (
                    archive_id,
                    normalized_user_id,
                    runtime_generation,
                    object_key,
                    wrapped_key,
                    key_id,
                    owner_digest,
                    timestamp,
                ),
            )
            database.execute(
                """INSERT INTO managed_backup_operations (
                       user_id, job_id, archive_id, operation, status,
                       runtime_generation, source_runner_id, source_sprite_id,
                       started_at, updated_at
                   ) VALUES (?, ?, ?, 'create', 'running', ?, ?, ?, ?, ?)""",
                (
                    normalized_user_id,
                    job_id,
                    archive_id,
                    runtime_generation,
                    runtime["runner_id"],
                    runtime["sprite_external_id"],
                    timestamp,
                    timestamp,
                ),
            )
            archive_row = database.execute(
                "SELECT * FROM managed_backup_archives WHERE id = ?",
                (archive_id,),
            ).fetchone()
            operation_row = database.execute(
                "SELECT * FROM managed_backup_operations WHERE user_id = ?",
                (normalized_user_id,),
            ).fetchone()
            assert archive_row is not None and operation_row is not None
            database.commit()
        except ManagedBackupConflictError:
            database.rollback()
            raise
        except Exception:
            database.rollback()
            raise
    return ManagedBackupCreationClaim(
        archive=_archive(archive_row),
        operation=_operation(operation_row),
    )


def start_managed_backup_restore(
    user_id: str,
    *,
    archive_id: str,
    runtime_generation: int,
    job_id: str,
    now: datetime,
) -> ManagedBackupCreationClaim:
    """Claim one tenant-owned ready archive for replacement-runtime restore."""
    normalized_user_id = _require_user_id(user_id)
    if not isinstance(archive_id, str) or not archive_id:
        raise ValueError("archive_id must not be empty")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id must not be empty")
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")
    timestamp = _timestamp(now)
    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            runtime = database.execute(
                """SELECT generation, runner_id, sprite_external_id
                   FROM managed_runtimes
                   WHERE user_id = ? AND lifecycle_status = 'ready'""",
                (normalized_user_id,),
            ).fetchone()
            archive_row = database.execute(
                """SELECT * FROM managed_backup_archives
                   WHERE id = ? AND user_id = ? AND status = 'ready'
                     AND object_version IS NOT NULL""",
                (archive_id, normalized_user_id),
            ).fetchone()
            if runtime is None or runtime["generation"] != runtime_generation:
                raise ManagedBackupConflictError("managed runtime generation changed")
            if archive_row is None:
                raise ManagedBackupConflictError("managed backup archive is not ready")
            database.execute(
                """INSERT INTO managed_backup_operations (
                       user_id, job_id, archive_id, operation, status,
                       runtime_generation, source_runner_id, source_sprite_id,
                       started_at, updated_at, last_error
                   ) VALUES (?, ?, ?, 'restore', 'running', ?, ?, ?, ?, ?, NULL)""",
                (
                    normalized_user_id,
                    job_id,
                    archive_id,
                    runtime_generation,
                    runtime["runner_id"],
                    runtime["sprite_external_id"],
                    timestamp,
                    timestamp,
                ),
            )
            database.commit()
        except sqlite3.IntegrityError:
            database.rollback()
            raise ManagedBackupConflictError("managed backup operation is active") from None
        except Exception:
            database.rollback()
            raise
    return ManagedBackupCreationClaim(
        archive=_archive(archive_row),
        operation=ManagedBackupOperation(
            user_id=normalized_user_id,
            job_id=job_id,
            archive_id=archive_id,
            operation="restore",
            status="running",
            runtime_generation=runtime_generation,
            started_at=timestamp,
            updated_at=timestamp,
            last_error=None,
        ),
    )


def start_managed_backup_deletion(
    user_id: str,
    *,
    archive_id: str,
    runtime_generation: int,
    job_id: str,
    now: datetime,
) -> ManagedBackupCreationClaim:
    """Claim exact-version deletion while retaining wrapped recovery material."""
    normalized_user_id = _require_user_id(user_id)
    if not isinstance(archive_id, str) or not archive_id:
        raise ValueError("archive_id must not be empty")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id must not be empty")
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")
    timestamp = _timestamp(now)
    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            result = database.execute(
                """UPDATE managed_backup_archives SET status = 'deleting'
                   WHERE id = ? AND user_id = ? AND status = 'ready'
                     AND object_version IS NOT NULL""",
                (archive_id, normalized_user_id),
            )
            if result.rowcount != 1:
                raise ManagedBackupConflictError("managed backup archive is not ready")
            database.execute(
                """INSERT INTO managed_backup_operations (
                       user_id, job_id, archive_id, operation, status,
                       runtime_generation, started_at, updated_at, last_error
                   ) VALUES (?, ?, ?, 'delete', 'running', ?, ?, ?, NULL)""",
                (
                    normalized_user_id,
                    job_id,
                    archive_id,
                    runtime_generation,
                    timestamp,
                    timestamp,
                ),
            )
            archive_row = database.execute(
                "SELECT * FROM managed_backup_archives WHERE id = ?",
                (archive_id,),
            ).fetchone()
            assert archive_row is not None
            database.commit()
        except sqlite3.IntegrityError:
            database.rollback()
            raise ManagedBackupConflictError("managed backup operation is active") from None
        except Exception:
            database.rollback()
            raise
    return ManagedBackupCreationClaim(
        archive=_archive(archive_row),
        operation=ManagedBackupOperation(
            user_id=normalized_user_id,
            job_id=job_id,
            archive_id=archive_id,
            operation="delete",
            status="running",
            runtime_generation=runtime_generation,
            started_at=timestamp,
            updated_at=timestamp,
            last_error=None,
        ),
    )


def complete_managed_backup_deletion(
    user_id: str,
    *,
    job_id: str,
    lease_token: str,
    runtime_generation: int,
    now: datetime,
) -> bool:
    """Retire one exact remotely deleted archive and release its fence."""
    normalized_user_id = _require_user_id(user_id)
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id must not be empty")
    if not isinstance(lease_token, str) or not lease_token:
        raise ValueError("lease_token must not be empty")
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")
    timestamp = _timestamp(now)
    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            operation = database.execute(
                """SELECT archive_id FROM managed_backup_operations
                   WHERE user_id = ? AND job_id = ? AND operation = 'delete'
                     AND status = 'running' AND runtime_generation = ?
                     AND lease_token = ? AND lease_expires_at > ?""",
                (
                    normalized_user_id,
                    job_id,
                    runtime_generation,
                    lease_token,
                    timestamp,
                ),
            ).fetchone()
            if operation is None:
                database.rollback()
                return False
            result = database.execute(
                """UPDATE managed_backup_archives
                   SET status = 'deleted', object_version = NULL, size_bytes = NULL,
                       sha256 = NULL, wrapped_key = X'', completed_at = ?,
                       last_error = NULL
                   WHERE id = ? AND user_id = ? AND status = 'deleting'""",
                (timestamp, operation["archive_id"], normalized_user_id),
            )
            if result.rowcount != 1:
                database.rollback()
                return False
            database.execute(
                "DELETE FROM managed_backup_operations WHERE user_id = ? AND job_id = ?",
                (normalized_user_id, job_id),
            )
            database.commit()
            return True
        except Exception:
            database.rollback()
            raise


def fail_managed_backup_creation(
    user_id: str,
    *,
    job_id: str,
    runtime_generation: int,
    error_code: str,
    now: datetime,
) -> bool:
    """Fail one matching create operation and release its maintenance fence."""
    normalized_user_id = _require_user_id(user_id)
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id must not be empty")
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")
    if (
        not isinstance(error_code, str)
        or not error_code
        or len(error_code) > 100
        or any(character not in "abcdefghijklmnopqrstuvwxyz_" for character in error_code)
    ):
        raise ValueError("error_code must be bounded lowercase identifier text")
    timestamp = _timestamp(now)
    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            operation = database.execute(
                """SELECT archive_id FROM managed_backup_operations
                   WHERE user_id = ? AND job_id = ? AND operation = 'create'
                     AND status = 'running' AND runtime_generation = ?""",
                (normalized_user_id, job_id, runtime_generation),
            ).fetchone()
            if operation is None:
                database.rollback()
                return False
            result = database.execute(
                """UPDATE managed_backup_archives
                   SET status = 'failed', completed_at = ?, last_error = ?
                   WHERE id = ? AND user_id = ? AND status = 'creating'
                     AND runtime_generation = ?""",
                (
                    timestamp,
                    error_code,
                    operation["archive_id"],
                    normalized_user_id,
                    runtime_generation,
                ),
            )
            if result.rowcount != 1:
                database.rollback()
                return False
            database.execute(
                """UPDATE managed_backup_operations
                   SET status = 'failed', updated_at = ?, last_error = ?
                   WHERE user_id = ? AND job_id = ? AND status = 'running'""",
                (timestamp, error_code, normalized_user_id, job_id),
            )
            database.commit()
            return True
        except Exception:
            database.rollback()
            raise


def list_managed_backup_retention_candidates(
    *,
    cutoff: str,
    limit: int,
) -> tuple[ManagedBackupArchive, ...]:
    """Return bounded old ready archives not owned by active operations."""
    if not isinstance(cutoff, str) or not cutoff:
        raise ValueError("cutoff must not be empty")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with get_control_db() as database:
        rows = database.execute(
            """SELECT archive.* FROM managed_backup_archives AS archive
               WHERE archive.status = 'ready' AND archive.created_at < ?
                 AND archive.object_version IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM managed_backup_operations AS operation
                     WHERE operation.archive_id = archive.id
                       AND operation.status = 'running'
                 )
               ORDER BY archive.created_at, archive.id LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
    return tuple(_archive(row) for row in rows)


def get_managed_backup_operation(
    user_id: str,
    job_id: str,
) -> ManagedBackupOperation | None:
    """Return one tenant-owned operation without exposing another account."""
    normalized_user_id = _require_user_id(user_id)
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id must not be empty")
    with get_control_db() as database:
        row = database.execute(
            """SELECT * FROM managed_backup_operations
               WHERE user_id = ? AND job_id = ?""",
            (normalized_user_id, job_id),
        ).fetchone()
    return None if row is None else _operation(row)


def managed_backup_operation_is_running(user_id: str) -> bool:
    """Return whether one account has a durable managed maintenance fence."""
    normalized_user_id = _require_user_id(user_id)
    with get_control_db() as database:
        row = database.execute(
            """SELECT 1 FROM managed_backup_operations
               WHERE user_id = ? AND status = 'running'""",
            (normalized_user_id,),
        ).fetchone()
    return row is not None


def get_running_managed_backup_operation_for_runner(
    runner_id: str,
) -> ManagedBackupOperation | None:
    """Return the running maintenance job that fences one exact source runner."""
    if not isinstance(runner_id, str) or not runner_id or len(runner_id) > 128:
        raise ValueError("runner_id must be bounded non-empty text")
    with get_control_db() as database:
        row = database.execute(
            """SELECT * FROM managed_backup_operations
               WHERE source_runner_id = ? AND status = 'running'""",
            (runner_id,),
        ).fetchone()
    return None if row is None else _operation(row)


def list_managed_backup_archives(
    user_id: str,
    *,
    limit: int = 100,
) -> tuple[ManagedBackupArchive, ...]:
    """Return bounded newest-first archive rows owned by one account."""
    normalized_user_id = _require_user_id(user_id)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with get_control_db() as database:
        rows = database.execute(
            """SELECT * FROM managed_backup_archives
               WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?""",
            (normalized_user_id, limit),
        ).fetchall()
    return tuple(_archive(row) for row in rows)


def get_managed_backup_archive(
    user_id: str,
    archive_id: str,
) -> ManagedBackupArchive | None:
    """Return one tenant-owned archive without exposing another account."""
    normalized_user_id = _require_user_id(user_id)
    if not isinstance(archive_id, str) or not archive_id:
        raise ValueError("archive_id must not be empty")
    with get_control_db() as database:
        row = database.execute(
            "SELECT * FROM managed_backup_archives WHERE user_id = ? AND id = ?",
            (normalized_user_id, archive_id),
        ).fetchone()
    return None if row is None else _archive(row)


def record_managed_backup_upload(
    user_id: str,
    *,
    job_id: str,
    lease_token: str,
    runtime_generation: int,
    size_bytes: int,
    sha256: str,
    object_version: str,
    now: datetime,
) -> bool:
    """Persist exact uploaded object metadata before runtime recovery."""
    normalized_user_id = _require_user_id(user_id)
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id must not be empty")
    if not isinstance(lease_token, str) or not lease_token:
        raise ValueError("lease_token must not be empty")
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")
    if type(size_bytes) is not int or size_bytes <= 0:
        raise ValueError("size_bytes must be a positive integer")
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    if not isinstance(object_version, str) or not object_version or len(object_version) > 1024:
        raise ValueError("object_version must be bounded non-empty text")
    timestamp = _timestamp(now)
    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            operation = database.execute(
                """SELECT archive_id FROM managed_backup_operations
                   WHERE user_id = ? AND job_id = ? AND operation = 'create'
                     AND status = 'running' AND runtime_generation = ?
                     AND lease_token = ? AND lease_expires_at > ?""",
                (
                    normalized_user_id,
                    job_id,
                    runtime_generation,
                    lease_token,
                    timestamp,
                ),
            ).fetchone()
            if operation is None:
                database.rollback()
                return False
            result = database.execute(
                """UPDATE managed_backup_archives
                   SET status = 'uploaded', object_version = ?, size_bytes = ?,
                       sha256 = ?, completed_at = ?, last_error = NULL
                   WHERE id = ? AND user_id = ? AND status = 'creating'
                     AND runtime_generation = ?""",
                (
                    object_version,
                    size_bytes,
                    sha256,
                    timestamp,
                    operation["archive_id"],
                    normalized_user_id,
                    runtime_generation,
                ),
            )
            database.commit()
            return result.rowcount == 1
        except Exception:
            database.rollback()
            raise


def complete_managed_backup_creation(
    user_id: str,
    *,
    job_id: str,
    lease_token: str | None = None,
    runtime_generation: int,
    size_bytes: int,
    sha256: str,
    object_version: str | None,
    now: datetime,
) -> bool:
    """Publish verified object metadata for the matching active creation."""
    normalized_user_id = _require_user_id(user_id)
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id must not be empty")
    if not isinstance(lease_token, str) or not lease_token:
        raise ValueError("lease_token must not be empty")
    if type(runtime_generation) is not int or runtime_generation <= 0:
        raise ValueError("runtime_generation must be a positive integer")
    if type(size_bytes) is not int or size_bytes <= 0:
        raise ValueError("size_bytes must be a positive integer")
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    if not isinstance(object_version, str) or not object_version or len(object_version) > 1024:
        return False
    timestamp = _timestamp(now)
    with get_control_db() as database:
        try:
            database.execute("BEGIN IMMEDIATE")
            operation = database.execute(
                """SELECT archive_id FROM managed_backup_operations
                   WHERE user_id = ? AND job_id = ? AND operation = 'create'
                     AND status = 'running' AND runtime_generation = ?
                     AND lease_token = ? AND lease_expires_at > ?""",
                (
                    normalized_user_id,
                    job_id,
                    runtime_generation,
                    lease_token,
                    timestamp,
                ),
            ).fetchone()
            runtime = database.execute(
                "SELECT generation, lifecycle_status FROM managed_runtimes WHERE user_id = ?",
                (normalized_user_id,),
            ).fetchone()
            if (
                operation is None
                or runtime is None
                or runtime["generation"] != runtime_generation
                or runtime["lifecycle_status"] != "ready"
            ):
                database.rollback()
                return False
            result = database.execute(
                """UPDATE managed_backup_archives
                   SET status = 'ready', object_version = ?, size_bytes = ?,
                       sha256 = ?, completed_at = ?, last_error = NULL
                   WHERE id = ? AND user_id = ?
                     AND status IN ('creating', 'uploaded')
                     AND runtime_generation = ?""",
                (
                    object_version,
                    size_bytes,
                    sha256,
                    timestamp,
                    operation["archive_id"],
                    normalized_user_id,
                    runtime_generation,
                ),
            )
            if result.rowcount != 1:
                database.rollback()
                return False
            database.execute(
                "DELETE FROM managed_backup_operations WHERE user_id = ? AND job_id = ?",
                (normalized_user_id, job_id),
            )
            database.commit()
            return True
        except Exception:
            database.rollback()
            raise
