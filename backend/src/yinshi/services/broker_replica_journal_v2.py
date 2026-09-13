"""Append-only replica lifecycle V2 journal with authenticated continuation."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from yinshi.services.broker_protocol import (
    BROKER_FRAME_BYTES_MAX,
    BROKER_RESPONSE_BYTES_MAX,
    BrokerProtocolError,
    BrokerRequest,
    JsonValue,
    canonical_json,
    parse_signed_request,
    verify_broker_response,
)
from yinshi.services.broker_replica_journal import (
    REPLICA_JOURNAL_SCHEMA_STATEMENTS as LEGACY_REPLICA_JOURNAL_V1_SCHEMA_STATEMENTS,
    AdmissionReceipt,
    ArtifactReference,
    DrainReceipt,
    ExportReceipt,
    IngestReceipt,
    PublishReceipt,
    ReclaimReceipt,
    RejectedReceipt,
    ReplicaAuthority,
    ReplicaJournalPosition,
    VerifyReceipt,
    parse_replica_authority,
)
from yinshi.services.replica_artifact_contract import (
    REPLICA_ARTIFACT_MEDIA_TYPES,
    compute_replica_artifact_set_sha256,
    validate_distinct_artifact_ids,
    validate_replica_identifier,
    validate_replica_operation_id,
)

REPLICA_JOURNAL_V2_SCHEMA_VERSION = 2
RECEIPT_JSON_BYTES_MAX = 2_048
_APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_APPEND_RECEIPT_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_JOURNAL_ID_PATTERN = re.compile(r"^[0-9a-f]{32}_[0-9a-f]{64}$")
_SQLITE_ROLLBACK_MAGIC = b"\xd9\xd5\x05\xf9 \xa1c\xd7"
_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_OBJECT_FORMATS = ("sha1", "sha256")
_LEGACY_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_STAGES = ("ingest", "verify", "publish", "admission", "drain", "export", "reclaim")
_STAGE_INDEX = {stage: index for index, stage in enumerate(_STAGES)}
_INITIAL_STAGES = frozenset(_STAGES[:4])
_CONTINUATION_STAGES = frozenset(_STAGES[4:])
_BASE_UNRESOLVED_REASONS = frozenset(
    {
        "broker_restart_unknown",
        "cancellation_unknown",
        "sqlite_commit_unknown",
        "timeout_unknown",
        "transport_unknown",
    }
)
_STAGE_UNRESOLVED_REASONS = {
    "ingest": frozenset({"transfer_unknown", "verification_unknown"}),
    "verify": frozenset({"verification_unknown"}),
    "publish": frozenset({"publication_unknown", "synchronization_unknown"}),
    "admission": frozenset({"admission_unknown", "verification_unknown"}),
    "drain": frozenset({"acknowledgment_unknown", "runtime_live", "unit_state_unknown"}),
    "export": frozenset(
        {"export_acknowledgment_unknown", "transfer_unknown", "verification_unknown"}
    ),
    "reclaim": frozenset(
        {"absence_unknown", "effect_unknown", "presence_unknown", "unit_state_unknown"}
    ),
}
_REJECTED_STATUSES = {
    "ingest": frozenset({"artifact_rejected", "limit_rejected"}),
    "verify": frozenset({"verification_rejected"}),
    "publish": frozenset({"publication_rejected"}),
    "admission": frozenset({"admission_rejected"}),
    "drain": frozenset({"drain_rejected"}),
    "export": frozenset({"export_rejected", "limit_rejected"}),
    "reclaim": frozenset({"reclaim_rejected"}),
}


class LegacyReplicaJournalStateError(RuntimeError):
    """Reject unsafe legacy replica journal state before V2 startup."""


class ReplicaJournalV2Error(RuntimeError):
    """Base class for replica journal V2 failures."""


class ReplicaJournalV2SyncError(ReplicaJournalV2Error):
    """Report malformed, conflicting, or uncertain durable V2 state."""


class ReplicaJournalV2CommitAbsent(ReplicaJournalV2SyncError):
    """Report a commit that a fresh connection confirms is absent."""


class ReplicaJournalV2ConflictError(ReplicaJournalV2SyncError):
    """Reject a call that conflicts with immutable V2 state."""


def _token(value: object, description: str) -> str:
    if not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(f"replica {description} is invalid")
    return value


def _digest(value: object, description: str) -> str:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"replica {description} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ReplicaDrainContinuation:
    """Application-authenticated authority for post-admission stages."""

    authority: ReplicaAuthority
    initial_request_sha256: str
    admission_receipt_id: str
    application_drain_intent_receipt_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, ReplicaAuthority):
            raise TypeError("replica drain continuation authority is invalid")
        _digest(self.initial_request_sha256, "initial request SHA-256")
        _token(self.admission_receipt_id, "admission receipt ID")
        _token(
            self.application_drain_intent_receipt_id,
            "application drain intent receipt ID",
        )


@dataclass(frozen=True, slots=True)
class CompletedReplicaReceipts:
    """Validated durable successful receipts for one lifecycle."""

    ingest: IngestReceipt | None
    verify: VerifyReceipt | None
    publish: PublishReceipt | None
    admission: AdmissionReceipt | None
    drain: DrainReceipt | None
    export: ExportReceipt | None
    reclaim: ReclaimReceipt | None


@dataclass(frozen=True, slots=True)
class ActiveReplicaLifecycleV2:
    """Durable paused lifecycle state."""

    request_frame: bytes
    authority: ReplicaAuthority
    owner_token: str
    owner_broker_incarnation: str
    admission_receipt: AdmissionReceipt
    response_frame: bytes
    drain_request_frame: bytes | None


@dataclass(frozen=True, slots=True)
class IncompleteReplicaLifecycleV2:
    """Durable claimed nonterminal lifecycle exposed for recovery."""

    request_frame: bytes
    drain_request_frame: bytes | None
    owner_token: str
    owner_broker_incarnation: str
    stage: str
    stage_started: bool
    paused: bool
    reason: str
    authority: ReplicaAuthority


@dataclass(frozen=True, slots=True)
class ReplicaJournalV2Decision:
    """One immutable lifecycle decision."""

    state: str
    stage: str | None
    response_frame: bytes | None
    owner_broker_incarnation: str | None


@dataclass(frozen=True, slots=True)
class _PinnedFileSnapshot:
    device: int
    inode: int
    mode: int
    size: int
    access_time_ns: int
    modification_time_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _PinnedFile:
    path: Path
    file_descriptor: int
    snapshot: _PinnedFileSnapshot


@dataclass(frozen=True, slots=True)
class _Event:
    event_id: int
    append_receipt_id: str
    database_incarnation: str
    operation_id: str
    event_type: str
    stage: str | None
    request_frame: bytes | None
    request_frame_sha256: str | None
    request_broker_incarnation: str | None
    request_type: str | None
    request_nonce: str | None
    request_connection_sequence: int | None
    request_payload_digest: str | None
    physical_target_id: str | None
    replica_generation: int | None
    execution_owner_id: str | None
    initial_request_sha256: str | None
    admission_receipt_id: str | None
    application_drain_intent_receipt_id: str | None
    owner_token: str | None
    owner_broker_incarnation: str | None
    stage_status: str | None
    receipt_json: bytes | None
    unresolved_reason: str | None
    response_frame: bytes | None
    response_frame_sha256: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class _Replay:
    initial_request: BrokerRequest
    initial_request_frame: bytes
    authority: ReplicaAuthority
    owner_token: str | None
    owner_broker_incarnation: str | None
    next_stage_index: int
    stage_started: bool
    paused: bool
    active_response_frame: bytes | None
    drain_request: BrokerRequest | None
    drain_request_frame: bytes | None
    continuation: ReplicaDrainContinuation | None
    terminal_state: str | None
    terminal_stage: str | None
    terminal_response_frame: bytes | None
    receipts: CompletedReplicaReceipts


REPLICA_JOURNAL_V2_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE replica_journal_v2_meta (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 0),
        application_id TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        journal_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE replica_journal_v2_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        append_receipt_id TEXT NOT NULL UNIQUE,
        database_incarnation TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (
            event_type IN (
                'accepted', 'authority_claimed', 'stage_started',
                'stage_outcome', 'stage_rejected', 'stage_unresolved',
                'lifecycle_paused', 'drain_continuation_accepted'
            )
        ),
        stage TEXT CHECK (
            stage IS NULL OR stage IN (
                'ingest', 'verify', 'publish', 'admission',
                'drain', 'export', 'reclaim'
            )
        ),
        request_frame BLOB,
        request_frame_sha256 TEXT,
        request_broker_incarnation TEXT,
        request_type TEXT,
        request_nonce TEXT,
        request_connection_sequence INTEGER,
        request_payload_digest TEXT,
        physical_target_id TEXT,
        replica_generation INTEGER,
        execution_owner_id TEXT,
        initial_request_sha256 TEXT,
        admission_receipt_id TEXT,
        application_drain_intent_receipt_id TEXT,
        owner_token TEXT,
        owner_broker_incarnation TEXT,
        stage_status TEXT,
        receipt_json BLOB,
        unresolved_reason TEXT,
        response_frame BLOB,
        response_frame_sha256 TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE UNIQUE INDEX replica_journal_v2_one_acceptance
    ON replica_journal_v2_events(database_incarnation, operation_id)
    WHERE event_type = 'accepted'
    """,
    """
    CREATE UNIQUE INDEX replica_journal_v2_one_claim
    ON replica_journal_v2_events(database_incarnation, operation_id)
    WHERE event_type = 'authority_claimed'
    """,
    """
    CREATE UNIQUE INDEX replica_journal_v2_one_stage_start
    ON replica_journal_v2_events(database_incarnation, operation_id, stage)
    WHERE event_type = 'stage_started'
    """,
    """
    CREATE UNIQUE INDEX replica_journal_v2_one_stage_completion
    ON replica_journal_v2_events(database_incarnation, operation_id, stage)
    WHERE event_type IN ('stage_outcome', 'stage_rejected', 'stage_unresolved')
    """,
    """
    CREATE UNIQUE INDEX replica_journal_v2_one_pause
    ON replica_journal_v2_events(database_incarnation, operation_id)
    WHERE event_type = 'lifecycle_paused'
    """,
    """
    CREATE UNIQUE INDEX replica_journal_v2_one_drain_continuation
    ON replica_journal_v2_events(database_incarnation, operation_id)
    WHERE event_type = 'drain_continuation_accepted'
    """,
    """
    CREATE UNIQUE INDEX replica_journal_v2_one_terminal
    ON replica_journal_v2_events(database_incarnation, operation_id)
    WHERE event_type IN ('stage_rejected', 'stage_unresolved')
       OR (event_type = 'stage_outcome' AND stage = 'reclaim')
    """,
    """
    CREATE TRIGGER replica_journal_v2_reject_update
    BEFORE UPDATE ON replica_journal_v2_events
    BEGIN
        SELECT RAISE(ABORT, 'replica journal V2 events are immutable');
    END
    """,
    """
    CREATE TRIGGER replica_journal_v2_reject_delete
    BEFORE DELETE ON replica_journal_v2_events
    BEGIN
        SELECT RAISE(ABORT, 'replica journal V2 events are immutable');
    END
    """,
    """
    CREATE TRIGGER replica_journal_v2_require_transition
    BEFORE INSERT ON replica_journal_v2_events
    WHEN NEW.event_type != 'accepted'
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM replica_journal_v2_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id
              AND event_type = 'accepted'
        ) THEN RAISE(ABORT, 'replica journal V2 acceptance is missing') END;
        SELECT CASE WHEN NEW.event_type != 'authority_claimed' AND NOT EXISTS (
            SELECT 1 FROM replica_journal_v2_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id
              AND event_type = 'authority_claimed'
              AND owner_token = NEW.owner_token
        ) THEN RAISE(ABORT, 'replica journal V2 authority is missing') END;
        SELECT CASE WHEN NEW.event_type IN (
            'stage_outcome', 'stage_rejected', 'stage_unresolved'
        ) AND NOT (
            NEW.event_type = 'stage_unresolved'
            AND NEW.stage = 'drain'
            AND NEW.unresolved_reason = 'broker_restart_unknown'
            AND EXISTS (
                SELECT 1 FROM replica_journal_v2_events
                WHERE database_incarnation = NEW.database_incarnation
                  AND operation_id = NEW.operation_id
                  AND event_type = 'lifecycle_paused'
            )
        ) AND NOT EXISTS (
            SELECT 1 FROM replica_journal_v2_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id
              AND event_type = 'stage_started'
              AND stage = NEW.stage
              AND owner_token = NEW.owner_token
        ) THEN RAISE(ABORT, 'replica journal V2 stage start is missing') END;
        SELECT CASE WHEN NEW.event_type = 'lifecycle_paused' AND NOT EXISTS (
            SELECT 1 FROM replica_journal_v2_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id
              AND event_type = 'stage_outcome'
              AND stage = 'admission'
              AND stage_status = 'completed'
              AND owner_token = NEW.owner_token
        ) THEN RAISE(ABORT, 'replica journal V2 admission outcome is missing') END;
        SELECT CASE WHEN NEW.event_type = 'drain_continuation_accepted' AND NOT EXISTS (
            SELECT 1 FROM replica_journal_v2_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id
              AND event_type = 'lifecycle_paused'
              AND owner_token = NEW.owner_token
        ) THEN RAISE(ABORT, 'replica journal V2 pause is missing') END;
        SELECT CASE WHEN NEW.stage IN ('drain', 'export', 'reclaim')
            AND NEW.event_type IN (
                'stage_started', 'stage_outcome', 'stage_rejected', 'stage_unresolved'
            )
            AND NOT (
                NEW.event_type = 'stage_unresolved'
                AND NEW.stage = 'drain'
                AND NEW.unresolved_reason = 'broker_restart_unknown'
            )
            AND NOT EXISTS (
                SELECT 1 FROM replica_journal_v2_events
                WHERE database_incarnation = NEW.database_incarnation
                  AND operation_id = NEW.operation_id
                  AND event_type = 'drain_continuation_accepted'
            ) THEN RAISE(ABORT, 'replica journal V2 continuation is missing') END;
        SELECT CASE WHEN EXISTS (
            SELECT 1 FROM replica_journal_v2_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id
              AND (event_type IN ('stage_rejected', 'stage_unresolved')
                OR (event_type = 'stage_outcome' AND stage = 'reclaim'))
        ) THEN RAISE(ABORT, 'replica journal V2 terminal state is immutable') END;
    END
    """,
    """
    CREATE TRIGGER replica_journal_v2_meta_reject_update
    BEFORE UPDATE ON replica_journal_v2_meta
    BEGIN
        SELECT RAISE(ABORT, 'replica journal V2 metadata is immutable');
    END
    """,
    """
    CREATE TRIGGER replica_journal_v2_meta_reject_delete
    BEFORE DELETE ON replica_journal_v2_meta
    BEGIN
        SELECT RAISE(ABORT, 'replica journal V2 metadata is immutable');
    END
    """,
)


def _normalized_sql(value: str) -> str:
    return " ".join(value.split()).rstrip(";")


def _schema_from(statements: tuple[str, ...]) -> dict[tuple[str, str], str]:
    database = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            database.execute(statement)
        rows = database.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {(str(kind), str(name)): _normalized_sql(str(sql)) for kind, name, sql in rows}
    finally:
        database.close()


@lru_cache(maxsize=1)
def _expected_schema() -> dict[tuple[str, str], str]:
    return _schema_from(REPLICA_JOURNAL_V2_SCHEMA_STATEMENTS)


@lru_cache(maxsize=1)
def _legacy_expected_schema() -> dict[tuple[str, str], str]:
    return _schema_from(LEGACY_REPLICA_JOURNAL_V1_SCHEMA_STATEMENTS)


def _hash_file_descriptor(file_descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        block = os.pread(file_descriptor, min(1024 * 1024, size - offset), offset)
        if not block:
            raise OSError("pinned journal became shorter while inspected")
        digest.update(block)
        offset += len(block)
    if os.pread(file_descriptor, 1, size):
        raise OSError("pinned journal became longer while inspected")
    return digest.hexdigest()


def _restore_pinned_timestamps(
    path: Path,
    file_descriptor: int,
    snapshot: _PinnedFileSnapshot,
) -> None:
    pinned = os.fstat(file_descriptor)
    path_metadata = path.lstat()
    if (
        (pinned.st_dev, pinned.st_ino) != (snapshot.device, snapshot.inode)
        or (path_metadata.st_dev, path_metadata.st_ino) != (snapshot.device, snapshot.inode)
        or pinned.st_mode != snapshot.mode
        or path_metadata.st_mode != snapshot.mode
        or pinned.st_size != snapshot.size
        or path_metadata.st_size != snapshot.size
        or pinned.st_mtime_ns != snapshot.modification_time_ns
        or path_metadata.st_mtime_ns != snapshot.modification_time_ns
    ):
        raise OSError("pinned journal identity changed before timestamp restoration")
    if (
        pinned.st_atime_ns != snapshot.access_time_ns
        or path_metadata.st_atime_ns != snapshot.access_time_ns
    ):
        os.utime(
            file_descriptor,
            ns=(snapshot.access_time_ns, snapshot.modification_time_ns),
        )
    restored = os.fstat(file_descriptor)
    restored_path = path.lstat()
    if (
        (restored.st_dev, restored.st_ino) != (snapshot.device, snapshot.inode)
        or (restored_path.st_dev, restored_path.st_ino) != (snapshot.device, snapshot.inode)
        or restored.st_mode != snapshot.mode
        or restored_path.st_mode != snapshot.mode
        or restored.st_size != snapshot.size
        or restored_path.st_size != snapshot.size
        or restored.st_atime_ns != snapshot.access_time_ns
        or restored_path.st_atime_ns != snapshot.access_time_ns
        or restored.st_mtime_ns != snapshot.modification_time_ns
        or restored_path.st_mtime_ns != snapshot.modification_time_ns
    ):
        raise OSError("pinned journal timestamp restoration could not be verified")


def _open_pinned_file(path: Path) -> tuple[int, _PinnedFileSnapshot]:
    path_metadata = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    no_atime = getattr(os, "O_NOATIME", 0)
    try:
        file_descriptor = os.open(path, flags | no_atime)
    except OSError as exc:
        if no_atime == 0 or exc.errno not in {errno.EINVAL, errno.EPERM}:
            raise
        file_descriptor = os.open(path, flags)
    cleanup_snapshot: _PinnedFileSnapshot | None = None
    try:
        pinned = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or not stat.S_ISREG(pinned.st_mode)
            or (path_metadata.st_dev, path_metadata.st_ino) != (pinned.st_dev, pinned.st_ino)
            or path_metadata.st_size != pinned.st_size
            or path_metadata.st_mode != pinned.st_mode
            or path_metadata.st_mtime_ns != pinned.st_mtime_ns
        ):
            raise OSError("journal path changed before inspection")
        cleanup_snapshot = _PinnedFileSnapshot(
            device=pinned.st_dev,
            inode=pinned.st_ino,
            mode=pinned.st_mode,
            size=pinned.st_size,
            access_time_ns=pinned.st_atime_ns,
            modification_time_ns=pinned.st_mtime_ns,
            sha256="",
        )
        content_sha256 = _hash_file_descriptor(file_descriptor, pinned.st_size)
        verified = os.fstat(file_descriptor)
        verified_path = path.lstat()
        if (
            (verified.st_dev, verified.st_ino) != (pinned.st_dev, pinned.st_ino)
            or (verified_path.st_dev, verified_path.st_ino) != (pinned.st_dev, pinned.st_ino)
            or verified.st_mode != pinned.st_mode
            or verified_path.st_mode != pinned.st_mode
            or verified.st_size != pinned.st_size
            or verified_path.st_size != pinned.st_size
            or verified.st_mtime_ns != pinned.st_mtime_ns
            or verified_path.st_mtime_ns != pinned.st_mtime_ns
        ):
            raise OSError("journal changed while pinned")
        snapshot = _PinnedFileSnapshot(
            device=pinned.st_dev,
            inode=pinned.st_ino,
            mode=pinned.st_mode,
            size=pinned.st_size,
            access_time_ns=pinned.st_atime_ns,
            modification_time_ns=pinned.st_mtime_ns,
            sha256=content_sha256,
        )
        return file_descriptor, snapshot
    except Exception:
        try:
            if cleanup_snapshot is not None:
                _restore_pinned_timestamps(path, file_descriptor, cleanup_snapshot)
        finally:
            os.close(file_descriptor)
        raise


def _finish_pinned_file(
    path: Path,
    file_descriptor: int,
    snapshot: _PinnedFileSnapshot,
) -> None:
    primary_error: OSError | None = None
    try:
        content_sha256 = _hash_file_descriptor(file_descriptor, snapshot.size)
        pinned = os.fstat(file_descriptor)
        path_metadata = path.lstat()
        if (
            (pinned.st_dev, pinned.st_ino) != (snapshot.device, snapshot.inode)
            or (path_metadata.st_dev, path_metadata.st_ino) != (snapshot.device, snapshot.inode)
            or pinned.st_mode != snapshot.mode
            or path_metadata.st_mode != snapshot.mode
            or pinned.st_size != snapshot.size
            or path_metadata.st_size != snapshot.size
            or content_sha256 != snapshot.sha256
        ):
            raise OSError("journal changed while inspected")
    except OSError as exc:
        primary_error = exc
    try:
        _restore_pinned_timestamps(path, file_descriptor, snapshot)
    except OSError as exc:
        raise OSError("journal preservation cleanup failed") from exc
    if primary_error is not None:
        raise primary_error


def _connect_pinned_immutable(file_descriptor: int) -> sqlite3.Connection:
    uri = f"{Path(f'/dev/fd/{file_descriptor}').as_uri()}?mode=ro&immutable=1"
    database = sqlite3.connect(uri, uri=True, timeout=5.0)
    database.execute("PRAGMA query_only = ON")
    return database


def _copy_pinned_file(
    file_descriptor: int,
    snapshot: _PinnedFileSnapshot,
    destination: Path,
) -> None:
    destination_descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        offset = 0
        while offset < snapshot.size:
            block = os.pread(
                file_descriptor,
                min(1024 * 1024, snapshot.size - offset),
                offset,
            )
            if not block:
                raise OSError("pinned journal became shorter while copied")
            written = 0
            while written < len(block):
                count = os.write(destination_descriptor, block[written:])
                if count < 1:
                    raise OSError("private journal copy could not be written")
                written += count
            offset += len(block)
        os.fsync(destination_descriptor)
    finally:
        os.close(destination_descriptor)


def _finish_pinned_files(files: list[_PinnedFile]) -> None:
    primary_error: OSError | None = None
    for pinned_file in reversed(files):
        try:
            _finish_pinned_file(
                pinned_file.path,
                pinned_file.file_descriptor,
                pinned_file.snapshot,
            )
        except OSError as exc:
            if primary_error is None:
                primary_error = exc
        finally:
            os.close(pinned_file.file_descriptor)
    if primary_error is not None:
        raise primary_error


def _open_pinned_files(paths: list[Path]) -> list[_PinnedFile]:
    files: list[_PinnedFile] = []
    try:
        for path in paths:
            file_descriptor, snapshot = _open_pinned_file(path)
            files.append(_PinnedFile(path, file_descriptor, snapshot))
        return files
    except OSError:
        _finish_pinned_files(files)
        raise


def _legacy_sidecar_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LegacyReplicaJournalStateError(
            "legacy replica journal sidecar is unreadable"
        ) from exc
    return True


def _require_no_legacy_sidecars(path: Path, *, orphaned: bool) -> None:
    for suffix in _LEGACY_SIDECAR_SUFFIXES:
        if _legacy_sidecar_exists(Path(f"{path}{suffix}")):
            state = "orphaned" if orphaned else "unresolved"
            raise LegacyReplicaJournalStateError(f"legacy replica journal sidecar is {state}")


def inspect_legacy_replica_v1_journal(path: Path, *, application_id: str) -> None:
    """Inspect replica V1 through pinned immutable read-only SQLite access."""
    if not isinstance(path, Path):
        raise TypeError("legacy replica journal path must be a Path")
    if (
        not isinstance(application_id, str)
        or _APPLICATION_ID_PATTERN.fullmatch(application_id) is None
    ):
        raise ValueError("legacy replica journal application ID is invalid")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _require_no_legacy_sidecars(path, orphaned=True)
        return
    except OSError as exc:
        raise LegacyReplicaJournalStateError("legacy replica journal is unreadable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o444 == 0:
        raise LegacyReplicaJournalStateError("legacy replica journal is unreadable")
    _require_no_legacy_sidecars(path, orphaned=False)
    try:
        file_descriptor, snapshot = _open_pinned_file(path)
    except OSError as exc:
        raise LegacyReplicaJournalStateError("legacy replica journal is unreadable") from exc
    database: sqlite3.Connection | None = None
    try:
        try:
            database = _connect_pinned_immutable(file_descriptor)
            if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise LegacyReplicaJournalStateError("legacy replica journal is malformed")
            objects = database.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            actual = {
                (str(kind), str(name)): _normalized_sql(str(sql)) for kind, name, sql in objects
            }
            if actual != _legacy_expected_schema():
                raise LegacyReplicaJournalStateError("legacy replica journal schema is malformed")
            rows = database.execute(
                "SELECT singleton, application_id, schema_version, journal_id "
                "FROM replica_journal_meta"
            ).fetchall()
            if len(rows) == 1 and isinstance(rows[0][2], int) and rows[0][2] > 1:
                raise LegacyReplicaJournalStateError(
                    "legacy replica journal version is unsupported"
                )
            if (
                len(rows) != 1
                or rows[0][0] != 0
                or rows[0][1] != application_id
                or rows[0][2] != 1
                or not isinstance(rows[0][3], str)
                or _JOURNAL_ID_PATTERN.fullmatch(rows[0][3]) is None
            ):
                raise LegacyReplicaJournalStateError("legacy replica journal metadata is malformed")
            accepted = database.execute(
                "SELECT 1 FROM replica_journal_events WHERE event_type = 'accepted' LIMIT 1"
            ).fetchone()
            if accepted is not None:
                raise LegacyReplicaJournalStateError(
                    "legacy replica journal contains an accepted operation"
                )
            any_event = database.execute("SELECT 1 FROM replica_journal_events LIMIT 1").fetchone()
            if any_event is not None:
                raise LegacyReplicaJournalStateError(
                    "legacy replica journal contains malformed history"
                )
        except LegacyReplicaJournalStateError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise LegacyReplicaJournalStateError("legacy replica journal is unreadable") from exc
        finally:
            if database is not None:
                database.close()
    finally:
        try:
            _require_no_legacy_sidecars(path, orphaned=False)
        finally:
            try:
                _finish_pinned_file(path, file_descriptor, snapshot)
            except OSError as exc:
                raise LegacyReplicaJournalStateError(
                    "legacy replica journal preservation could not be verified"
                ) from exc
            finally:
                os.close(file_descriptor)


def replica_drain_continuation_from_request(
    request: BrokerRequest,
) -> ReplicaDrainContinuation:
    """Strictly parse one authenticated replica drain continuation payload."""
    if not isinstance(request, BrokerRequest) or request.request_type != "replica.drain":
        raise ValueError("replica drain request is invalid")
    required = {
        "authority",
        "initial_request_sha256",
        "admission_receipt_id",
        "application_drain_intent_receipt_id",
    }
    if set(request.payload) != required:
        raise ValueError("replica drain continuation fields are invalid")
    return ReplicaDrainContinuation(
        authority=parse_replica_authority(request.payload["authority"]),
        initial_request_sha256=cast(str, request.payload["initial_request_sha256"]),
        admission_receipt_id=cast(str, request.payload["admission_receipt_id"]),
        application_drain_intent_receipt_id=cast(
            str, request.payload["application_drain_intent_receipt_id"]
        ),
    )


def _artifact_json(reference: ArtifactReference) -> dict[str, JsonValue]:
    return {
        "artifact_id": reference.artifact_id,
        "byte_length": reference.byte_length,
        "sha256": reference.sha256,
    }


def _parse_artifact(value: object) -> ArtifactReference:
    if not isinstance(value, dict) or set(value) != {"artifact_id", "sha256", "byte_length"}:
        raise ValueError("replica artifact receipt is malformed")
    return ArtifactReference(
        artifact_id=cast(str, value["artifact_id"]),
        sha256=cast(str, value["sha256"]),
        byte_length=cast(int, value["byte_length"]),
    )


def _validate_initial_payload(value: object) -> dict[str, JsonValue]:
    required = {
        "artifact_set_sha256",
        "authority",
        "bundle",
        "index_objects",
        "limits_sha256",
        "object_format",
        "reconciliation_fingerprint",
        "repository_id",
        "source_state_sha256",
        "workspace_id",
        "worktree",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("replica request payload fields are invalid")
    validate_replica_identifier(value["workspace_id"], "request workspace ID")
    validate_replica_identifier(value["repository_id"], "request repository ID")
    parse_replica_authority(value["authority"])
    if type(value["object_format"]) is not str or value["object_format"] not in _OBJECT_FORMATS:
        raise ValueError("replica request object format is invalid")
    for field, description in (
        ("artifact_set_sha256", "request artifact set SHA-256"),
        ("limits_sha256", "request limits SHA-256"),
        ("source_state_sha256", "request source state SHA-256"),
        ("reconciliation_fingerprint", "request reconciliation fingerprint"),
    ):
        _digest(value[field], description)
    references = (
        _parse_artifact(value["bundle"]),
        _parse_artifact(value["worktree"]),
        _parse_artifact(value["index_objects"]),
    )
    validate_distinct_artifact_ids(tuple(item.artifact_id for item in references))
    return cast(dict[str, JsonValue], value)


def _expected_artifact_set_sha256(request: BrokerRequest, authority: ReplicaAuthority) -> str:
    payload = _validate_initial_payload(request.payload)

    def binding(role: str, reference: ArtifactReference) -> dict[str, object]:
        return {
            "role": role,
            "media_type": REPLICA_ARTIFACT_MEDIA_TYPES[role],
            "artifact_id": reference.artifact_id,
            "sha256": reference.sha256,
            "byte_length": reference.byte_length,
        }

    return compute_replica_artifact_set_sha256(
        operation_id=request.operation_id,
        repository_id=cast(str, payload["repository_id"]),
        workspace_id=cast(str, payload["workspace_id"]),
        physical_target_id=authority.physical_target_id,
        replica_generation=authority.replica_generation,
        execution_owner_id=authority.execution_owner_id,
        object_format=cast(str, payload["object_format"]),
        source_state_sha256=cast(str, payload["source_state_sha256"]),
        reconciliation_fingerprint=cast(str, payload["reconciliation_fingerprint"]),
        bundle=binding("committed_bundle", _parse_artifact(payload["bundle"])),
        worktree=binding("worktree", _parse_artifact(payload["worktree"])),
        index_objects=binding("index_objects", _parse_artifact(payload["index_objects"])),
        limits_sha256=cast(str, payload["limits_sha256"]),
    )


def _receipt_json(stage: str, receipt: object) -> bytes:
    if stage == "ingest" and isinstance(receipt, IngestReceipt):
        value: dict[str, JsonValue] = {
            "bundle": _artifact_json(receipt.bundle),
            "index_objects": _artifact_json(receipt.index_objects),
            "receipt_id": receipt.receipt_id,
            "worktree": _artifact_json(receipt.worktree),
        }
    elif stage == "verify" and isinstance(receipt, VerifyReceipt):
        value = {
            "artifact_set_sha256": receipt.artifact_set_sha256,
            "bundle_sha256": receipt.bundle_sha256,
            "index_objects_sha256": receipt.index_objects_sha256,
            "receipt_id": receipt.receipt_id,
            "reconciliation_fingerprint": receipt.reconciliation_fingerprint,
            "source_state_sha256": receipt.source_state_sha256,
            "worktree_sha256": receipt.worktree_sha256,
        }
    elif stage == "publish" and isinstance(receipt, PublishReceipt):
        value = {
            "artifact_set_sha256": receipt.artifact_set_sha256,
            "execution_owner_id": receipt.execution_owner_id,
            "physical_target_id": receipt.physical_target_id,
            "publication_marker_sha256": receipt.publication_marker_sha256,
            "receipt_id": receipt.receipt_id,
            "replica_generation": receipt.replica_generation,
            "synchronization_receipt_id": receipt.synchronization_receipt_id,
        }
    elif stage == "admission" and isinstance(receipt, AdmissionReceipt):
        value = {
            "execution_owner_id": receipt.execution_owner_id,
            "receipt_id": receipt.receipt_id,
            "runtime_unit_id": receipt.runtime_unit_id,
            "session_socket_sha256": receipt.session_socket_sha256,
        }
    elif stage == "drain" and isinstance(receipt, DrainReceipt):
        value = {
            "drain_ack_receipt_id": receipt.drain_ack_receipt_id,
            "quiescence_receipt_id": receipt.quiescence_receipt_id,
        }
    elif stage == "export" and isinstance(receipt, ExportReceipt):
        value = {
            "bundle": _artifact_json(receipt.bundle),
            "export_receipt_id": receipt.export_receipt_id,
            "index_objects": _artifact_json(receipt.index_objects),
            "worktree": _artifact_json(receipt.worktree),
        }
    elif stage == "reclaim" and isinstance(receipt, ReclaimReceipt):
        value = {"reclaim_receipt_id": receipt.reclaim_receipt_id}
    else:
        raise TypeError("replica stage receipt type is invalid")
    encoded = canonical_json(value)
    if len(encoded) > RECEIPT_JSON_BYTES_MAX:
        raise ValueError("replica stage receipt is too large")
    return encoded


def _rejected_json(receipt: RejectedReceipt) -> bytes:
    if not isinstance(receipt, RejectedReceipt):
        raise TypeError("replica rejected receipt type is invalid")
    encoded = canonical_json({"receipt_id": receipt.receipt_id})
    if len(encoded) > RECEIPT_JSON_BYTES_MAX:
        raise ValueError("replica rejected receipt is too large")
    return encoded


def _canonical_object(raw: object, *, maximum: int, description: str) -> dict[str, object]:
    if not isinstance(raw, bytes) or not raw or len(raw) > maximum:
        raise ValueError(f"replica {description} is malformed")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"replica {description} is malformed") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise ValueError(f"replica {description} is not canonical")
    return cast(dict[str, object], value)


def _request_from_frame(frame: object, public_key: Ed25519PublicKey) -> BrokerRequest:
    if not isinstance(frame, bytes) or not frame or len(frame) > BROKER_FRAME_BYTES_MAX:
        raise ValueError("replica request frame is malformed")
    try:
        return parse_signed_request(frame, public_key=public_key)
    except BrokerProtocolError as exc:
        raise ValueError("replica request authentication failed") from exc


def _validate_request_frame(
    frame: object,
    request: BrokerRequest,
    public_key: Ed25519PublicKey,
) -> bytes:
    parsed = _request_from_frame(frame, public_key)
    if parsed != request:
        raise ValueError("replica request frame does not bind the request")
    return cast(bytes, frame)


def _validate_response_frame(
    frame: object,
    request: BrokerRequest,
    *,
    public_key: Ed25519PublicKey,
    state: str,
    stage: str,
    code: str | None,
    receipt_id: str | None,
) -> bytes:
    value = _canonical_object(
        frame,
        maximum=BROKER_RESPONSE_BYTES_MAX,
        description="response frame",
    )
    try:
        verified = verify_broker_response(
            cast(bytes, frame),
            public_key=public_key,
            expected_request=request,
        )
    except BrokerProtocolError as exc:
        raise ValueError("replica response authentication failed") from exc
    expected_status = "ok" if state in {"active", "completed"} else "error"
    if verified.status != expected_status:
        raise ValueError("replica response status is invalid")
    if (verified.error is None) != (expected_status == "ok"):
        raise ValueError("replica response error is invalid")
    if expected_status == "error" and verified.error != code:
        raise ValueError("replica response error does not match the result")
    expected_result: dict[str, JsonValue] = {"stage": stage, "state": state}
    if code is not None:
        expected_result["code"] = code
    if receipt_id is not None:
        expected_result["receipt_id"] = receipt_id
    if verified.result != expected_result:
        raise ValueError("replica response result is invalid")
    if value.get("result") != expected_result:
        raise ValueError("replica stored response result is invalid")
    return cast(bytes, frame)


def _parse_receipt(stage: str, raw: object) -> object:
    value = _canonical_object(raw, maximum=RECEIPT_JSON_BYTES_MAX, description="receipt")
    try:
        if stage == "ingest" and set(value) == {
            "bundle",
            "index_objects",
            "receipt_id",
            "worktree",
        }:
            return IngestReceipt(
                receipt_id=cast(str, value["receipt_id"]),
                bundle=_parse_artifact(value["bundle"]),
                worktree=_parse_artifact(value["worktree"]),
                index_objects=_parse_artifact(value["index_objects"]),
            )
        if stage == "verify" and set(value) == {
            "artifact_set_sha256",
            "bundle_sha256",
            "index_objects_sha256",
            "receipt_id",
            "reconciliation_fingerprint",
            "source_state_sha256",
            "worktree_sha256",
        }:
            return VerifyReceipt(
                receipt_id=cast(str, value["receipt_id"]),
                bundle_sha256=cast(str, value["bundle_sha256"]),
                worktree_sha256=cast(str, value["worktree_sha256"]),
                index_objects_sha256=cast(str, value["index_objects_sha256"]),
                source_state_sha256=cast(str, value["source_state_sha256"]),
                reconciliation_fingerprint=cast(str, value["reconciliation_fingerprint"]),
                artifact_set_sha256=cast(str, value["artifact_set_sha256"]),
            )
        if stage == "publish" and set(value) == {
            "artifact_set_sha256",
            "execution_owner_id",
            "physical_target_id",
            "publication_marker_sha256",
            "receipt_id",
            "replica_generation",
            "synchronization_receipt_id",
        }:
            return PublishReceipt(
                receipt_id=cast(str, value["receipt_id"]),
                physical_target_id=cast(str, value["physical_target_id"]),
                replica_generation=cast(int, value["replica_generation"]),
                execution_owner_id=cast(str, value["execution_owner_id"]),
                publication_marker_sha256=cast(str, value["publication_marker_sha256"]),
                synchronization_receipt_id=cast(str, value["synchronization_receipt_id"]),
                artifact_set_sha256=cast(str, value["artifact_set_sha256"]),
            )
        if stage == "admission" and set(value) == {
            "execution_owner_id",
            "receipt_id",
            "runtime_unit_id",
            "session_socket_sha256",
        }:
            return AdmissionReceipt(
                receipt_id=cast(str, value["receipt_id"]),
                execution_owner_id=cast(str, value["execution_owner_id"]),
                runtime_unit_id=cast(str, value["runtime_unit_id"]),
                session_socket_sha256=cast(str, value["session_socket_sha256"]),
            )
        if stage == "drain" and set(value) == {
            "drain_ack_receipt_id",
            "quiescence_receipt_id",
        }:
            return DrainReceipt(
                drain_ack_receipt_id=cast(str, value["drain_ack_receipt_id"]),
                quiescence_receipt_id=cast(str, value["quiescence_receipt_id"]),
            )
        if stage == "export" and set(value) == {
            "bundle",
            "export_receipt_id",
            "index_objects",
            "worktree",
        }:
            return ExportReceipt(
                export_receipt_id=cast(str, value["export_receipt_id"]),
                bundle=_parse_artifact(value["bundle"]),
                worktree=_parse_artifact(value["worktree"]),
                index_objects=_parse_artifact(value["index_objects"]),
            )
        if stage == "reclaim" and set(value) == {"reclaim_receipt_id"}:
            return ReclaimReceipt(reclaim_receipt_id=cast(str, value["reclaim_receipt_id"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("replica receipt is invalid") from exc
    raise ValueError("replica receipt fields are invalid")


def _parse_rejected_receipt(raw: object) -> RejectedReceipt:
    value = _canonical_object(raw, maximum=RECEIPT_JSON_BYTES_MAX, description="receipt")
    if set(value) != {"receipt_id"}:
        raise ValueError("replica rejected receipt fields are invalid")
    return RejectedReceipt(receipt_id=cast(str, value["receipt_id"]))


def _validate_receipt_binding(
    request: BrokerRequest,
    authority: ReplicaAuthority,
    stage: str,
    receipt: object,
) -> None:
    payload = _validate_initial_payload(request.payload)
    artifact_set_sha256 = cast(str, payload["artifact_set_sha256"])
    bundle = _parse_artifact(payload["bundle"])
    worktree = _parse_artifact(payload["worktree"])
    index_objects = _parse_artifact(payload["index_objects"])
    if isinstance(receipt, IngestReceipt):
        if (receipt.bundle, receipt.worktree, receipt.index_objects) != (
            bundle,
            worktree,
            index_objects,
        ):
            raise ValueError("replica ingest receipt conflicts with authenticated artifacts")
    elif isinstance(receipt, VerifyReceipt):
        if (
            receipt.bundle_sha256 != bundle.sha256
            or receipt.worktree_sha256 != worktree.sha256
            or receipt.index_objects_sha256 != index_objects.sha256
            or receipt.source_state_sha256 != payload["source_state_sha256"]
            or receipt.reconciliation_fingerprint != payload["reconciliation_fingerprint"]
            or receipt.artifact_set_sha256 != artifact_set_sha256
        ):
            raise ValueError("replica verify receipt conflicts with authenticated artifacts")
    elif isinstance(receipt, PublishReceipt):
        if (
            receipt.physical_target_id != authority.physical_target_id
            or receipt.replica_generation != authority.replica_generation
            or receipt.execution_owner_id != authority.execution_owner_id
            or receipt.artifact_set_sha256 != artifact_set_sha256
        ):
            raise ValueError("replica publish receipt conflicts with durable authority")
    elif isinstance(receipt, AdmissionReceipt):
        if receipt.execution_owner_id != authority.execution_owner_id:
            raise ValueError("replica admission receipt conflicts with durable authority")
    elif isinstance(receipt, DrainReceipt):
        pass
    elif isinstance(receipt, ExportReceipt):
        validate_distinct_artifact_ids(
            (
                receipt.bundle.artifact_id,
                receipt.worktree.artifact_id,
                receipt.index_objects.artifact_id,
            )
        )
    elif isinstance(receipt, ReclaimReceipt):
        pass
    else:
        raise TypeError("replica stage receipt type is invalid")


def _public_key_bytes(public_key: Ed25519PublicKey) -> bytes:
    if not isinstance(public_key, Ed25519PublicKey):
        raise TypeError("replica journal public key is invalid")
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _configuration_digest(
    expected_limits_sha256: str,
    request_public_key: Ed25519PublicKey,
    response_public_key: Ed25519PublicKey,
) -> str:
    value = canonical_json(
        {
            "expected_limits_sha256": expected_limits_sha256,
            "application_request_public_key": _public_key_bytes(request_public_key).hex(),
            "broker_response_public_key": _public_key_bytes(response_public_key).hex(),
        }
    )
    return hashlib.sha256(b"yinshi-replica-journal-configuration-v2\0" + value).hexdigest()


def _event(row: tuple[object, ...]) -> _Event:
    if len(row) != 27:
        raise ReplicaJournalV2SyncError("replica journal V2 row shape is invalid")
    try:
        event = _Event(
            event_id=cast(int, row[0]),
            append_receipt_id=cast(str, row[1]),
            database_incarnation=cast(str, row[2]),
            operation_id=cast(str, row[3]),
            event_type=cast(str, row[4]),
            stage=cast(str | None, row[5]),
            request_frame=None if row[6] is None else bytes(cast(bytes, row[6])),
            request_frame_sha256=cast(str | None, row[7]),
            request_broker_incarnation=cast(str | None, row[8]),
            request_type=cast(str | None, row[9]),
            request_nonce=cast(str | None, row[10]),
            request_connection_sequence=cast(int | None, row[11]),
            request_payload_digest=cast(str | None, row[12]),
            physical_target_id=cast(str | None, row[13]),
            replica_generation=cast(int | None, row[14]),
            execution_owner_id=cast(str | None, row[15]),
            initial_request_sha256=cast(str | None, row[16]),
            admission_receipt_id=cast(str | None, row[17]),
            application_drain_intent_receipt_id=cast(str | None, row[18]),
            owner_token=cast(str | None, row[19]),
            owner_broker_incarnation=cast(str | None, row[20]),
            stage_status=cast(str | None, row[21]),
            receipt_json=None if row[22] is None else bytes(cast(bytes, row[22])),
            unresolved_reason=cast(str | None, row[23]),
            response_frame=None if row[24] is None else bytes(cast(bytes, row[24])),
            response_frame_sha256=cast(str | None, row[25]),
            created_at=cast(str, row[26]),
        )
    except (TypeError, ValueError) as exc:
        raise ReplicaJournalV2SyncError("replica journal V2 row is malformed") from exc
    if (
        type(event.event_id) is not int
        or event.event_id < 1
        or not isinstance(event.append_receipt_id, str)
        or _APPEND_RECEIPT_PATTERN.fullmatch(event.append_receipt_id) is None
        or not isinstance(event.database_incarnation, str)
        or _TOKEN_PATTERN.fullmatch(event.database_incarnation) is None
        or not isinstance(event.operation_id, str)
        or not isinstance(event.created_at, str)
        or _TIMESTAMP_PATTERN.fullmatch(event.created_at) is None
    ):
        raise ReplicaJournalV2SyncError("replica journal V2 row is malformed")
    try:
        validate_replica_operation_id(event.operation_id)
    except (TypeError, ValueError) as exc:
        raise ReplicaJournalV2SyncError("replica journal V2 identity is malformed") from exc
    return event


class BrokerReplicaJournalV2:
    """Own synchronous V2 journal calls without retaining SQLite handles."""

    def __init__(
        self,
        path: Path,
        *,
        application_id: str,
        expected_limits_sha256: str,
        request_public_key: Ed25519PublicKey,
        response_public_key: Ed25519PublicKey,
    ) -> None:
        if not isinstance(path, Path):
            raise TypeError("replica journal V2 path must be a Path")
        if (
            not isinstance(application_id, str)
            or _APPLICATION_ID_PATTERN.fullmatch(application_id) is None
        ):
            raise ValueError("replica journal V2 application ID is invalid")
        self._path = path
        self._application_id = application_id
        self._expected_limits_sha256 = _digest(expected_limits_sha256, "configured limits SHA-256")
        _public_key_bytes(request_public_key)
        _public_key_bytes(response_public_key)
        self._request_public_key = request_public_key
        self._response_public_key = response_public_key
        self._configuration_sha256 = _configuration_digest(
            self._expected_limits_sha256,
            request_public_key,
            response_public_key,
        )
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _identity(request: BrokerRequest) -> tuple[str, str]:
        if not isinstance(request, BrokerRequest):
            raise TypeError("replica journal V2 request is invalid")
        return request.database_incarnation, request.operation_id

    def _connect(self) -> sqlite3.Connection:
        try:
            database = sqlite3.connect(self._path, timeout=5.0)
            database.execute("PRAGMA busy_timeout = 5000")
            database.execute("PRAGMA foreign_keys = ON")
            database.execute("PRAGMA synchronous = FULL")
            database.execute("PRAGMA fullfsync = ON")
            database.execute("PRAGMA journal_mode = DELETE")
            return database
        except (OSError, sqlite3.Error) as exc:
            raise ReplicaJournalV2SyncError("replica journal V2 synchronization failed") from exc

    def _validate_recovered_private_copy(
        self,
        candidate: Path,
        *,
        expect_wal: bool,
    ) -> None:
        database: sqlite3.Connection | None = None
        try:
            database = sqlite3.connect(candidate, timeout=5.0)
            database.execute("PRAGMA busy_timeout = 5000")
            database.execute("PRAGMA foreign_keys = ON")
            database.execute("PRAGMA synchronous = FULL")
            database.execute("PRAGMA fullfsync = ON")
            self._validate(database)
            if expect_wal:
                checkpoint = database.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
                if (
                    checkpoint is None
                    or len(checkpoint) != 3
                    or checkpoint[0] != 0
                    or not isinstance(checkpoint[1], int)
                    or checkpoint[1] < 1
                    or checkpoint[2] != checkpoint[1]
                ):
                    raise ReplicaJournalV2SyncError(
                        "replica journal V2 private WAL recovery is incomplete"
                    )
                journal_mode = database.execute("PRAGMA journal_mode = DELETE").fetchone()
                if journal_mode != ("delete",):
                    raise ReplicaJournalV2SyncError(
                        "replica journal V2 private WAL mode could not be closed"
                    )
                self._validate(database)
        except ReplicaJournalV2SyncError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise ReplicaJournalV2SyncError(
                "replica journal V2 private rollback recovery failed"
            ) from exc
        finally:
            if database is not None:
                database.close()
        private_rollback = Path(f"{candidate}-journal")
        try:
            with private_rollback.open("rb") as rollback_file:
                header = rollback_file.read(len(_SQLITE_ROLLBACK_MAGIC))
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ReplicaJournalV2SyncError(
                "replica journal V2 private rollback state is unreadable"
            ) from exc
        if header == _SQLITE_ROLLBACK_MAGIC:
            raise ReplicaJournalV2SyncError(
                "replica journal V2 private rollback recovery is incomplete"
            )

    def _recover_existing_rollback(self, rollback_path: Path) -> None:
        try:
            main_descriptor, main_snapshot = _open_pinned_file(self._path)
        except OSError as exc:
            raise ReplicaJournalV2SyncError(
                "replica journal V2 existing file is unreadable"
            ) from exc
        try:
            rollback_descriptor, rollback_snapshot = _open_pinned_file(rollback_path)
            try:
                if (
                    rollback_snapshot.size <= 512
                    or os.pread(
                        rollback_descriptor,
                        len(_SQLITE_ROLLBACK_MAGIC),
                        0,
                    )
                    != _SQLITE_ROLLBACK_MAGIC
                ):
                    raise ReplicaJournalV2SyncError("replica journal V2 rollback state is not hot")
                with tempfile.TemporaryDirectory(
                    prefix=".replica-journal-v2-recovery-",
                    dir=self._path.parent,
                ) as recovery_directory:
                    candidate = Path(recovery_directory) / "replica.sqlite3"
                    _copy_pinned_file(main_descriptor, main_snapshot, candidate)
                    _copy_pinned_file(
                        rollback_descriptor,
                        rollback_snapshot,
                        Path(f"{candidate}-journal"),
                    )
                    self._validate_recovered_private_copy(
                        candidate,
                        expect_wal=False,
                    )
            finally:
                try:
                    _finish_pinned_file(
                        rollback_path,
                        rollback_descriptor,
                        rollback_snapshot,
                    )
                finally:
                    os.close(rollback_descriptor)
        finally:
            try:
                _finish_pinned_file(self._path, main_descriptor, main_snapshot)
            finally:
                os.close(main_descriptor)
        database = self._connect()
        try:
            self._validate(database)
        finally:
            database.close()
        try:
            with rollback_path.open("rb") as rollback_file:
                header = rollback_file.read(len(_SQLITE_ROLLBACK_MAGIC))
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ReplicaJournalV2SyncError(
                "replica journal V2 recovered rollback state is unreadable"
            ) from exc
        if header == _SQLITE_ROLLBACK_MAGIC:
            raise ReplicaJournalV2SyncError("replica journal V2 rollback recovery is incomplete")
        try:
            rollback_path.unlink()
            parent = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except OSError as exc:
            raise ReplicaJournalV2SyncError(
                "replica journal V2 recovered rollback cleanup failed"
            ) from exc

    @staticmethod
    def _stale_sidecar_is_safe(
        pinned_file: _PinnedFile,
        metadata: os.stat_result,
    ) -> bool:
        snapshot = pinned_file.snapshot
        return (
            stat.S_ISREG(metadata.st_mode)
            and (metadata.st_dev, metadata.st_ino) == (snapshot.device, snapshot.inode)
            and metadata.st_mode == snapshot.mode
            and metadata.st_size == snapshot.size
            and metadata.st_uid == os.geteuid()
            and metadata.st_gid == os.getegid()
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == 0o600
        )

    def _cleanup_stale_wal_sidecars(self, sidecar_paths: list[Path]) -> None:
        state_name = (
            "SHM" if len(sidecar_paths) == 1 and sidecar_paths[0].name.endswith("-shm") else "WAL"
        )
        try:
            files = _open_pinned_files([self._path, *sidecar_paths])
        except OSError as exc:
            raise ReplicaJournalV2SyncError(
                f"replica journal V2 stale {state_name} state is unsafe"
            ) from exc
        try:
            for pinned_file in files[1:]:
                metadata = os.fstat(pinned_file.file_descriptor)
                if not self._stale_sidecar_is_safe(pinned_file, metadata):
                    raise ReplicaJournalV2SyncError(
                        f"replica journal V2 stale {state_name} state is unsafe"
                    )
                if pinned_file.path.name.endswith("-wal") and pinned_file.snapshot.size != 0:
                    raise ReplicaJournalV2SyncError(
                        "replica journal V2 stale WAL state is not empty"
                    )
            database = _connect_pinned_immutable(files[0].file_descriptor)
            try:
                self._validate(database)
                if database.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                    raise ReplicaJournalV2SyncError(
                        "replica journal V2 stale WAL main is not in DELETE mode"
                    )
            finally:
                database.close()
        finally:
            try:
                _finish_pinned_files(files)
            except OSError as exc:
                raise ReplicaJournalV2SyncError(
                    f"replica journal V2 stale {state_name} preservation failed"
                ) from exc
        for pinned_file in files[1:]:
            try:
                metadata = pinned_file.path.lstat()
            except OSError as exc:
                raise ReplicaJournalV2SyncError(
                    f"replica journal V2 stale {state_name} state changed before cleanup"
                ) from exc
            if not self._stale_sidecar_is_safe(pinned_file, metadata):
                raise ReplicaJournalV2SyncError(
                    f"replica journal V2 stale {state_name} state changed before cleanup"
                )
        try:
            for sidecar_path in sidecar_paths:
                sidecar_path.unlink()
            parent = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except OSError as exc:
            raise ReplicaJournalV2SyncError(
                f"replica journal V2 stale {state_name} cleanup failed"
            ) from exc

    def _recover_existing_wal(self, sidecar_paths: list[Path]) -> None:
        files = _open_pinned_files([self._path, *sidecar_paths])
        try:
            with tempfile.TemporaryDirectory(
                prefix=".replica-journal-v2-wal-recovery-",
                dir=self._path.parent,
            ) as recovery_directory:
                candidate = Path(recovery_directory) / "replica.sqlite3"
                _copy_pinned_file(
                    files[0].file_descriptor,
                    files[0].snapshot,
                    candidate,
                )
                for pinned_file in files[1:]:
                    suffix = str(pinned_file.path)[len(str(self._path)) :]
                    _copy_pinned_file(
                        pinned_file.file_descriptor,
                        pinned_file.snapshot,
                        Path(f"{candidate}{suffix}"),
                    )
                self._validate_recovered_private_copy(
                    candidate,
                    expect_wal=True,
                )
        finally:
            _finish_pinned_files(files)
        database = self._connect()
        try:
            self._validate(database)
        finally:
            database.close()
        wal_path = Path(f"{self._path}-wal")
        try:
            wal_path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ReplicaJournalV2SyncError(
                "replica journal V2 recovered WAL state is unreadable"
            ) from exc
        else:
            raise ReplicaJournalV2SyncError("replica journal V2 WAL recovery is incomplete")
        shm_path = Path(f"{self._path}-shm")
        try:
            shm_path.lstat()
        except FileNotFoundError:
            shm_exists = False
        except OSError as exc:
            raise ReplicaJournalV2SyncError(
                "replica journal V2 recovered SHM state is unreadable"
            ) from exc
        else:
            shm_exists = True
        main_descriptor = os.open(
            self._path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(main_descriptor)
        finally:
            os.close(main_descriptor)
        if shm_exists:
            self._cleanup_stale_wal_sidecars([shm_path])
        else:
            parent = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)

    @staticmethod
    def _rollback_journal_is_hot(rollback_path: Path) -> bool:
        try:
            file_descriptor, snapshot = _open_pinned_file(rollback_path)
        except OSError as exc:
            raise ReplicaJournalV2SyncError(
                "replica journal V2 rollback state is unreadable"
            ) from exc
        try:
            return (
                snapshot.size > 512
                and os.pread(
                    file_descriptor,
                    len(_SQLITE_ROLLBACK_MAGIC),
                    0,
                )
                == _SQLITE_ROLLBACK_MAGIC
            )
        finally:
            try:
                _finish_pinned_file(rollback_path, file_descriptor, snapshot)
            finally:
                os.close(file_descriptor)

    def _validate_existing_file_immutable(self) -> None:
        rollback_path = Path(f"{self._path}-journal")
        wal_path = Path(f"{self._path}-wal")
        shm_path = Path(f"{self._path}-shm")
        existing_sidecars: list[Path] = []
        sidecar_metadata: dict[Path, os.stat_result] = {}
        for sidecar_path in (rollback_path, wal_path, shm_path):
            try:
                metadata = sidecar_path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ReplicaJournalV2SyncError(
                    "replica journal V2 sidecar state is unreadable"
                ) from exc
            existing_sidecars.append(sidecar_path)
            sidecar_metadata[sidecar_path] = metadata
        wal_sidecars = [
            sidecar_path
            for sidecar_path in existing_sidecars
            if sidecar_path in {wal_path, shm_path}
        ]
        if wal_sidecars:
            if rollback_path in existing_sidecars:
                raise ReplicaJournalV2SyncError("replica journal V2 mixes rollback and WAL state")
            if wal_path not in wal_sidecars or sidecar_metadata[wal_path].st_size == 0:
                self._cleanup_stale_wal_sidecars(wal_sidecars)
            else:
                self._recover_existing_wal(wal_sidecars)
            return
        if rollback_path in existing_sidecars and self._rollback_journal_is_hot(rollback_path):
            self._recover_existing_rollback(rollback_path)
            return
        try:
            file_descriptor, snapshot = _open_pinned_file(self._path)
        except OSError as exc:
            raise ReplicaJournalV2SyncError(
                "replica journal V2 existing file is unreadable"
            ) from exc
        database: sqlite3.Connection | None = None
        try:
            database = _connect_pinned_immutable(file_descriptor)
            self._validate(database)
        finally:
            if database is not None:
                database.close()
            try:
                _finish_pinned_file(self._path, file_descriptor, snapshot)
            except OSError as exc:
                raise ReplicaJournalV2SyncError(
                    "replica journal V2 existing file preservation failed"
                ) from exc
            finally:
                os.close(file_descriptor)

    def _create_schema_in_empty_file(self) -> None:
        database = self._connect()
        try:
            database.execute("BEGIN EXCLUSIVE")
            if (
                database.execute("PRAGMA user_version").fetchone() != (0,)
                or database.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
                is not None
            ):
                raise ReplicaJournalV2SyncError(
                    "replica journal V2 empty file changed before initialization"
                )
            for statement in REPLICA_JOURNAL_V2_SCHEMA_STATEMENTS:
                database.execute(statement)
            journal_id = secrets.token_hex(16) + "_" + self._configuration_sha256
            database.execute(
                "INSERT INTO replica_journal_v2_meta VALUES (0, ?, ?, ?)",
                (
                    self._application_id,
                    REPLICA_JOURNAL_V2_SCHEMA_VERSION,
                    journal_id,
                ),
            )
            database.commit()
            self._validate(database)
        finally:
            database.close()

    def _initialize(self) -> None:
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._path.parent, 0o700)
            created = False
            try:
                metadata = self._path.lstat()
            except FileNotFoundError:
                try:
                    file_descriptor = os.open(
                        self._path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except OSError as exc:
                    raise ReplicaJournalV2SyncError(
                        "replica journal V2 creation race was detected"
                    ) from exc
                os.close(file_descriptor)
                metadata = self._path.lstat()
                created = True
            if not stat.S_ISREG(metadata.st_mode):
                raise ReplicaJournalV2SyncError("replica journal V2 path is unsafe")
            if metadata.st_size == 0:
                self._create_schema_in_empty_file()
            else:
                self._validate_existing_file_immutable()
            os.chmod(self._path, 0o600)
            parent = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
            with self._open():
                pass
            if created and self._path.stat().st_size == 0:
                raise ReplicaJournalV2SyncError(
                    "replica journal V2 creation did not persist schema"
                )
        except ReplicaJournalV2SyncError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise ReplicaJournalV2SyncError("replica journal V2 initialization failed") from exc

    def _open(self) -> sqlite3.Connection:
        database = self._connect()
        try:
            self._validate(database)
            return database
        except Exception:
            database.close()
            raise

    def _validate(self, database: sqlite3.Connection) -> None:
        try:
            if database.execute("PRAGMA user_version").fetchone() != (0,):
                raise ReplicaJournalV2SyncError("replica journal V2 user version is unsupported")
            if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ReplicaJournalV2SyncError("replica journal V2 integrity check failed")
            objects = database.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            actual = {
                (str(kind), str(name)): _normalized_sql(str(sql)) for kind, name, sql in objects
            }
            if actual != _expected_schema():
                raise ReplicaJournalV2SyncError("replica journal V2 schema is incompatible")
            meta = database.execute(
                "SELECT singleton, application_id, schema_version, journal_id "
                "FROM replica_journal_v2_meta"
            ).fetchall()
            expected_suffix = "_" + self._configuration_sha256
            if (
                len(meta) != 1
                or meta[0][0] != 0
                or meta[0][1] != self._application_id
                or meta[0][2] != REPLICA_JOURNAL_V2_SCHEMA_VERSION
                or not isinstance(meta[0][3], str)
                or _JOURNAL_ID_PATTERN.fullmatch(meta[0][3]) is None
                or not meta[0][3].endswith(expected_suffix)
            ):
                raise ReplicaJournalV2SyncError("replica journal V2 metadata is not configured")
            self._validate_events(database)
        except ReplicaJournalV2SyncError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise ReplicaJournalV2SyncError("replica journal V2 validation failed") from exc

    @staticmethod
    def _selected_columns() -> str:
        return (
            "event_id, append_receipt_id, database_incarnation, operation_id, "
            "event_type, stage, request_frame, request_frame_sha256, "
            "request_broker_incarnation, request_type, request_nonce, "
            "request_connection_sequence, request_payload_digest, "
            "physical_target_id, replica_generation, execution_owner_id, "
            "initial_request_sha256, admission_receipt_id, "
            "application_drain_intent_receipt_id, owner_token, "
            "owner_broker_incarnation, stage_status, receipt_json, "
            "unresolved_reason, response_frame, response_frame_sha256, created_at"
        )

    def _all_events(self, database: sqlite3.Connection) -> list[_Event]:
        rows = database.execute(
            f"SELECT {self._selected_columns()} FROM replica_journal_v2_events ORDER BY event_id"
        ).fetchall()
        return [_event(tuple(row)) for row in rows]

    def _rows_for(
        self,
        database: sqlite3.Connection,
        identity: tuple[str, str],
    ) -> list[_Event]:
        rows = database.execute(
            f"SELECT {self._selected_columns()} FROM replica_journal_v2_events "
            "WHERE database_incarnation = ? AND operation_id = ? ORDER BY event_id",
            identity,
        ).fetchall()
        return [_event(tuple(row)) for row in rows]

    def _validate_events(self, database: sqlite3.Connection) -> None:
        events = self._all_events(database)
        if [event.event_id for event in events] != list(range(1, len(events) + 1)):
            raise ReplicaJournalV2SyncError("replica journal V2 event sequence has gaps")
        if len({event.append_receipt_id for event in events}) != len(events):
            raise ReplicaJournalV2SyncError("replica journal V2 append receipt is duplicated")
        identities: list[tuple[str, str]] = []
        for event in events:
            identity = (event.database_incarnation, event.operation_id)
            if identity not in identities:
                identities.append(identity)
        for identity in identities:
            self._replay(
                [
                    event
                    for event in events
                    if (event.database_incarnation, event.operation_id) == identity
                ]
            )

    @staticmethod
    def _all_null(event: _Event, fields: tuple[str, ...]) -> bool:
        return all(getattr(event, name) is None for name in fields)

    def _validate_request_columns(
        self,
        event: _Event,
        expected_type: str,
    ) -> BrokerRequest:
        required = (
            event.request_frame,
            event.request_frame_sha256,
            event.request_broker_incarnation,
            event.request_type,
            event.request_nonce,
            event.request_connection_sequence,
            event.request_payload_digest,
            event.physical_target_id,
            event.replica_generation,
            event.execution_owner_id,
        )
        if any(item is None for item in required):
            raise ReplicaJournalV2SyncError("replica journal V2 request fields are incomplete")
        request = _request_from_frame(event.request_frame, self._request_public_key)
        if (
            event.request_frame_sha256
            != hashlib.sha256(cast(bytes, event.request_frame)).hexdigest()
            or request.broker_incarnation != event.request_broker_incarnation
            or request.request_type != event.request_type
            or request.nonce != event.request_nonce
            or request.connection_sequence != event.request_connection_sequence
            or request.payload_digest != event.request_payload_digest
            or request.database_incarnation != event.database_incarnation
            or request.operation_id != event.operation_id
            or request.request_type != expected_type
        ):
            raise ReplicaJournalV2SyncError("replica journal V2 request binding is invalid")
        return request

    def _validate_field_matrix(self, event: _Event) -> None:
        request_fields = (
            "request_frame",
            "request_frame_sha256",
            "request_broker_incarnation",
            "request_type",
            "request_nonce",
            "request_connection_sequence",
            "request_payload_digest",
            "physical_target_id",
            "replica_generation",
            "execution_owner_id",
        )
        continuation_fields = (
            "initial_request_sha256",
            "admission_receipt_id",
            "application_drain_intent_receipt_id",
        )
        if event.event_type == "accepted":
            if (
                event.stage is not None
                or any(getattr(event, field) is None for field in request_fields)
                or not self._all_null(
                    event,
                    continuation_fields
                    + (
                        "owner_token",
                        "owner_broker_incarnation",
                        "stage_status",
                        "receipt_json",
                        "unresolved_reason",
                        "response_frame",
                        "response_frame_sha256",
                    ),
                )
            ):
                raise ReplicaJournalV2SyncError("replica journal V2 acceptance fields are invalid")
            return
        if event.event_type == "drain_continuation_accepted":
            if (
                event.stage is not None
                or any(getattr(event, field) is None for field in request_fields)
                or any(getattr(event, field) is None for field in continuation_fields)
                or event.owner_token is None
                or not self._all_null(
                    event,
                    (
                        "owner_broker_incarnation",
                        "stage_status",
                        "receipt_json",
                        "unresolved_reason",
                        "response_frame",
                        "response_frame_sha256",
                    ),
                )
            ):
                raise ReplicaJournalV2SyncError(
                    "replica journal V2 continuation fields are invalid"
                )
            return
        if not self._all_null(event, request_fields + continuation_fields):
            raise ReplicaJournalV2SyncError("replica journal V2 nonrequest fields are invalid")
        if event.event_type == "authority_claimed":
            valid = (
                event.stage is None
                and event.owner_token is not None
                and event.owner_broker_incarnation is not None
                and self._all_null(
                    event,
                    (
                        "stage_status",
                        "receipt_json",
                        "unresolved_reason",
                        "response_frame",
                        "response_frame_sha256",
                    ),
                )
            )
        elif event.event_type == "stage_started":
            valid = (
                event.stage in _STAGE_INDEX
                and event.owner_token is not None
                and self._all_null(
                    event,
                    (
                        "owner_broker_incarnation",
                        "stage_status",
                        "receipt_json",
                        "unresolved_reason",
                        "response_frame",
                        "response_frame_sha256",
                    ),
                )
            )
        elif event.event_type == "stage_outcome":
            valid = (
                event.stage in _STAGE_INDEX
                and event.owner_token is not None
                and event.owner_broker_incarnation is None
                and event.stage_status == "completed"
                and event.receipt_json is not None
                and event.unresolved_reason is None
                and ((event.stage == "reclaim") == (event.response_frame is not None))
                and ((event.stage == "reclaim") == (event.response_frame_sha256 is not None))
            )
        elif event.event_type == "stage_rejected":
            valid = (
                event.stage in _STAGE_INDEX
                and event.owner_token is not None
                and event.owner_broker_incarnation is None
                and event.stage_status in _REJECTED_STATUSES[event.stage]
                and event.receipt_json is not None
                and event.unresolved_reason is None
                and event.response_frame is not None
                and event.response_frame_sha256 is not None
            )
        elif event.event_type == "stage_unresolved":
            valid = (
                event.stage in _STAGE_INDEX
                and event.owner_token is not None
                and self._all_null(
                    event,
                    ("owner_broker_incarnation", "stage_status", "receipt_json"),
                )
                and event.unresolved_reason is not None
                and event.response_frame is not None
                and event.response_frame_sha256 is not None
            )
        elif event.event_type == "lifecycle_paused":
            valid = (
                event.stage is None
                and event.owner_token is not None
                and self._all_null(
                    event,
                    (
                        "owner_broker_incarnation",
                        "stage_status",
                        "receipt_json",
                        "unresolved_reason",
                    ),
                )
                and event.response_frame is not None
                and event.response_frame_sha256 is not None
            )
        else:
            valid = False
        if not valid:
            raise ReplicaJournalV2SyncError("replica journal V2 event fields are invalid")

    def _replay(self, events: list[_Event]) -> _Replay:
        if not events:
            raise ReplicaJournalV2SyncError("replica journal V2 history is empty")
        for event in events:
            self._validate_field_matrix(event)
        accepted = events[0]
        if accepted.event_type != "accepted":
            raise ReplicaJournalV2SyncError("replica journal V2 acceptance is not first")
        initial_request = self._validate_request_columns(accepted, "replica.lifecycle")
        payload = _validate_initial_payload(initial_request.payload)
        authority = ReplicaAuthority(
            physical_target_id=cast(str, accepted.physical_target_id),
            replica_generation=cast(int, accepted.replica_generation),
            execution_owner_id=cast(str, accepted.execution_owner_id),
        )
        if (
            parse_replica_authority(payload["authority"]) != authority
            or payload["limits_sha256"] != self._expected_limits_sha256
            or payload["artifact_set_sha256"]
            != _expected_artifact_set_sha256(initial_request, authority)
        ):
            raise ReplicaJournalV2SyncError("replica journal V2 accepted payload is not configured")
        initial_sha256 = hashlib.sha256(cast(bytes, accepted.request_frame)).hexdigest()
        owner_token: str | None = None
        owner_broker_incarnation: str | None = None
        next_stage_index = 0
        stage_started = False
        paused = False
        active_response: bytes | None = None
        drain_request: BrokerRequest | None = None
        drain_frame: bytes | None = None
        continuation: ReplicaDrainContinuation | None = None
        terminal_state: str | None = None
        terminal_stage: str | None = None
        terminal_response: bytes | None = None
        receipt_values: list[object | None] = [None] * len(_STAGES)
        admission_pending_pause = False
        previous_event_type = "accepted"
        for event in events[1:]:
            if terminal_state is not None:
                raise ReplicaJournalV2SyncError(
                    "replica journal V2 contains an event after terminal state"
                )
            if admission_pending_pause and event.event_type != "lifecycle_paused":
                raise ReplicaJournalV2SyncError("replica journal V2 admission pause is not atomic")
            if event.event_type == "authority_claimed":
                if owner_token is not None or previous_event_type != "accepted":
                    raise ReplicaJournalV2SyncError("replica journal V2 authority order is invalid")
                if event.owner_broker_incarnation != initial_request.broker_incarnation:
                    raise ReplicaJournalV2SyncError("replica journal V2 owner broker is invalid")
                try:
                    owner_token = _token(event.owner_token, "owner token")
                    owner_broker_incarnation = _token(
                        event.owner_broker_incarnation,
                        "owner broker incarnation",
                    )
                except ValueError as exc:
                    raise ReplicaJournalV2SyncError(
                        "replica journal V2 authority is malformed"
                    ) from exc
            elif event.event_type == "stage_started":
                if (
                    owner_token is None
                    or event.owner_token != owner_token
                    or stage_started
                    or paused
                    and continuation is None
                    or next_stage_index >= len(_STAGES)
                    or event.stage != _STAGES[next_stage_index]
                ):
                    raise ReplicaJournalV2SyncError(
                        "replica journal V2 stage start order is invalid"
                    )
                stage_started = True
            elif event.event_type in {
                "stage_outcome",
                "stage_rejected",
                "stage_unresolved",
            }:
                expected_stage = (
                    None if next_stage_index >= len(_STAGES) else _STAGES[next_stage_index]
                )
                direct_foreign_pause = (
                    event.event_type == "stage_unresolved"
                    and event.stage == "drain"
                    and event.unresolved_reason == "broker_restart_unknown"
                    and paused
                    and not stage_started
                )
                if (
                    owner_token is None
                    or event.owner_token != owner_token
                    or event.stage != expected_stage
                    or not (stage_started or direct_foreign_pause)
                ):
                    raise ReplicaJournalV2SyncError(
                        "replica journal V2 completion order is invalid"
                    )
                response_request = (
                    drain_request if event.stage in _CONTINUATION_STAGES else initial_request
                )
                if direct_foreign_pause:
                    response_request = drain_request or initial_request
                if response_request is None:
                    raise ReplicaJournalV2SyncError(
                        "replica journal V2 response request is missing"
                    )
                if event.event_type == "stage_outcome":
                    receipt = _parse_receipt(cast(str, event.stage), event.receipt_json)
                    _validate_receipt_binding(
                        initial_request,
                        authority,
                        cast(str, event.stage),
                        receipt,
                    )
                    receipt_values[next_stage_index] = receipt
                    if event.stage == "reclaim":
                        if not isinstance(receipt, ReclaimReceipt):
                            raise ReplicaJournalV2SyncError(
                                "replica journal V2 reclaim receipt is invalid"
                            )
                        _validate_response_frame(
                            event.response_frame,
                            response_request,
                            public_key=self._response_public_key,
                            state="completed",
                            stage="reclaim",
                            code=None,
                            receipt_id=receipt.reclaim_receipt_id,
                        )
                        terminal_state = "completed"
                        terminal_stage = "reclaim"
                        terminal_response = event.response_frame
                    else:
                        if event.stage == "admission":
                            admission_pending_pause = True
                        next_stage_index += 1
                        stage_started = False
                elif event.event_type == "stage_rejected":
                    rejected = _parse_rejected_receipt(event.receipt_json)
                    if event.stage_status not in _REJECTED_STATUSES[cast(str, event.stage)]:
                        raise ReplicaJournalV2SyncError(
                            "replica journal V2 rejection status is invalid"
                        )
                    _validate_response_frame(
                        event.response_frame,
                        response_request,
                        public_key=self._response_public_key,
                        state="rejected",
                        stage=cast(str, event.stage),
                        code=event.stage_status,
                        receipt_id=rejected.receipt_id,
                    )
                    terminal_state = "rejected"
                    terminal_stage = event.stage
                    terminal_response = event.response_frame
                else:
                    valid_reasons = (
                        _BASE_UNRESOLVED_REASONS | _STAGE_UNRESOLVED_REASONS[cast(str, event.stage)]
                    )
                    if event.unresolved_reason not in valid_reasons:
                        raise ReplicaJournalV2SyncError(
                            "replica journal V2 unresolved reason is invalid"
                        )
                    _validate_response_frame(
                        event.response_frame,
                        response_request,
                        public_key=self._response_public_key,
                        state="unresolved",
                        stage=cast(str, event.stage),
                        code=event.unresolved_reason,
                        receipt_id=None,
                    )
                    terminal_state = "unresolved"
                    terminal_stage = event.stage
                    terminal_response = event.response_frame
            elif event.event_type == "lifecycle_paused":
                admission = receipt_values[_STAGE_INDEX["admission"]]
                if (
                    not admission_pending_pause
                    or not isinstance(admission, AdmissionReceipt)
                    or event.owner_token != owner_token
                    or paused
                ):
                    raise ReplicaJournalV2SyncError("replica journal V2 pause order is invalid")
                _validate_response_frame(
                    event.response_frame,
                    initial_request,
                    public_key=self._response_public_key,
                    state="active",
                    stage="admission",
                    code=None,
                    receipt_id=admission.receipt_id,
                )
                active_response = event.response_frame
                paused = True
                admission_pending_pause = False
            elif event.event_type == "drain_continuation_accepted":
                if (
                    not paused
                    or continuation is not None
                    or event.owner_token != owner_token
                    or owner_broker_incarnation is None
                ):
                    raise ReplicaJournalV2SyncError(
                        "replica journal V2 continuation order is invalid"
                    )
                drain_request = self._validate_request_columns(event, "replica.drain")
                parsed = replica_drain_continuation_from_request(drain_request)
                if (
                    parsed.authority != authority
                    or event.physical_target_id != parsed.authority.physical_target_id
                    or event.replica_generation != parsed.authority.replica_generation
                    or event.execution_owner_id != parsed.authority.execution_owner_id
                    or parsed.initial_request_sha256 != initial_sha256
                    or event.initial_request_sha256 != parsed.initial_request_sha256
                    or event.admission_receipt_id != parsed.admission_receipt_id
                    or event.application_drain_intent_receipt_id
                    != parsed.application_drain_intent_receipt_id
                    or drain_request.broker_incarnation != owner_broker_incarnation
                ):
                    raise ReplicaJournalV2SyncError(
                        "replica journal V2 continuation binding is invalid"
                    )
                admission = receipt_values[_STAGE_INDEX["admission"]]
                if (
                    not isinstance(admission, AdmissionReceipt)
                    or parsed.admission_receipt_id != admission.receipt_id
                ):
                    raise ReplicaJournalV2SyncError(
                        "replica journal V2 continuation admission is invalid"
                    )
                continuation = parsed
                drain_frame = event.request_frame
            else:
                raise ReplicaJournalV2SyncError("replica journal V2 event type is invalid")
            if event.response_frame is not None and (
                event.response_frame_sha256 != hashlib.sha256(event.response_frame).hexdigest()
            ):
                raise ReplicaJournalV2SyncError("replica journal V2 response digest is invalid")
            previous_event_type = event.event_type
        if admission_pending_pause:
            raise ReplicaJournalV2SyncError("replica journal V2 admission pause is missing")
        return _Replay(
            initial_request=initial_request,
            initial_request_frame=cast(bytes, accepted.request_frame),
            authority=authority,
            owner_token=owner_token,
            owner_broker_incarnation=owner_broker_incarnation,
            next_stage_index=next_stage_index,
            stage_started=stage_started,
            paused=paused,
            active_response_frame=active_response,
            drain_request=drain_request,
            drain_request_frame=drain_frame,
            continuation=continuation,
            terminal_state=terminal_state,
            terminal_stage=terminal_stage,
            terminal_response_frame=terminal_response,
            receipts=CompletedReplicaReceipts(
                ingest=cast(IngestReceipt | None, receipt_values[0]),
                verify=cast(VerifyReceipt | None, receipt_values[1]),
                publish=cast(PublishReceipt | None, receipt_values[2]),
                admission=cast(AdmissionReceipt | None, receipt_values[3]),
                drain=cast(DrainReceipt | None, receipt_values[4]),
                export=cast(ExportReceipt | None, receipt_values[5]),
                reclaim=cast(ReclaimReceipt | None, receipt_values[6]),
            ),
        )

    def _journal_id(self, database: sqlite3.Connection) -> str:
        row = database.execute(
            "SELECT journal_id FROM replica_journal_v2_meta WHERE singleton = 0"
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            raise ReplicaJournalV2SyncError("replica journal V2 ID is missing")
        return row[0]

    def _position(self, database: sqlite3.Connection, event: _Event) -> ReplicaJournalPosition:
        return ReplicaJournalPosition(
            journal_id=self._journal_id(database),
            sequence=event.event_id,
            append_receipt_id=event.append_receipt_id,
        )

    @staticmethod
    def _decision(replay: _Replay, *, initial: bool) -> ReplicaJournalV2Decision:
        if initial and replay.paused:
            return ReplicaJournalV2Decision(
                state="active",
                stage="admission",
                response_frame=replay.active_response_frame,
                owner_broker_incarnation=replay.owner_broker_incarnation,
            )
        if replay.terminal_state is not None:
            return ReplicaJournalV2Decision(
                state=replay.terminal_state,
                stage=replay.terminal_stage,
                response_frame=replay.terminal_response_frame,
                owner_broker_incarnation=replay.owner_broker_incarnation,
            )
        if not initial and replay.continuation is None:
            return ReplicaJournalV2Decision(
                state="absent",
                stage=None,
                response_frame=None,
                owner_broker_incarnation=replay.owner_broker_incarnation,
            )
        if replay.owner_token is None:
            return ReplicaJournalV2Decision("accepted", None, None, None)
        if replay.next_stage_index >= len(_STAGES):
            raise ReplicaJournalV2SyncError(
                "replica journal V2 nonterminal lifecycle has no next stage"
            )
        return ReplicaJournalV2Decision(
            state="in_flight" if replay.stage_started else "claimed",
            stage=_STAGES[replay.next_stage_index],
            response_frame=None,
            owner_broker_incarnation=replay.owner_broker_incarnation,
        )

    def _load_replay(
        self,
        database: sqlite3.Connection,
        request: BrokerRequest,
    ) -> _Replay | None:
        events = self._rows_for(database, self._identity(request))
        if not events:
            return None
        replay = self._replay(events)
        if request.request_type == "replica.lifecycle":
            if replay.initial_request != request:
                raise ReplicaJournalV2ConflictError(
                    "replica journal V2 initial request conflicts with durable state"
                )
        elif request.request_type == "replica.drain":
            if replay.drain_request is None:
                raise ReplicaJournalV2ConflictError(
                    "replica journal V2 drain continuation is missing"
                )
            if replay.drain_request != request:
                raise ReplicaJournalV2ConflictError(
                    "replica journal V2 drain request conflicts with durable state"
                )
        else:
            raise ValueError("replica journal V2 request type is invalid")
        return replay

    def initial_status(self, request: BrokerRequest) -> ReplicaJournalV2Decision:
        if request.request_type != "replica.lifecycle":
            raise ValueError("replica journal V2 initial request type is invalid")
        database = self._open()
        try:
            replay = self._load_replay(database, request)
            if replay is None:
                return ReplicaJournalV2Decision("absent", None, None, None)
            return self._decision(replay, initial=True)
        finally:
            database.close()

    def drain_status(self, request: BrokerRequest) -> ReplicaJournalV2Decision:
        if request.request_type != "replica.drain":
            raise ValueError("replica journal V2 drain request type is invalid")
        database = self._open()
        try:
            events = self._rows_for(database, self._identity(request))
            if not events:
                return ReplicaJournalV2Decision("absent", None, None, None)
            replay = self._replay(events)
            if replay.drain_request is None:
                return ReplicaJournalV2Decision(
                    "absent", None, None, replay.owner_broker_incarnation
                )
            if replay.drain_request != request:
                raise ReplicaJournalV2ConflictError(
                    "replica journal V2 drain request conflicts with durable state"
                )
            return self._decision(replay, initial=False)
        finally:
            database.close()

    def _validate_rejection(self, stage: str, status: str, receipt: RejectedReceipt) -> None:
        self._validate_stage(stage)
        if status not in _REJECTED_STATUSES[stage]:
            raise ValueError("replica rejection status is invalid")
        _rejected_json(receipt)

    def _validate_unresolved_reason(self, stage: str, reason: str) -> None:
        self._validate_stage(stage)
        if reason not in _BASE_UNRESOLVED_REASONS | _STAGE_UNRESOLVED_REASONS[stage]:
            raise ValueError("replica unresolved reason is invalid")

    @staticmethod
    def _validate_stage(stage: str) -> None:
        if not isinstance(stage, str) or stage not in _STAGE_INDEX:
            raise ValueError("replica stage is invalid")

    def require_response_signer(self, private_key: Ed25519PrivateKey) -> None:
        """Require a private key matching the stable configured response key."""
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError("replica response signer is invalid")
        if _public_key_bytes(private_key.public_key()) != _public_key_bytes(
            self._response_public_key
        ):
            raise ReplicaJournalV2SyncError("replica response signer does not match configured key")

    def accept_initial(
        self,
        request: BrokerRequest,
        request_frame: bytes,
        authority: ReplicaAuthority,
    ) -> ReplicaJournalV2Decision:
        if request.request_type != "replica.lifecycle":
            raise ValueError("replica journal V2 initial request type is invalid")
        try:
            frame = _validate_request_frame(request_frame, request, self._request_public_key)
        except ValueError as exc:
            raise ReplicaJournalV2ConflictError(
                "replica initial request authentication failed"
            ) from exc
        if not isinstance(authority, ReplicaAuthority):
            raise TypeError("replica authority is invalid")
        payload = _validate_initial_payload(request.payload)
        if parse_replica_authority(payload["authority"]) != authority:
            raise ReplicaJournalV2ConflictError(
                "replica initial request authority differs from supplied authority"
            )
        if payload["limits_sha256"] != self._expected_limits_sha256:
            raise ReplicaJournalV2ConflictError(
                "replica initial request limit profile is not configured"
            )
        if payload["artifact_set_sha256"] != _expected_artifact_set_sha256(request, authority):
            raise ReplicaJournalV2ConflictError("replica initial artifact set identity is invalid")
        identity = self._identity(request)
        database = self._open()
        try:
            events = self._rows_for(database, identity)
            if events:
                replay = self._replay(events)
                if (
                    replay.initial_request != request
                    or replay.initial_request_frame != frame
                    or replay.authority != authority
                ):
                    raise ReplicaJournalV2ConflictError(
                        "replica initial acceptance conflicts with durable state"
                    )
                return self._decision(replay, initial=True)
        finally:
            database.close()
        values = self._event_values(
            identity,
            event_type="accepted",
            request_frame=frame,
            authority=authority,
        )
        self._append_many(identity, [values])
        return self.initial_status(request)

    def claim_authority(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        broker_incarnation: str,
    ) -> ReplicaJournalV2Decision:
        if request.request_type != "replica.lifecycle":
            raise ValueError("replica journal V2 initial request type is invalid")
        _token(owner_token, "owner token")
        _token(broker_incarnation, "owner broker incarnation")
        if broker_incarnation != request.broker_incarnation:
            raise ReplicaJournalV2ConflictError(
                "replica owner broker differs from authenticated initial request"
            )
        identity = self._identity(request)
        database = self._open()
        try:
            replay = self._load_replay(database, request)
            if replay is None:
                raise ReplicaJournalV2ConflictError("replica initial acceptance is missing")
            if replay.owner_token is not None:
                if (
                    replay.owner_token != owner_token
                    or replay.owner_broker_incarnation != broker_incarnation
                ):
                    raise ReplicaJournalV2ConflictError(
                        "replica authority conflicts with durable owner"
                    )
                return self._decision(replay, initial=True)
        finally:
            database.close()
        values = self._event_values(
            identity,
            event_type="authority_claimed",
            owner_token=owner_token,
            owner_broker_incarnation=broker_incarnation,
        )
        self._append_many(identity, [values])
        return self.initial_status(request)

    def _require_active_stage(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
    ) -> _Replay:
        self._validate_stage(stage)
        _token(owner_token, "owner token")
        if (stage in _INITIAL_STAGES and request.request_type != "replica.lifecycle") or (
            stage in _CONTINUATION_STAGES and request.request_type != "replica.drain"
        ):
            raise ReplicaJournalV2ConflictError("replica stage request boundary conflicts")
        database = self._open()
        try:
            replay = self._load_replay(database, request)
            if replay is None:
                raise ReplicaJournalV2ConflictError("replica acceptance is missing")
            if replay.terminal_state is not None:
                raise ReplicaJournalV2ConflictError("replica lifecycle is already terminal")
            if replay.owner_token != owner_token:
                raise ReplicaJournalV2ConflictError("replica lifecycle owner conflicts")
            if replay.next_stage_index >= len(_STAGES) or _STAGES[replay.next_stage_index] != stage:
                raise ReplicaJournalV2ConflictError("replica stage order conflicts")
            if stage in _CONTINUATION_STAGES and replay.continuation is None:
                raise ReplicaJournalV2ConflictError("replica drain continuation is not durable")
            return replay
        finally:
            database.close()

    def _existing_stage_start(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
    ) -> ReplicaJournalPosition | None:
        identity = self._identity(request)
        database = self._open()
        try:
            self._load_replay(database, request)
            starts = [
                event
                for event in self._rows_for(database, identity)
                if event.event_type == "stage_started" and event.stage == stage
            ]
            if not starts:
                return None
            if starts[0].owner_token != owner_token:
                raise ReplicaJournalV2ConflictError("replica stage start conflicts")
            return self._position(database, starts[0])
        finally:
            database.close()

    def begin_stage(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
    ) -> ReplicaJournalPosition:
        self._validate_stage(stage)
        _token(owner_token, "owner token")
        identity = self._identity(request)
        existing = self._existing_stage_start(
            request,
            owner_token=owner_token,
            stage=stage,
        )
        if existing is not None:
            return existing
        try:
            replay = self._require_active_stage(
                request,
                owner_token=owner_token,
                stage=stage,
            )
        except ReplicaJournalV2ConflictError:
            existing = self._existing_stage_start(
                request,
                owner_token=owner_token,
                stage=stage,
            )
            if existing is not None:
                return existing
            raise
        if replay.stage_started:
            existing = self._existing_stage_start(
                request,
                owner_token=owner_token,
                stage=stage,
            )
            if existing is not None:
                return existing
            raise ReplicaJournalV2ConflictError("replica stage is already started")
        values = self._event_values(
            identity,
            event_type="stage_started",
            stage=stage,
            owner_token=owner_token,
        )
        return self._append_many(identity, [values])[-1]

    def complete_stage(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
        receipt: object,
        response_frame: bytes | None = None,
    ) -> ReplicaJournalPosition:
        self._validate_stage(stage)
        if stage == "admission":
            raise ReplicaJournalV2ConflictError("replica admission must use atomic pause")
        receipt_json = _receipt_json(stage, receipt)
        terminal_response: bytes | None = None
        if stage == "reclaim":
            if not isinstance(receipt, ReclaimReceipt) or response_frame is None:
                raise ValueError("replica reclaim response is required")
            terminal_response = _validate_response_frame(
                response_frame,
                request,
                public_key=self._response_public_key,
                state="completed",
                stage="reclaim",
                code=None,
                receipt_id=receipt.reclaim_receipt_id,
            )
        elif response_frame is not None:
            raise ValueError("replica nonterminal response is not permitted")
        identity = self._identity(request)
        existing = self._existing_stage_completion(
            request,
            stage=stage,
            event_type="stage_outcome",
            owner_token=owner_token,
            stage_status="completed",
            receipt_json=receipt_json,
            unresolved_reason=None,
            response_frame=terminal_response,
        )
        if existing is not None:
            return existing
        try:
            replay = self._require_active_stage(
                request,
                owner_token=owner_token,
                stage=stage,
            )
        except ReplicaJournalV2ConflictError:
            existing = self._existing_stage_completion(
                request,
                stage=stage,
                event_type="stage_outcome",
                owner_token=owner_token,
                stage_status="completed",
                receipt_json=receipt_json,
                unresolved_reason=None,
                response_frame=terminal_response,
            )
            if existing is not None:
                return existing
            raise
        _validate_receipt_binding(replay.initial_request, replay.authority, stage, receipt)
        if not replay.stage_started:
            raise ReplicaJournalV2ConflictError("replica stage start is missing")
        values = self._event_values(
            identity,
            event_type="stage_outcome",
            stage=stage,
            owner_token=owner_token,
            stage_status="completed",
            receipt_json=receipt_json,
            response_frame=terminal_response,
        )
        return self._append_many(identity, [values])[-1]

    def _existing_pause(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        receipt_json: bytes,
        response_frame: bytes,
    ) -> ReplicaJournalPosition | None:
        identity = self._identity(request)
        database = self._open()
        try:
            replay = self._load_replay(database, request)
            if replay is None:
                return None
            events = self._rows_for(database, identity)
            pauses = [event for event in events if event.event_type == "lifecycle_paused"]
            outcomes = [
                event
                for event in events
                if event.event_type == "stage_outcome" and event.stage == "admission"
            ]
            if not pauses and not outcomes:
                return None
            if (
                len(pauses) != 1
                or len(outcomes) != 1
                or outcomes[0].owner_token != owner_token
                or outcomes[0].stage_status != "completed"
                or outcomes[0].receipt_json != receipt_json
                or pauses[0].owner_token != owner_token
                or pauses[0].response_frame != response_frame
            ):
                raise ReplicaJournalV2ConflictError(
                    "replica admission pause conflicts with durable state"
                )
            return self._position(database, pauses[0])
        finally:
            database.close()

    def pause_after_admission(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        receipt: AdmissionReceipt,
        response_frame: bytes,
    ) -> ReplicaJournalPosition:
        if request.request_type != "replica.lifecycle":
            raise ReplicaJournalV2ConflictError("replica pause must bind the initial request")
        _token(owner_token, "owner token")
        receipt_json = _receipt_json("admission", receipt)
        response = _validate_response_frame(
            response_frame,
            request,
            public_key=self._response_public_key,
            state="active",
            stage="admission",
            code=None,
            receipt_id=receipt.receipt_id,
        )
        identity = self._identity(request)
        existing = self._existing_pause(
            request,
            owner_token=owner_token,
            receipt_json=receipt_json,
            response_frame=response,
        )
        if existing is not None:
            return existing
        try:
            replay = self._require_active_stage(
                request,
                owner_token=owner_token,
                stage="admission",
            )
        except ReplicaJournalV2ConflictError:
            existing = self._existing_pause(
                request,
                owner_token=owner_token,
                receipt_json=receipt_json,
                response_frame=response,
            )
            if existing is not None:
                return existing
            raise
        if not replay.stage_started:
            existing = self._existing_pause(
                request,
                owner_token=owner_token,
                receipt_json=receipt_json,
                response_frame=response,
            )
            if existing is not None:
                return existing
            raise ReplicaJournalV2ConflictError("replica admission stage start is missing")
        _validate_receipt_binding(replay.initial_request, replay.authority, "admission", receipt)
        outcome = self._event_values(
            identity,
            event_type="stage_outcome",
            stage="admission",
            owner_token=owner_token,
            stage_status="completed",
            receipt_json=receipt_json,
        )
        pause = self._event_values(
            identity,
            event_type="lifecycle_paused",
            owner_token=owner_token,
            response_frame=response,
        )
        return self._append_many(identity, [outcome, pause])[-1]

    def accept_drain_continuation(
        self,
        request: BrokerRequest,
        request_frame: bytes,
        continuation: ReplicaDrainContinuation,
    ) -> ReplicaJournalV2Decision:
        if request.request_type != "replica.drain":
            raise ValueError("replica drain request type is invalid")
        try:
            frame = _validate_request_frame(request_frame, request, self._request_public_key)
        except ValueError as exc:
            raise ReplicaJournalV2ConflictError(
                "replica drain request authentication failed"
            ) from exc
        parsed = replica_drain_continuation_from_request(request)
        if not isinstance(continuation, ReplicaDrainContinuation):
            raise TypeError("replica drain continuation is invalid")
        if parsed != continuation:
            raise ReplicaJournalV2ConflictError(
                "replica drain payload differs from supplied continuation"
            )
        identity = self._identity(request)
        database = self._open()
        try:
            events = self._rows_for(database, identity)
            if not events:
                raise ReplicaJournalV2ConflictError("replica paused lifecycle is missing")
            replay = self._replay(events)
            if replay.continuation is not None:
                if (
                    replay.drain_request != request
                    or replay.drain_request_frame != frame
                    or replay.continuation != continuation
                ):
                    raise ReplicaJournalV2ConflictError(
                        "replica drain continuation conflicts with durable state"
                    )
                return self._decision(replay, initial=False)
            if replay.terminal_state is not None:
                raise ReplicaJournalV2ConflictError("replica lifecycle is already terminal")
            admission = replay.receipts.admission
            if (
                not replay.paused
                or replay.owner_token is None
                or replay.owner_broker_incarnation is None
                or request.broker_incarnation != replay.owner_broker_incarnation
                or continuation.authority != replay.authority
                or continuation.initial_request_sha256
                != hashlib.sha256(replay.initial_request_frame).hexdigest()
                or not isinstance(admission, AdmissionReceipt)
                or continuation.admission_receipt_id != admission.receipt_id
            ):
                raise ReplicaJournalV2ConflictError(
                    "replica drain continuation does not match durable pause"
                )
            owner_token = replay.owner_token
        finally:
            database.close()
        values = self._event_values(
            identity,
            event_type="drain_continuation_accepted",
            request_frame=frame,
            authority=continuation.authority,
            continuation=continuation,
            owner_token=owner_token,
        )
        self._append_many(identity, [values])
        return self.drain_status(request)

    def reject_stage(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
        status: str,
        receipt: RejectedReceipt,
        response_frame: bytes,
    ) -> ReplicaJournalPosition:
        self._validate_rejection(stage, status, receipt)
        receipt_json = _rejected_json(receipt)
        response = _validate_response_frame(
            response_frame,
            request,
            public_key=self._response_public_key,
            state="rejected",
            stage=stage,
            code=status,
            receipt_id=receipt.receipt_id,
        )
        identity = self._identity(request)
        existing = self._existing_stage_completion(
            request,
            stage=stage,
            event_type="stage_rejected",
            owner_token=owner_token,
            stage_status=status,
            receipt_json=receipt_json,
            unresolved_reason=None,
            response_frame=response,
        )
        if existing is not None:
            return existing
        try:
            replay = self._require_active_stage(
                request,
                owner_token=owner_token,
                stage=stage,
            )
        except ReplicaJournalV2ConflictError:
            existing = self._existing_stage_completion(
                request,
                stage=stage,
                event_type="stage_rejected",
                owner_token=owner_token,
                stage_status=status,
                receipt_json=receipt_json,
                unresolved_reason=None,
                response_frame=response,
            )
            if existing is not None:
                return existing
            raise
        if not replay.stage_started:
            existing = self._existing_stage_completion(
                request,
                stage=stage,
                event_type="stage_rejected",
                owner_token=owner_token,
                stage_status=status,
                receipt_json=receipt_json,
                unresolved_reason=None,
                response_frame=response,
            )
            if existing is not None:
                return existing
            raise ReplicaJournalV2ConflictError("replica stage start is missing")
        values = self._event_values(
            identity,
            event_type="stage_rejected",
            stage=stage,
            owner_token=owner_token,
            stage_status=status,
            receipt_json=receipt_json,
            response_frame=response,
        )
        return self._append_many(identity, [values])[-1]

    def mark_stage_unresolved(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
        reason: str,
        response_frame: bytes,
    ) -> ReplicaJournalPosition:
        self._validate_unresolved_reason(stage, reason)
        identity = self._identity(request)
        response = _validate_response_frame(
            response_frame,
            request,
            public_key=self._response_public_key,
            state="unresolved",
            stage=stage,
            code=reason,
            receipt_id=None,
        )
        existing = self._existing_stage_completion(
            request,
            stage=stage,
            event_type="stage_unresolved",
            owner_token=owner_token,
            stage_status=None,
            receipt_json=None,
            unresolved_reason=reason,
            response_frame=response,
        )
        if existing is not None:
            return existing
        database = self._open()
        try:
            events = self._rows_for(database, identity)
            if not events:
                raise ReplicaJournalV2ConflictError("replica acceptance is missing")
            replay = self._replay(events)
            direct_foreign_pause = (
                stage == "drain"
                and reason == "broker_restart_unknown"
                and replay.paused
                and not replay.stage_started
                and (
                    (
                        replay.continuation is None
                        and request.request_type == "replica.lifecycle"
                        and replay.initial_request == request
                    )
                    or (
                        replay.continuation is not None
                        and request.request_type == "replica.drain"
                        and replay.drain_request == request
                    )
                )
            )
        finally:
            database.close()
        if not direct_foreign_pause:
            try:
                replay = self._require_active_stage(
                    request,
                    owner_token=owner_token,
                    stage=stage,
                )
            except ReplicaJournalV2ConflictError:
                existing = self._existing_stage_completion(
                    request,
                    stage=stage,
                    event_type="stage_unresolved",
                    owner_token=owner_token,
                    stage_status=None,
                    receipt_json=None,
                    unresolved_reason=reason,
                    response_frame=response,
                )
                if existing is not None:
                    return existing
                raise
            if not replay.stage_started:
                existing = self._existing_stage_completion(
                    request,
                    stage=stage,
                    event_type="stage_unresolved",
                    owner_token=owner_token,
                    stage_status=None,
                    receipt_json=None,
                    unresolved_reason=reason,
                    response_frame=response,
                )
                if existing is not None:
                    return existing
                raise ReplicaJournalV2ConflictError("replica stage start is missing")
        elif replay.owner_token != owner_token:
            raise ReplicaJournalV2ConflictError("replica lifecycle owner conflicts")
        values = self._event_values(
            identity,
            event_type="stage_unresolved",
            stage=stage,
            owner_token=owner_token,
            unresolved_reason=reason,
            response_frame=response,
        )
        return self._append_many(identity, [values])[-1]

    def _existing_stage_completion(
        self,
        request: BrokerRequest,
        *,
        stage: str,
        event_type: str,
        owner_token: str,
        stage_status: str | None,
        receipt_json: bytes | None,
        unresolved_reason: str | None,
        response_frame: bytes | None,
    ) -> ReplicaJournalPosition | None:
        identity = self._identity(request)
        database = self._open()
        try:
            self._load_replay(database, request)
            events = self._rows_for(database, identity)
            completions = [
                event
                for event in events
                if event.stage == stage
                and event.event_type in {"stage_outcome", "stage_rejected", "stage_unresolved"}
            ]
            if not completions:
                return None
            event = completions[0]
            if (
                event.event_type != event_type
                or event.owner_token != owner_token
                or event.stage_status != stage_status
                or event.receipt_json != receipt_json
                or event.unresolved_reason != unresolved_reason
                or event.response_frame != response_frame
            ):
                raise ReplicaJournalV2ConflictError(
                    "replica stage completion conflicts with durable state"
                )
            return self._position(database, event)
        finally:
            database.close()

    def active_lifecycle(self, request: BrokerRequest) -> ActiveReplicaLifecycleV2 | None:
        database = self._open()
        try:
            replay = self._load_replay(database, request)
            if replay is None or not replay.paused:
                return None
            admission = replay.receipts.admission
            if (
                replay.owner_token is None
                or replay.owner_broker_incarnation is None
                or not isinstance(admission, AdmissionReceipt)
                or replay.active_response_frame is None
            ):
                raise ReplicaJournalV2SyncError("replica active lifecycle is incomplete")
            return ActiveReplicaLifecycleV2(
                request_frame=replay.initial_request_frame,
                authority=replay.authority,
                owner_token=replay.owner_token,
                owner_broker_incarnation=replay.owner_broker_incarnation,
                admission_receipt=admission,
                response_frame=replay.active_response_frame,
                drain_request_frame=replay.drain_request_frame,
            )
        finally:
            database.close()

    def completed_receipts(self, request: BrokerRequest) -> CompletedReplicaReceipts:
        database = self._open()
        try:
            replay = self._load_replay(database, request)
            if replay is None:
                raise ReplicaJournalV2ConflictError("replica acceptance is missing")
            return replay.receipts
        finally:
            database.close()

    def incomplete_lifecycles(self) -> tuple[IncompleteReplicaLifecycleV2, ...]:
        database = self._open()
        try:
            identities = database.execute(
                "SELECT database_incarnation, operation_id, MIN(event_id) "
                "FROM replica_journal_v2_events GROUP BY 1, 2 ORDER BY MIN(event_id)"
            ).fetchall()
            result: list[IncompleteReplicaLifecycleV2] = []
            for database_incarnation, operation_id, _first in identities:
                replay = self._replay(
                    self._rows_for(
                        database,
                        (str(database_incarnation), str(operation_id)),
                    )
                )
                if replay.owner_token is None or replay.terminal_state is not None:
                    continue
                if replay.next_stage_index >= len(_STAGES):
                    raise ReplicaJournalV2SyncError(
                        "replica incomplete lifecycle has no next stage"
                    )
                result.append(
                    IncompleteReplicaLifecycleV2(
                        request_frame=replay.initial_request_frame,
                        drain_request_frame=replay.drain_request_frame,
                        owner_token=replay.owner_token,
                        owner_broker_incarnation=cast(str, replay.owner_broker_incarnation),
                        stage=_STAGES[replay.next_stage_index],
                        stage_started=replay.stage_started,
                        paused=replay.paused,
                        reason="broker_restart_unknown",
                        authority=replay.authority,
                    )
                )
            return tuple(result)
        finally:
            database.close()

    def authenticated_initial_request(
        self, lifecycle: IncompleteReplicaLifecycleV2 | ActiveReplicaLifecycleV2
    ) -> BrokerRequest:
        if not isinstance(lifecycle, (IncompleteReplicaLifecycleV2, ActiveReplicaLifecycleV2)):
            raise TypeError("replica lifecycle is invalid")
        try:
            request = _request_from_frame(lifecycle.request_frame, self._request_public_key)
        except ValueError as exc:
            raise ReplicaJournalV2SyncError(
                "replica initial request authentication failed"
            ) from exc
        if request.request_type != "replica.lifecycle":
            raise ReplicaJournalV2SyncError("replica initial request type is invalid")
        return request

    def authenticated_drain_request(
        self, lifecycle: IncompleteReplicaLifecycleV2 | ActiveReplicaLifecycleV2
    ) -> BrokerRequest:
        if not isinstance(lifecycle, (IncompleteReplicaLifecycleV2, ActiveReplicaLifecycleV2)):
            raise TypeError("replica lifecycle is invalid")
        if lifecycle.drain_request_frame is None:
            raise ReplicaJournalV2SyncError("replica drain continuation is missing")
        try:
            request = _request_from_frame(lifecycle.drain_request_frame, self._request_public_key)
        except ValueError as exc:
            raise ReplicaJournalV2SyncError("replica drain request authentication failed") from exc
        replica_drain_continuation_from_request(request)
        return request

    def _event_values(
        self,
        identity: tuple[str, str],
        *,
        event_type: str,
        stage: str | None = None,
        request_frame: bytes | None = None,
        authority: ReplicaAuthority | None = None,
        continuation: ReplicaDrainContinuation | None = None,
        owner_token: str | None = None,
        owner_broker_incarnation: str | None = None,
        stage_status: str | None = None,
        receipt_json: bytes | None = None,
        unresolved_reason: str | None = None,
        response_frame: bytes | None = None,
    ) -> tuple[object, ...]:
        request = (
            None
            if request_frame is None
            else _request_from_frame(request_frame, self._request_public_key)
        )
        if request is not None and authority is None:
            raise ValueError("replica request authority is missing")
        if request is None and authority is not None:
            raise ValueError("replica authority without request is invalid")
        return (
            secrets.token_hex(16),
            identity[0],
            identity[1],
            event_type,
            stage,
            request_frame,
            None if request_frame is None else hashlib.sha256(request_frame).hexdigest(),
            None if request is None else request.broker_incarnation,
            None if request is None else request.request_type,
            None if request is None else request.nonce,
            None if request is None else request.connection_sequence,
            None if request is None else request.payload_digest,
            None if authority is None else authority.physical_target_id,
            None if authority is None else authority.replica_generation,
            None if authority is None else authority.execution_owner_id,
            None if continuation is None else continuation.initial_request_sha256,
            None if continuation is None else continuation.admission_receipt_id,
            None if continuation is None else continuation.application_drain_intent_receipt_id,
            owner_token,
            owner_broker_incarnation,
            stage_status,
            receipt_json,
            unresolved_reason,
            response_frame,
            None if response_frame is None else hashlib.sha256(response_frame).hexdigest(),
        )

    @staticmethod
    def _insert_sql() -> str:
        return """
            INSERT INTO replica_journal_v2_events(
                append_receipt_id, database_incarnation, operation_id,
                event_type, stage, request_frame, request_frame_sha256,
                request_broker_incarnation, request_type, request_nonce,
                request_connection_sequence, request_payload_digest,
                physical_target_id, replica_generation, execution_owner_id,
                initial_request_sha256, admission_receipt_id,
                application_drain_intent_receipt_id, owner_token,
                owner_broker_incarnation, stage_status, receipt_json,
                unresolved_reason, response_frame, response_frame_sha256
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )
        """

    def _commit_transaction(self, database: sqlite3.Connection) -> None:
        """Commit through an overridable uncertain-commit fault boundary."""
        database.commit()

    def _append_many(
        self,
        identity: tuple[str, str],
        values: list[tuple[object, ...]],
    ) -> tuple[ReplicaJournalPosition, ...]:
        if not values:
            raise ValueError("replica journal V2 append batch is empty")
        database = self._open()
        event_ids: list[int] = []
        commit_uncertain = False
        collision = False
        try:
            database.execute("BEGIN IMMEDIATE")
            for item in values:
                cursor = database.execute(self._insert_sql(), item)
                event_ids.append(cast(int, cursor.lastrowid))
            try:
                self._commit_transaction(database)
            except (OSError, sqlite3.Error):
                commit_uncertain = True
        except sqlite3.IntegrityError:
            collision = True
        except (OSError, sqlite3.Error) as exc:
            raise ReplicaJournalV2SyncError("replica journal V2 append failed") from exc
        finally:
            database.close()
        if commit_uncertain or collision:
            return self._confirm_appended_events(
                identity=identity,
                intended=values,
                collision=collision,
            )
        if len(event_ids) != len(values):
            raise ReplicaJournalV2SyncError("replica journal V2 append positions are missing")
        database = self._open()
        try:
            journal_id = self._journal_id(database)
            return tuple(
                ReplicaJournalPosition(journal_id, event_id, cast(str, item[0]))
                for event_id, item in zip(event_ids, values, strict=True)
            )
        finally:
            database.close()

    @staticmethod
    def _winner_clause(values: tuple[object, ...]) -> tuple[str, tuple[object, ...]]:
        event_type = cast(str, values[3])
        stage = cast(str | None, values[4])
        identity = (values[1], values[2])
        if event_type in {
            "accepted",
            "authority_claimed",
            "lifecycle_paused",
            "drain_continuation_accepted",
        }:
            return "event_type = ?", (*identity, event_type)
        if event_type == "stage_started":
            return "event_type = ? AND stage = ?", (*identity, event_type, stage)
        return (
            ("event_type IN ('stage_outcome', 'stage_rejected', 'stage_unresolved') AND stage = ?"),
            (*identity, stage),
        )

    def _inspect_intended_event(
        self,
        identity: tuple[str, str],
        values: tuple[object, ...],
    ) -> tuple[str, int | None, str | None]:
        database = self._open()
        try:
            selected = self._selected_columns().replace(", created_at", "")
            row = database.execute(
                f"SELECT {selected} FROM replica_journal_v2_events WHERE append_receipt_id = ?",
                (values[0],),
            ).fetchone()
            if row is not None:
                if tuple(row[1:]) == values:
                    return "present", cast(int, row[0]), cast(str, row[1])
                return "conflict", None, None
            clause, parameters = self._winner_clause(values)
            winner = database.execute(
                f"SELECT {selected} FROM replica_journal_v2_events "
                "WHERE database_incarnation = ? AND operation_id = ? "
                f"AND {clause} LIMIT 1",
                parameters,
            ).fetchone()
            if winner is None:
                return "absent", None, None
            if tuple(winner[2:]) == values[1:]:
                return "equivalent", cast(int, winner[0]), cast(str, winner[1])
            return "conflict", None, None
        except (OSError, sqlite3.Error, TypeError) as exc:
            raise ReplicaJournalV2SyncError(
                "replica journal V2 commit verification failed"
            ) from exc
        finally:
            database.close()

    def _confirm_appended_events(
        self,
        *,
        identity: tuple[str, str],
        intended: list[tuple[object, ...]],
        collision: bool,
    ) -> tuple[ReplicaJournalPosition, ...]:
        verdicts = [self._inspect_intended_event(identity, values) for values in intended]
        if all(
            verdict in {"present", "equivalent"}
            and event_id is not None
            and append_receipt_id is not None
            for verdict, event_id, append_receipt_id in verdicts
        ):
            database = self._open()
            try:
                journal_id = self._journal_id(database)
                return tuple(
                    ReplicaJournalPosition(
                        journal_id,
                        cast(int, event_id),
                        cast(str, append_receipt_id),
                    )
                    for _verdict, event_id, append_receipt_id in verdicts
                )
            finally:
                database.close()
        if all(verdict == "absent" for verdict, _event_id, _receipt in verdicts):
            if collision:
                raise ReplicaJournalV2ConflictError(
                    "replica journal V2 transition conflicts with durable state"
                )
            raise ReplicaJournalV2CommitAbsent(
                "replica journal V2 commit is absent after fresh verification"
            )
        if collision:
            raise ReplicaJournalV2ConflictError(
                "replica journal V2 transition conflicts with durable state"
            )
        raise ReplicaJournalV2SyncError(
            "replica journal V2 atomic commit conflicts with persisted state"
        )
