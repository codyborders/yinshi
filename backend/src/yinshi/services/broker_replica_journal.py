"""Broker-owned append-only authority for one replica lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from collections.abc import Mapping
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
    BROKER_PROTOCOL_VERSION,
    BROKER_RESPONSE_BYTES_MAX,
    BrokerProtocolError,
    BrokerRequest,
    JsonValue,
    canonical_json,
    parse_signed_request,
    verify_broker_response,
)
from yinshi.services.replica_artifact_contract import (
    REPLICA_ARTIFACT_MEDIA_TYPES,
    compute_replica_artifact_set_sha256,
    validate_distinct_artifact_ids,
    validate_replica_identifier,
    validate_replica_operation_id,
)

REPLICA_JOURNAL_SCHEMA_VERSION = 1
ARTIFACT_BYTES_MAX = 8 * 1024 * 1024 * 1024
RECEIPT_JSON_BYTES_MAX = 2_048
_APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_APPEND_RECEIPT_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_JOURNAL_ID_PATTERN = re.compile(r"^[0-9a-f]{32}_[0-9a-f]{64}$")
_OBJECT_FORMATS = ("sha1", "sha256")
_STAGES = ("ingest", "verify", "publish", "admission", "drain", "export", "reclaim")
_STAGE_INDEX = {stage: index for index, stage in enumerate(_STAGES)}
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


class ReplicaJournalError(RuntimeError):
    """Base class for replica journal failures."""


class ReplicaJournalSyncError(ReplicaJournalError):
    """Report malformed, conflicting, or uncertain durable state."""


class ReplicaJournalCommitAbsent(ReplicaJournalSyncError):
    """Report a commit that a fresh connection confirms is absent."""


class ReplicaJournalConflictError(ReplicaJournalSyncError):
    """Reject a call that conflicts with an immutable event."""


class ReplicaJournalEffectResultError(ValueError):
    """Reject an effect result that cannot satisfy a journal transition."""


def _token(value: object, description: str) -> str:
    if not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(f"replica {description} is invalid")
    return value


def _digest(value: object, description: str) -> str:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"replica {description} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ReplicaAuthority:
    """Application-owned target binding accepted before broker effects."""

    physical_target_id: str
    replica_generation: int
    execution_owner_id: str

    def __post_init__(self) -> None:
        validate_replica_identifier(self.physical_target_id, "physical target ID")
        validate_replica_identifier(self.execution_owner_id, "execution owner ID")
        if type(self.replica_generation) is not int or self.replica_generation < 1:
            raise ValueError("replica generation must be a positive integer")


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """Bounded identity for artifact bytes stored outside SQLite."""

    artifact_id: str
    sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        validate_replica_identifier(self.artifact_id, "artifact ID")
        _digest(self.sha256, "artifact SHA-256")
        if (
            type(self.byte_length) is not int
            or self.byte_length < 0
            or self.byte_length > ARTIFACT_BYTES_MAX
        ):
            raise ValueError("replica artifact byte length is invalid")


@dataclass(frozen=True, slots=True)
class IngestReceipt:
    receipt_id: str
    bundle: ArtifactReference
    worktree: ArtifactReference
    index_objects: ArtifactReference

    def __post_init__(self) -> None:
        _token(self.receipt_id, "ingest receipt ID")
        if not isinstance(self.bundle, ArtifactReference):
            raise TypeError("replica bundle reference is invalid")
        if not isinstance(self.worktree, ArtifactReference):
            raise TypeError("replica worktree reference is invalid")
        if not isinstance(self.index_objects, ArtifactReference):
            raise TypeError("replica index objects reference is invalid")
        validate_distinct_artifact_ids(
            (
                self.bundle.artifact_id,
                self.worktree.artifact_id,
                self.index_objects.artifact_id,
            )
        )


@dataclass(frozen=True, slots=True)
class VerifyReceipt:
    receipt_id: str
    bundle_sha256: str
    worktree_sha256: str
    index_objects_sha256: str
    source_state_sha256: str
    reconciliation_fingerprint: str
    artifact_set_sha256: str

    def __post_init__(self) -> None:
        _token(self.receipt_id, "verification receipt ID")
        _digest(self.bundle_sha256, "verified bundle SHA-256")
        _digest(self.worktree_sha256, "verified worktree SHA-256")
        _digest(self.index_objects_sha256, "verified index objects SHA-256")
        _digest(self.source_state_sha256, "verified source state SHA-256")
        _digest(
            self.reconciliation_fingerprint,
            "verified reconciliation fingerprint",
        )
        _digest(self.artifact_set_sha256, "verified artifact set SHA-256")


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    receipt_id: str
    physical_target_id: str
    replica_generation: int
    execution_owner_id: str
    publication_marker_sha256: str
    synchronization_receipt_id: str
    artifact_set_sha256: str

    def __post_init__(self) -> None:
        _token(self.receipt_id, "publication receipt ID")
        validate_replica_identifier(self.physical_target_id, "published physical target ID")
        if type(self.replica_generation) is not int or self.replica_generation < 1:
            raise ValueError("published replica generation is invalid")
        validate_replica_identifier(self.execution_owner_id, "published execution owner ID")
        _digest(self.publication_marker_sha256, "publication marker SHA-256")
        _token(self.synchronization_receipt_id, "synchronization receipt ID")
        _digest(self.artifact_set_sha256, "published artifact set SHA-256")


@dataclass(frozen=True, slots=True)
class AdmissionReceipt:
    receipt_id: str
    execution_owner_id: str
    runtime_unit_id: str
    session_socket_sha256: str

    def __post_init__(self) -> None:
        _token(self.receipt_id, "admission receipt ID")
        validate_replica_identifier(self.execution_owner_id, "admitted execution owner ID")
        _token(self.runtime_unit_id, "runtime unit ID")
        _digest(self.session_socket_sha256, "session socket SHA-256")


@dataclass(frozen=True, slots=True)
class DrainReceipt:
    drain_ack_receipt_id: str
    quiescence_receipt_id: str

    def __post_init__(self) -> None:
        _token(self.drain_ack_receipt_id, "drain acknowledgment receipt ID")
        _token(self.quiescence_receipt_id, "quiescence receipt ID")


@dataclass(frozen=True, slots=True)
class ExportReceipt:
    export_receipt_id: str
    bundle: ArtifactReference
    worktree: ArtifactReference
    index_objects: ArtifactReference

    def __post_init__(self) -> None:
        _token(self.export_receipt_id, "export receipt ID")
        if not isinstance(self.bundle, ArtifactReference):
            raise TypeError("replica export bundle reference is invalid")
        if not isinstance(self.worktree, ArtifactReference):
            raise TypeError("replica export worktree reference is invalid")
        if not isinstance(self.index_objects, ArtifactReference):
            raise TypeError("replica export index objects reference is invalid")
        validate_distinct_artifact_ids(
            (
                self.bundle.artifact_id,
                self.worktree.artifact_id,
                self.index_objects.artifact_id,
            )
        )


@dataclass(frozen=True, slots=True)
class ReclaimReceipt:
    reclaim_receipt_id: str

    def __post_init__(self) -> None:
        _token(self.reclaim_receipt_id, "reclaim receipt ID")


@dataclass(frozen=True, slots=True)
class RejectedReceipt:
    receipt_id: str

    def __post_init__(self) -> None:
        _token(self.receipt_id, "rejection receipt ID")


@dataclass(frozen=True, slots=True)
class ReplicaJournalPosition:
    journal_id: str
    sequence: int
    append_receipt_id: str


@dataclass(frozen=True, slots=True)
class ReplicaJournalDecision:
    state: str
    stage: str | None
    response_frame: bytes | None
    owner_broker_incarnation: str | None


@dataclass(frozen=True, slots=True)
class IncompleteReplicaLifecycle:
    request_frame: bytes
    owner_token: str
    owner_broker_incarnation: str
    stage: str
    stage_started: bool
    reason: str
    authority: ReplicaAuthority


@dataclass(frozen=True, slots=True)
class _Replay:
    request_frame: bytes
    authority: ReplicaAuthority
    owner_token: str | None
    owner_broker_incarnation: str | None
    next_stage_index: int
    stage_started: bool
    terminal_state: str | None
    terminal_stage: str | None
    response_frame: bytes | None


@dataclass(frozen=True, slots=True)
class _Event:
    event_id: int
    append_receipt_id: str
    database_incarnation: str
    operation_id: str
    request_type: str
    event_type: str
    stage: str | None
    request_frame: bytes | None
    request_frame_sha256: str | None
    request_broker_incarnation: str | None
    request_nonce: str | None
    request_connection_sequence: int | None
    request_payload_digest: str | None
    physical_target_id: str | None
    replica_generation: int | None
    execution_owner_id: str | None
    owner_token: str | None
    owner_broker_incarnation: str | None
    stage_status: str | None
    receipt_json: bytes | None
    unresolved_reason: str | None
    response_frame: bytes | None
    response_frame_sha256: str | None
    created_at: str


REPLICA_JOURNAL_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE replica_journal_meta (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 0),
        application_id TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        journal_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE replica_journal_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        append_receipt_id TEXT NOT NULL UNIQUE,
        database_incarnation TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        request_type TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (
            event_type IN (
                'accepted', 'authority_claimed', 'stage_started',
                'stage_outcome', 'stage_rejected', 'stage_unresolved'
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
        request_nonce TEXT,
        request_connection_sequence INTEGER,
        request_payload_digest TEXT,
        physical_target_id TEXT,
        replica_generation INTEGER,
        execution_owner_id TEXT,
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
    CREATE UNIQUE INDEX replica_journal_one_acceptance
    ON replica_journal_events(database_incarnation, operation_id, request_type)
    WHERE event_type = 'accepted'
    """,
    """
    CREATE UNIQUE INDEX replica_journal_one_claim
    ON replica_journal_events(database_incarnation, operation_id, request_type)
    WHERE event_type = 'authority_claimed'
    """,
    """
    CREATE UNIQUE INDEX replica_journal_one_stage_start
    ON replica_journal_events(database_incarnation, operation_id, request_type, stage)
    WHERE event_type = 'stage_started'
    """,
    """
    CREATE UNIQUE INDEX replica_journal_one_stage_completion
    ON replica_journal_events(database_incarnation, operation_id, request_type, stage)
    WHERE event_type IN ('stage_outcome', 'stage_rejected', 'stage_unresolved')
    """,
    """
    CREATE UNIQUE INDEX replica_journal_one_terminal
    ON replica_journal_events(database_incarnation, operation_id, request_type)
    WHERE event_type IN ('stage_rejected', 'stage_unresolved')
       OR (event_type = 'stage_outcome' AND stage = 'reclaim')
    """,
    """
    CREATE TRIGGER replica_journal_reject_update
    BEFORE UPDATE ON replica_journal_events
    BEGIN
        SELECT RAISE(ABORT, 'replica journal events are immutable');
    END
    """,
    """
    CREATE TRIGGER replica_journal_reject_delete
    BEFORE DELETE ON replica_journal_events
    BEGIN
        SELECT RAISE(ABORT, 'replica journal events are immutable');
    END
    """,
    """
    CREATE TRIGGER replica_journal_require_transition
    BEFORE INSERT ON replica_journal_events
    WHEN NEW.event_type != 'accepted'
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM replica_journal_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id
              AND request_type = NEW.request_type
              AND event_type = 'accepted'
        ) THEN RAISE(ABORT, 'replica journal acceptance is missing') END;
        SELECT CASE WHEN NEW.event_type != 'authority_claimed' AND NOT EXISTS (
            SELECT 1 FROM replica_journal_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id
              AND request_type = NEW.request_type
              AND event_type = 'authority_claimed'
              AND owner_token = NEW.owner_token
        ) THEN RAISE(ABORT, 'replica journal authority is missing') END;
        SELECT CASE WHEN NEW.event_type IN (
            'stage_outcome', 'stage_rejected', 'stage_unresolved'
        ) AND NOT EXISTS (
            SELECT 1 FROM replica_journal_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id
              AND request_type = NEW.request_type
              AND event_type = 'stage_started'
              AND stage = NEW.stage
              AND owner_token = NEW.owner_token
        ) THEN RAISE(ABORT, 'replica journal stage start is missing') END;
        SELECT CASE WHEN EXISTS (
            SELECT 1 FROM replica_journal_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id
              AND request_type = NEW.request_type
              AND (event_type IN ('stage_rejected', 'stage_unresolved')
                OR (event_type = 'stage_outcome' AND stage = 'reclaim'))
        ) THEN RAISE(ABORT, 'replica journal terminal state is immutable') END;
    END
    """,
    """
    CREATE TRIGGER replica_journal_meta_reject_update
    BEFORE UPDATE ON replica_journal_meta
    BEGIN
        SELECT RAISE(ABORT, 'replica journal metadata is immutable');
    END
    """,
    """
    CREATE TRIGGER replica_journal_meta_reject_delete
    BEFORE DELETE ON replica_journal_meta
    BEGIN
        SELECT RAISE(ABORT, 'replica journal metadata is immutable');
    END
    """,
)


def _normalized_sql(value: str) -> str:
    return " ".join(value.split()).rstrip(";")


@lru_cache(maxsize=1)
def _expected_schema() -> dict[tuple[str, str], str]:
    database = sqlite3.connect(":memory:")
    try:
        for statement in REPLICA_JOURNAL_SCHEMA_STATEMENTS:
            database.execute(statement)
        rows = database.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {(str(kind), str(name)): _normalized_sql(str(sql)) for kind, name, sql in rows}
    finally:
        database.close()


def _artifact_json(reference: ArtifactReference) -> dict[str, JsonValue]:
    return {
        "artifact_id": reference.artifact_id,
        "byte_length": reference.byte_length,
        "sha256": reference.sha256,
    }


def _artifact_binding_json(
    role: str,
    reference: ArtifactReference,
) -> dict[str, object]:
    return {
        "role": role,
        "media_type": REPLICA_ARTIFACT_MEDIA_TYPES[role],
        "artifact_id": reference.artifact_id,
        "sha256": reference.sha256,
        "byte_length": reference.byte_length,
    }


def parse_replica_authority(value: object) -> ReplicaAuthority:
    required = {"execution_owner_id", "physical_target_id", "replica_generation"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("replica request authority fields are invalid")
    return ReplicaAuthority(
        physical_target_id=cast(str, value["physical_target_id"]),
        replica_generation=cast(int, value["replica_generation"]),
        execution_owner_id=cast(str, value["execution_owner_id"]),
    )


def replica_authority_from_request(request: BrokerRequest) -> ReplicaAuthority:
    """Parse the logical authority bound into one authenticated lifecycle request."""
    if not isinstance(request, BrokerRequest) or request.request_type != "replica.lifecycle":
        raise ValueError("replica lifecycle request is invalid")
    payload = _validate_replica_payload(request.payload)
    return parse_replica_authority(payload["authority"])


def _expected_artifact_set_sha256(
    request: BrokerRequest,
    authority: ReplicaAuthority,
) -> str:
    payload = _validate_replica_payload(request.payload)
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
        bundle=_artifact_binding_json(
            "committed_bundle",
            _parse_artifact(payload["bundle"]),
        ),
        worktree=_artifact_binding_json(
            "worktree",
            _parse_artifact(payload["worktree"]),
        ),
        index_objects=_artifact_binding_json(
            "index_objects",
            _parse_artifact(payload["index_objects"]),
        ),
        limits_sha256=cast(str, payload["limits_sha256"]),
    )


def _receipt_json(stage: str, receipt: object) -> bytes:
    value: dict[str, JsonValue]
    if stage == "ingest" and isinstance(receipt, IngestReceipt):
        value = {
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
        raise TypeError(f"replica {stage} receipt type is invalid")
    encoded = canonical_json(value)
    if len(encoded) > RECEIPT_JSON_BYTES_MAX:
        raise ValueError("replica receipt exceeds the byte limit")
    return encoded


def _rejected_json(receipt: RejectedReceipt) -> bytes:
    encoded = canonical_json({"receipt_id": receipt.receipt_id})
    if len(encoded) > RECEIPT_JSON_BYTES_MAX:
        raise ValueError("replica rejection receipt exceeds the byte limit")
    return encoded


def _canonical_object(raw: object, *, maximum: int, description: str) -> dict[str, object]:
    if not isinstance(raw, (bytes, bytearray)) or not raw or len(raw) > maximum:
        raise ValueError(f"replica {description} size is invalid")
    try:
        decoded = bytes(raw).decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"replica {description} is invalid") from exc
    if not isinstance(value, dict):
        raise TypeError(f"replica {description} must be an object")
    try:
        if canonical_json(value) != bytes(raw):
            raise ValueError(f"replica {description} is not canonical")
    except BrokerProtocolError as exc:
        raise ValueError(f"replica {description} is invalid") from exc
    return cast(dict[str, object], value)


def _validate_replica_payload(value: object) -> dict[str, JsonValue]:
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
    _digest(value["artifact_set_sha256"], "request artifact set SHA-256")
    _digest(value["limits_sha256"], "request limits SHA-256")
    _digest(value["source_state_sha256"], "request source state SHA-256")
    _digest(
        value["reconciliation_fingerprint"],
        "request reconciliation fingerprint",
    )
    references = (
        _parse_artifact(value["bundle"]),
        _parse_artifact(value["worktree"]),
        _parse_artifact(value["index_objects"]),
    )
    validate_distinct_artifact_ids(tuple(reference.artifact_id for reference in references))
    return cast(dict[str, JsonValue], value)


def _request_from_frame(frame: object) -> BrokerRequest:
    value = _canonical_object(frame, maximum=BROKER_FRAME_BYTES_MAX, description="request frame")
    required = {
        "broker_incarnation",
        "connection_sequence",
        "database_incarnation",
        "nonce",
        "operation_id",
        "payload",
        "payload_digest",
        "protocol_version",
        "request_type",
        "signature",
    }
    if set(value) != required:
        raise ValueError("replica request frame fields are invalid")
    broker_incarnation = _token(value["broker_incarnation"], "request broker incarnation")
    database_incarnation = _token(value["database_incarnation"], "request database incarnation")
    nonce = _token(value["nonce"], "request nonce")
    operation_id = validate_replica_operation_id(value["operation_id"])
    request_type = value["request_type"]
    if request_type != "replica.lifecycle":
        raise ValueError("replica request type is invalid")
    sequence = value["connection_sequence"]
    if type(sequence) is not int or not 1 <= sequence <= 2**63 - 1:
        raise ValueError("replica request connection sequence is invalid")
    protocol_version = value["protocol_version"]
    if protocol_version != BROKER_PROTOCOL_VERSION:
        raise ValueError("replica request protocol version is invalid")
    signature = value["signature"]
    if not isinstance(signature, str) or len(signature) != 86:
        raise ValueError("replica request signature encoding is invalid")
    payload = _validate_replica_payload(value["payload"])
    payload_sha256 = hashlib.sha256(canonical_json(payload)).hexdigest()
    if value["payload_digest"] != payload_sha256:
        raise ValueError("replica request payload digest is invalid")
    return BrokerRequest(
        protocol_version=protocol_version,
        broker_incarnation=broker_incarnation,
        database_incarnation=database_incarnation,
        connection_sequence=sequence,
        operation_id=operation_id,
        request_type=request_type,
        nonce=nonce,
        payload_digest=payload_sha256,
        payload=payload,
    )


def _authenticate_request_frame(
    frame: object,
    public_key: Ed25519PublicKey,
) -> BrokerRequest:
    if not isinstance(frame, (bytes, bytearray)):
        raise TypeError("replica request frame is invalid")
    raw = bytes(frame)
    try:
        request = parse_signed_request(raw, public_key=public_key)
    except BrokerProtocolError as error:
        raise ValueError("replica request signature is invalid") from error
    if request.request_type != "replica.lifecycle":
        raise ValueError("replica request type is invalid")
    return request


def _validate_request_frame(frame: object, request: BrokerRequest | None = None) -> bytes:
    stored_request = _request_from_frame(frame)
    if request is not None and stored_request != request:
        raise ValueError("replica request frame does not bind the request")
    return bytes(cast(bytes | bytearray, frame))


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
    if not isinstance(frame, (bytes, bytearray)):
        raise TypeError("replica response frame is invalid")
    raw = bytes(frame)
    try:
        verify_broker_response(
            raw,
            public_key=public_key,
            expected_request=request,
        )
    except BrokerProtocolError as error:
        raise ValueError("replica response signature is invalid") from error
    value = _canonical_object(raw, maximum=BROKER_RESPONSE_BYTES_MAX, description="response frame")
    required = {
        "broker_incarnation",
        "connection_sequence",
        "database_incarnation",
        "error",
        "operation_id",
        "payload_digest",
        "protocol_version",
        "request_nonce",
        "request_type",
        "result",
        "signature",
        "status",
    }
    if set(value) != required:
        raise ValueError("replica response frame fields are invalid")
    expected: dict[str, object] = {
        "broker_incarnation": request.broker_incarnation,
        "connection_sequence": request.connection_sequence,
        "database_incarnation": request.database_incarnation,
        "operation_id": request.operation_id,
        "payload_digest": request.payload_digest,
        "protocol_version": request.protocol_version,
        "request_nonce": request.nonce,
        "request_type": request.request_type,
    }
    if any(value.get(name) != item for name, item in expected.items()):
        raise ValueError("replica response frame does not bind the request")
    if state not in {"completed", "rejected", "unresolved"} or stage not in _STAGE_INDEX:
        raise ValueError("replica terminal response disposition is invalid")
    expected_status = "ok" if state == "completed" else "error"
    if value["status"] != expected_status or value["error"] != code:
        raise ValueError("replica terminal response status conflicts with durable state")
    expected_result: dict[str, JsonValue] = {"stage": stage, "state": state}
    if code is not None:
        if not isinstance(code, str) or _ERROR_CODE_PATTERN.fullmatch(code) is None:
            raise ValueError("replica terminal response code is invalid")
        expected_result["code"] = code
    if receipt_id is not None:
        _token(receipt_id, "terminal response receipt ID")
        expected_result["receipt_id"] = receipt_id
    if value["result"] != expected_result:
        raise ValueError("replica terminal response result conflicts with durable state")
    signature = value["signature"]
    if not isinstance(signature, str) or len(signature) != 86:
        raise ValueError("replica response signature encoding is invalid")
    return bytes(frame)


def _parse_artifact(value: object) -> ArtifactReference:
    if not isinstance(value, dict) or set(value) != {"artifact_id", "sha256", "byte_length"}:
        raise ValueError("replica artifact receipt is malformed")
    return ArtifactReference(
        artifact_id=cast(str, value["artifact_id"]),
        sha256=cast(str, value["sha256"]),
        byte_length=cast(int, value["byte_length"]),
    )


def _validate_receipt(stage: str, raw: object) -> object:
    value = _canonical_object(raw, maximum=RECEIPT_JSON_BYTES_MAX, description=f"{stage} receipt")
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
    raise ValueError(f"replica {stage} receipt is malformed")


def _validate_receipt_binding(
    stage: str,
    receipt: object,
    request: BrokerRequest,
    authority: ReplicaAuthority,
) -> None:
    payload = request.payload
    expected_bundle = _parse_artifact(payload["bundle"])
    expected_worktree = _parse_artifact(payload["worktree"])
    expected_index_objects = _parse_artifact(payload["index_objects"])
    expected_artifact_set = payload["artifact_set_sha256"]
    if stage == "ingest" and isinstance(receipt, IngestReceipt):
        if (
            receipt.bundle != expected_bundle
            or receipt.worktree != expected_worktree
            or receipt.index_objects != expected_index_objects
        ):
            raise ValueError("replica ingest receipt conflicts with accepted artifacts")
        return
    if stage == "verify" and isinstance(receipt, VerifyReceipt):
        if (
            receipt.bundle_sha256 != expected_bundle.sha256
            or receipt.worktree_sha256 != expected_worktree.sha256
            or receipt.index_objects_sha256 != expected_index_objects.sha256
            or receipt.source_state_sha256 != payload["source_state_sha256"]
            or receipt.reconciliation_fingerprint != payload["reconciliation_fingerprint"]
            or receipt.artifact_set_sha256 != expected_artifact_set
        ):
            raise ValueError("replica verification receipt conflicts with accepted state")
        return
    if stage == "publish" and isinstance(receipt, PublishReceipt):
        if (
            receipt.physical_target_id != authority.physical_target_id
            or receipt.replica_generation != authority.replica_generation
            or receipt.execution_owner_id != authority.execution_owner_id
            or receipt.artifact_set_sha256 != expected_artifact_set
        ):
            raise ValueError("replica publication receipt conflicts with authority")
        return
    if (
        stage == "admission"
        and isinstance(receipt, AdmissionReceipt)
        and receipt.execution_owner_id != authority.execution_owner_id
    ):
        raise ValueError("replica admission receipt conflicts with authority")


def _event(row: tuple[object, ...]) -> _Event:
    return _Event(
        event_id=cast(int, row[0]),
        append_receipt_id=cast(str, row[1]),
        database_incarnation=cast(str, row[2]),
        operation_id=cast(str, row[3]),
        request_type=cast(str, row[4]),
        event_type=cast(str, row[5]),
        stage=cast(str | None, row[6]),
        request_frame=None if row[7] is None else bytes(cast(bytes, row[7])),
        request_frame_sha256=cast(str | None, row[8]),
        request_broker_incarnation=cast(str | None, row[9]),
        request_nonce=cast(str | None, row[10]),
        request_connection_sequence=cast(int | None, row[11]),
        request_payload_digest=cast(str | None, row[12]),
        physical_target_id=cast(str | None, row[13]),
        replica_generation=cast(int | None, row[14]),
        execution_owner_id=cast(str | None, row[15]),
        owner_token=cast(str | None, row[16]),
        owner_broker_incarnation=cast(str | None, row[17]),
        stage_status=cast(str | None, row[18]),
        receipt_json=None if row[19] is None else bytes(cast(bytes, row[19])),
        unresolved_reason=cast(str | None, row[20]),
        response_frame=None if row[21] is None else bytes(cast(bytes, row[21])),
        response_frame_sha256=cast(str | None, row[22]),
        created_at=cast(str, row[23]),
    )


class BrokerReplicaJournal:
    """Own replica transitions without retaining SQLite handles between calls."""

    def __init__(
        self,
        path: Path,
        *,
        application_id: str,
        expected_limits_sha256: str,
        request_public_key: Ed25519PublicKey,
        response_public_keys: Mapping[str, Ed25519PublicKey],
    ) -> None:
        if not isinstance(path, Path):
            raise TypeError("replica journal path must be a Path")
        if (
            not isinstance(application_id, str)
            or _APPLICATION_ID_PATTERN.fullmatch(application_id) is None
        ):
            raise ValueError("replica journal application ID is invalid")
        if not isinstance(request_public_key, Ed25519PublicKey):
            raise TypeError("replica request public key is invalid")
        if not isinstance(response_public_keys, Mapping) or not response_public_keys:
            raise ValueError("replica response public keyring is empty")
        validated_response_keys: dict[str, Ed25519PublicKey] = {}
        for incarnation, key in response_public_keys.items():
            _token(incarnation, "response key broker incarnation")
            if not isinstance(key, Ed25519PublicKey):
                raise TypeError("replica response public key is invalid")
            validated_response_keys[incarnation] = key
        self._path = path
        self._application_id = application_id
        self._expected_limits_sha256 = _digest(
            expected_limits_sha256,
            "configured replica limits SHA-256",
        )
        self._request_public_key = request_public_key
        self._response_public_keys = validated_response_keys
        request_key = request_public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        response_keyring = {
            incarnation: key.public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            ).hex()
            for incarnation, key in sorted(validated_response_keys.items())
        }
        self._configuration_sha256 = hashlib.sha256(
            b"yinshi-replica-journal-configuration-v1\0"
            + canonical_json(
                {
                    "limits_sha256": self._expected_limits_sha256,
                    "request_public_key": request_key.hex(),
                    "response_public_keys": response_keyring,
                }
            )
        ).hexdigest()
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _identity(request: BrokerRequest) -> tuple[str, str, str]:
        if not isinstance(request, BrokerRequest):
            raise TypeError("replica request is invalid")
        return request.database_incarnation, request.operation_id, request.request_type

    def _response_public_key(self, request: BrokerRequest) -> Ed25519PublicKey:
        key = self._response_public_keys.get(request.broker_incarnation)
        if key is None:
            raise ReplicaJournalConflictError("replica response signer is not configured")
        return key

    def _connect(self) -> sqlite3.Connection:
        database: sqlite3.Connection | None = None
        try:
            database = sqlite3.connect(self._path, timeout=5.0)
            database.execute("PRAGMA busy_timeout = 5000")
            database.execute("PRAGMA foreign_keys = ON")
            database.execute("PRAGMA journal_mode = DELETE")
            database.execute("PRAGMA synchronous = FULL")
            database.execute("PRAGMA fullfsync = ON")
            return database
        except (OSError, sqlite3.Error) as exc:
            if database is not None:
                database.close()
            raise ReplicaJournalSyncError("replica journal synchronization failed") from exc

    def _initialize(self) -> None:
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._path.parent, 0o700)
            database = self._connect()
            try:
                database.execute("BEGIN EXCLUSIVE")
                existing = database.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
                if existing is None:
                    for statement in REPLICA_JOURNAL_SCHEMA_STATEMENTS:
                        database.execute(statement)
                    database.execute(
                        "INSERT INTO replica_journal_meta VALUES (0, ?, ?, ?)",
                        (
                            self._application_id,
                            REPLICA_JOURNAL_SCHEMA_VERSION,
                            f"{secrets.token_hex(16)}_{self._configuration_sha256}",
                        ),
                    )
                database.commit()
            finally:
                database.close()
            os.chmod(self._path, 0o600)
            parent = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
            verified = self._open()
            verified.close()
        except ReplicaJournalSyncError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise ReplicaJournalSyncError("replica journal synchronization failed") from exc

    def _open(self) -> sqlite3.Connection:
        database = self._connect()
        try:
            self._validate(database)
        except BaseException:
            database.close()
            raise
        return database

    def _validate(self, database: sqlite3.Connection) -> None:
        try:
            if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ReplicaJournalSyncError("replica journal integrity check failed")
            schema_rows = database.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            actual = {
                (str(kind), str(name)): _normalized_sql(str(sql)) for kind, name, sql in schema_rows
            }
            if actual != _expected_schema():
                raise ReplicaJournalSyncError("replica journal schema objects are unsupported")
            metadata = database.execute(
                "SELECT singleton, application_id, schema_version, journal_id "
                "FROM replica_journal_meta"
            ).fetchall()
            if (
                len(metadata) != 1
                or metadata[0][0] != 0
                or metadata[0][1] != self._application_id
                or metadata[0][2] != REPLICA_JOURNAL_SCHEMA_VERSION
                or not isinstance(metadata[0][3], str)
                or _JOURNAL_ID_PATTERN.fullmatch(metadata[0][3]) is None
            ):
                raise ReplicaJournalSyncError("replica journal metadata is unsupported")
            if not metadata[0][3].endswith(f"_{self._configuration_sha256}"):
                raise ReplicaJournalSyncError(
                    "replica journal configured authentication or limit profile differs"
                )
            self._validate_events(database)
        except ReplicaJournalSyncError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise ReplicaJournalSyncError("replica journal validation failed") from exc

    def _all_rows(self, database: sqlite3.Connection) -> list[tuple[object, ...]]:
        return database.execute(
            """
            SELECT event_id, append_receipt_id, database_incarnation,
                   operation_id, request_type, event_type, stage, request_frame,
                   request_frame_sha256, request_broker_incarnation,
                   request_nonce, request_connection_sequence,
                   request_payload_digest, physical_target_id,
                   replica_generation, execution_owner_id, owner_token,
                   owner_broker_incarnation, stage_status, receipt_json,
                   unresolved_reason, response_frame, response_frame_sha256,
                   created_at
            FROM replica_journal_events ORDER BY event_id
            """
        ).fetchall()

    def _validate_events(self, database: sqlite3.Connection) -> None:
        histories: dict[tuple[str, str, str], list[_Event]] = {}
        prior_event_id = 0
        for row in self._all_rows(database):
            event = _event(tuple(row))
            if type(event.event_id) is not int or event.event_id != prior_event_id + 1:
                raise ReplicaJournalSyncError("replica journal event order is invalid")
            if _APPEND_RECEIPT_PATTERN.fullmatch(event.append_receipt_id) is None:
                raise ReplicaJournalSyncError("replica append receipt ID is invalid")
            _token(event.database_incarnation, "stored database incarnation")
            try:
                validate_replica_operation_id(event.operation_id)
            except ValueError as error:
                raise ReplicaJournalSyncError("replica stored operation ID is invalid") from error
            if event.request_type != "replica.lifecycle":
                raise ReplicaJournalSyncError("replica stored request type is invalid")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", event.created_at):
                raise ReplicaJournalSyncError("replica event timestamp is invalid")
            prior_event_id = event.event_id
            identity = (
                event.database_incarnation,
                event.operation_id,
                event.request_type,
            )
            histories.setdefault(identity, []).append(event)
        for history in histories.values():
            self._replay(history)

    def _rows_for(
        self, database: sqlite3.Connection, identity: tuple[str, str, str]
    ) -> list[_Event]:
        rows = database.execute(
            """
            SELECT event_id, append_receipt_id, database_incarnation,
                   operation_id, request_type, event_type, stage, request_frame,
                   request_frame_sha256, request_broker_incarnation,
                   request_nonce, request_connection_sequence,
                   request_payload_digest, physical_target_id,
                   replica_generation, execution_owner_id, owner_token,
                   owner_broker_incarnation, stage_status, receipt_json,
                   unresolved_reason, response_frame, response_frame_sha256,
                   created_at
            FROM replica_journal_events
            WHERE database_incarnation = ? AND operation_id = ? AND request_type = ?
            ORDER BY event_id
            """,
            identity,
        ).fetchall()
        return [_event(tuple(row)) for row in rows]

    @staticmethod
    def _nulls(event: _Event, names: tuple[str, ...]) -> bool:
        return all(getattr(event, name) is None for name in names)

    def _replay(self, events: list[_Event]) -> _Replay:
        if not events or events[0].event_type != "accepted":
            raise ReplicaJournalSyncError("replica journal acceptance order is invalid")
        accepted = events[0]
        if (
            accepted.stage is not None
            or accepted.request_frame is None
            or accepted.physical_target_id is None
            or accepted.replica_generation is None
            or accepted.execution_owner_id is None
            or not self._nulls(
                accepted,
                (
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
            raise ReplicaJournalSyncError("replica journal acceptance is malformed")
        try:
            accepted_request = _authenticate_request_frame(
                accepted.request_frame,
                self._request_public_key,
            )
        except ValueError as error:
            raise ReplicaJournalSyncError(
                "replica accepted request authentication failed"
            ) from error
        try:
            _validate_replica_payload(accepted_request.payload)
        except ValueError as error:
            raise ReplicaJournalSyncError("replica accepted request payload is invalid") from error
        accepted_binding = (
            accepted.database_incarnation,
            accepted.operation_id,
            accepted.request_type,
            accepted.request_frame_sha256,
            accepted.request_broker_incarnation,
            accepted.request_nonce,
            accepted.request_connection_sequence,
            accepted.request_payload_digest,
        )
        expected_binding = (
            accepted_request.database_incarnation,
            accepted_request.operation_id,
            accepted_request.request_type,
            hashlib.sha256(accepted.request_frame).hexdigest(),
            accepted_request.broker_incarnation,
            accepted_request.nonce,
            accepted_request.connection_sequence,
            accepted_request.payload_digest,
        )
        if accepted_binding != expected_binding:
            raise ReplicaJournalSyncError("replica accepted request binding is invalid")
        authority = ReplicaAuthority(
            accepted.physical_target_id,
            accepted.replica_generation,
            accepted.execution_owner_id,
        )
        if accepted_request.payload["limits_sha256"] != self._expected_limits_sha256:
            raise ReplicaJournalSyncError("replica accepted limit profile is invalid")
        response_public_key = self._response_public_keys.get(accepted_request.broker_incarnation)
        if response_public_key is None:
            raise ReplicaJournalSyncError("replica response signer is not configured")
        if accepted_request.payload["artifact_set_sha256"] != _expected_artifact_set_sha256(
            accepted_request, authority
        ):
            raise ReplicaJournalSyncError("replica accepted artifact set identity is invalid")
        owner_token: str | None = None
        owner_broker_incarnation: str | None = None
        next_stage_index = 0
        stage_started = False
        terminal_state: str | None = None
        terminal_stage: str | None = None
        terminal_response: bytes | None = None

        for index, event in enumerate(events[1:], start=1):
            if terminal_state is not None:
                raise ReplicaJournalSyncError(
                    "replica journal contains an event after terminal state"
                )
            if event.event_type == "authority_claimed":
                if index != 1 or owner_token is not None or event.stage is not None:
                    raise ReplicaJournalSyncError("replica journal authority order is invalid")
                if event.owner_token is None or event.owner_broker_incarnation is None:
                    raise ReplicaJournalSyncError("replica journal authority is malformed")
                _token(event.owner_token, "owner token")
                _token(event.owner_broker_incarnation, "owner broker incarnation")
                if event.owner_broker_incarnation != accepted_request.broker_incarnation:
                    raise ReplicaJournalSyncError(
                        "replica journal owner broker differs from authenticated request"
                    )
                if not self._nulls(
                    event,
                    (
                        "request_frame",
                        "request_frame_sha256",
                        "request_broker_incarnation",
                        "request_nonce",
                        "request_connection_sequence",
                        "request_payload_digest",
                        "physical_target_id",
                        "replica_generation",
                        "execution_owner_id",
                        "stage_status",
                        "receipt_json",
                        "unresolved_reason",
                        "response_frame",
                        "response_frame_sha256",
                    ),
                ):
                    raise ReplicaJournalSyncError("replica journal authority is malformed")
                owner_token = event.owner_token
                owner_broker_incarnation = event.owner_broker_incarnation
                continue
            if owner_token is None or owner_broker_incarnation is None:
                raise ReplicaJournalSyncError("replica journal event precedes authority claim")
            if event.owner_token != owner_token:
                raise ReplicaJournalSyncError("replica journal owner token changed")
            if next_stage_index >= len(_STAGES):
                raise ReplicaJournalSyncError("replica journal has too many stages")
            expected_stage = _STAGES[next_stage_index]
            if event.stage != expected_stage:
                raise ReplicaJournalSyncError("replica journal stage order is invalid")
            if not self._nulls(
                event,
                (
                    "request_frame",
                    "request_frame_sha256",
                    "request_broker_incarnation",
                    "request_nonce",
                    "request_connection_sequence",
                    "request_payload_digest",
                    "physical_target_id",
                    "replica_generation",
                    "execution_owner_id",
                    "owner_broker_incarnation",
                ),
            ):
                raise ReplicaJournalSyncError("replica journal stage event is malformed")
            if event.event_type == "stage_started":
                if stage_started or not self._nulls(
                    event,
                    (
                        "stage_status",
                        "receipt_json",
                        "unresolved_reason",
                        "response_frame",
                        "response_frame_sha256",
                    ),
                ):
                    raise ReplicaJournalSyncError("replica journal stage start is malformed")
                stage_started = True
                continue
            if not stage_started:
                raise ReplicaJournalSyncError("replica journal stage completion precedes its start")
            if event.event_type == "stage_outcome":
                if (
                    event.stage_status != "completed"
                    or event.receipt_json is None
                    or event.unresolved_reason is not None
                ):
                    raise ReplicaJournalSyncError("replica journal stage outcome is malformed")
                receipt = _validate_receipt(expected_stage, event.receipt_json)
                _validate_receipt_binding(
                    expected_stage,
                    receipt,
                    accepted_request,
                    authority,
                )
                if expected_stage == "reclaim":
                    if event.response_frame is None or event.response_frame_sha256 is None:
                        raise ReplicaJournalSyncError(
                            "replica journal terminal response is missing"
                        )
                    if not isinstance(receipt, ReclaimReceipt):
                        raise ReplicaJournalSyncError("replica reclaim receipt type is invalid")
                    _validate_response_frame(
                        event.response_frame,
                        accepted_request,
                        public_key=response_public_key,
                        state="completed",
                        stage="reclaim",
                        code=None,
                        receipt_id=receipt.reclaim_receipt_id,
                    )
                    if (
                        hashlib.sha256(event.response_frame).hexdigest()
                        != event.response_frame_sha256
                    ):
                        raise ReplicaJournalSyncError(
                            "replica journal terminal response digest is invalid"
                        )
                    terminal_state = "completed"
                    terminal_stage = expected_stage
                    terminal_response = event.response_frame
                elif event.response_frame is not None or event.response_frame_sha256 is not None:
                    raise ReplicaJournalSyncError("replica journal nonterminal response is invalid")
                next_stage_index += 1
                stage_started = False
                continue
            if event.event_type == "stage_rejected":
                if (
                    event.stage_status not in _REJECTED_STATUSES[expected_stage]
                    or event.receipt_json is None
                    or event.unresolved_reason is not None
                    or event.response_frame is None
                    or event.response_frame_sha256 is None
                ):
                    raise ReplicaJournalSyncError("replica journal rejection is malformed")
                value = _canonical_object(
                    event.receipt_json,
                    maximum=RECEIPT_JSON_BYTES_MAX,
                    description="rejection receipt",
                )
                if set(value) != {"receipt_id"}:
                    raise ReplicaJournalSyncError("replica journal rejection receipt is malformed")
                rejected_receipt = RejectedReceipt(receipt_id=cast(str, value["receipt_id"]))
                _validate_response_frame(
                    event.response_frame,
                    accepted_request,
                    public_key=response_public_key,
                    state="rejected",
                    stage=expected_stage,
                    code=event.stage_status,
                    receipt_id=rejected_receipt.receipt_id,
                )
                if hashlib.sha256(event.response_frame).hexdigest() != event.response_frame_sha256:
                    raise ReplicaJournalSyncError(
                        "replica journal rejection response digest is invalid"
                    )
                terminal_state = "rejected"
                terminal_stage = expected_stage
                terminal_response = event.response_frame
                continue
            if event.event_type == "stage_unresolved":
                reasons = _BASE_UNRESOLVED_REASONS | _STAGE_UNRESOLVED_REASONS[expected_stage]
                if (
                    event.stage_status is not None
                    or event.receipt_json is not None
                    or event.unresolved_reason not in reasons
                    or event.response_frame is None
                    or event.response_frame_sha256 is None
                ):
                    raise ReplicaJournalSyncError("replica journal unresolved event is malformed")
                _validate_response_frame(
                    event.response_frame,
                    accepted_request,
                    public_key=response_public_key,
                    state="unresolved",
                    stage=expected_stage,
                    code=event.unresolved_reason,
                    receipt_id=None,
                )
                if hashlib.sha256(event.response_frame).hexdigest() != event.response_frame_sha256:
                    raise ReplicaJournalSyncError(
                        "replica journal unresolved response digest is invalid"
                    )
                terminal_state = "unresolved"
                terminal_stage = expected_stage
                terminal_response = event.response_frame
                continue
            raise ReplicaJournalSyncError("replica journal event type is unsupported")

        return _Replay(
            request_frame=accepted.request_frame,
            authority=authority,
            owner_token=owner_token,
            owner_broker_incarnation=owner_broker_incarnation,
            next_stage_index=next_stage_index,
            stage_started=stage_started,
            terminal_state=terminal_state,
            terminal_stage=terminal_stage,
            response_frame=terminal_response,
        )

    def _journal_id(self, database: sqlite3.Connection) -> str:
        row = database.execute(
            "SELECT journal_id FROM replica_journal_meta WHERE singleton = 0"
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            raise ReplicaJournalSyncError("replica journal ID is missing")
        return row[0]

    def _position(self, database: sqlite3.Connection, event: _Event) -> ReplicaJournalPosition:
        return ReplicaJournalPosition(
            journal_id=self._journal_id(database),
            sequence=event.event_id,
            append_receipt_id=event.append_receipt_id,
        )

    def _decision(self, replay: _Replay) -> ReplicaJournalDecision:
        if replay.terminal_state is not None:
            return ReplicaJournalDecision(
                replay.terminal_state,
                replay.terminal_stage,
                replay.response_frame,
                replay.owner_broker_incarnation,
            )
        if replay.owner_token is None:
            return ReplicaJournalDecision("accepted", None, None, None)
        stage = (
            _STAGES[replay.next_stage_index]
            if replay.next_stage_index < len(_STAGES)
            else "reclaim"
        )
        return ReplicaJournalDecision(
            "in_flight" if replay.stage_started else "claimed",
            stage,
            None,
            replay.owner_broker_incarnation,
        )

    def status(self, request: BrokerRequest) -> ReplicaJournalDecision:
        identity = self._identity(request)
        database = self._open()
        try:
            events = self._rows_for(database, identity)
            if not events:
                return ReplicaJournalDecision("absent", None, None, None)
            _validate_request_frame(events[0].request_frame, request)
            return self._decision(self._replay(events))
        finally:
            database.close()

    def require_response_signer(
        self,
        broker_incarnation: str,
        private_key: Ed25519PrivateKey,
    ) -> None:
        """Require a private signer that matches one configured response key."""
        _token(broker_incarnation, "response broker incarnation")
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError("replica response private key is invalid")
        expected = self._response_public_keys.get(broker_incarnation)
        if expected is None:
            raise ValueError("replica response signer incarnation is not configured")
        actual_bytes = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        expected_bytes = expected.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        if actual_bytes != expected_bytes:
            raise ValueError("replica response signer does not match configured authority")

    def validate_stage_receipt(
        self,
        request: BrokerRequest,
        authority: ReplicaAuthority,
        stage: str,
        receipt: object,
    ) -> None:
        """Validate a typed effect receipt without opening the journal database."""
        if not isinstance(request, BrokerRequest):
            raise TypeError("replica receipt request is invalid")
        if not isinstance(authority, ReplicaAuthority):
            raise TypeError("replica receipt authority is invalid")
        try:
            self._validate_stage(stage)
            _receipt_json(stage, receipt)
            _validate_receipt_binding(stage, receipt, request, authority)
        except (TypeError, ValueError) as error:
            raise ReplicaJournalEffectResultError("replica stage receipt is invalid") from error

    def validate_rejection(
        self,
        stage: str,
        status: str,
        receipt: RejectedReceipt,
    ) -> None:
        """Validate a rejected effect result without opening the database."""
        try:
            self._validate_stage(stage)
            if status not in _REJECTED_STATUSES[stage]:
                raise ValueError("replica rejection status is invalid")
            if not isinstance(receipt, RejectedReceipt):
                raise TypeError("replica rejection receipt is invalid")
            _rejected_json(receipt)
        except (TypeError, ValueError) as error:
            raise ReplicaJournalEffectResultError(
                "replica rejected effect result is invalid"
            ) from error

    def validate_unresolved_reason(self, stage: str, reason: str) -> None:
        """Validate an unresolved reason without opening the database."""
        try:
            self._validate_stage(stage)
            if reason not in _BASE_UNRESOLVED_REASONS | _STAGE_UNRESOLVED_REASONS[stage]:
                raise ValueError("replica unresolved reason is invalid for the stage")
        except (TypeError, ValueError) as error:
            raise ReplicaJournalEffectResultError(
                "replica unresolved effect result is invalid"
            ) from error

    def accept(
        self,
        request: BrokerRequest,
        request_frame: bytes,
        authority: ReplicaAuthority,
    ) -> ReplicaJournalDecision:
        identity = self._identity(request)
        try:
            authenticated_request = _authenticate_request_frame(
                request_frame,
                self._request_public_key,
            )
        except ValueError as error:
            raise ReplicaJournalConflictError("replica request authentication failed") from error
        if authenticated_request != request:
            raise ReplicaJournalConflictError("replica request frame does not bind the request")
        frame = _validate_request_frame(request_frame, request)
        self._response_public_key(request)
        if not isinstance(authority, ReplicaAuthority):
            raise TypeError("replica authority is invalid")
        payload = _validate_replica_payload(request.payload)
        if parse_replica_authority(payload["authority"]) != authority:
            raise ReplicaJournalConflictError(
                "replica request authority differs from supplied authority"
            )
        if payload["limits_sha256"] != self._expected_limits_sha256:
            raise ReplicaJournalConflictError("replica request limit profile is not configured")
        expected_artifact_set = _expected_artifact_set_sha256(request, authority)
        if payload["artifact_set_sha256"] != expected_artifact_set:
            raise ReplicaJournalConflictError("replica artifact set identity is invalid")
        database = self._open()
        try:
            events = self._rows_for(database, identity)
            if events:
                replay = self._replay(events)
                if replay.request_frame != frame or replay.authority != authority:
                    raise ReplicaJournalConflictError(
                        "replica acceptance conflicts with durable authority"
                    )
                return self._decision(replay)
        finally:
            database.close()
        self._append(
            identity,
            event_type="accepted",
            request_frame=frame,
            physical_target_id=authority.physical_target_id,
            replica_generation=authority.replica_generation,
            execution_owner_id=authority.execution_owner_id,
        )
        return self.status(request)

    def claim_authority(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        broker_incarnation: str,
    ) -> ReplicaJournalDecision:
        identity = self._identity(request)
        _token(owner_token, "owner token")
        _token(broker_incarnation, "owner broker incarnation")
        if broker_incarnation != request.broker_incarnation:
            raise ReplicaJournalConflictError(
                "replica owner broker differs from authenticated request"
            )
        database = self._open()
        try:
            events = self._rows_for(database, identity)
            if not events:
                raise ReplicaJournalConflictError("replica acceptance is missing")
            _validate_request_frame(events[0].request_frame, request)
            replay = self._replay(events)
            if replay.owner_token is not None:
                if (
                    replay.owner_token != owner_token
                    or replay.owner_broker_incarnation != broker_incarnation
                ):
                    raise ReplicaJournalConflictError(
                        "replica authority conflicts with durable owner"
                    )
                return self._decision(replay)
        finally:
            database.close()
        self._append(
            identity,
            event_type="authority_claimed",
            owner_token=owner_token,
            owner_broker_incarnation=broker_incarnation,
        )
        return self.status(request)

    def begin_stage(
        self, request: BrokerRequest, *, owner_token: str, stage: str
    ) -> ReplicaJournalPosition:
        identity = self._identity(request)
        _token(owner_token, "owner token")
        self._validate_stage(stage)
        existing = self._existing_completion_or_start(
            request, owner_token=owner_token, stage=stage, event_type="stage_started"
        )
        if existing is not None:
            return existing
        replay = self._require_active(request, owner_token=owner_token, stage=stage)
        if replay.stage_started:
            raise ReplicaJournalConflictError("replica stage already started")
        return self._append(
            identity,
            event_type="stage_started",
            stage=stage,
            owner_token=owner_token,
        )

    def complete_ingest(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        receipt: IngestReceipt,
        response_frame: bytes | None = None,
    ) -> ReplicaJournalPosition:
        return self._complete(request, owner_token, "ingest", receipt, response_frame)

    def complete_verify(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        receipt: VerifyReceipt,
        response_frame: bytes | None = None,
    ) -> ReplicaJournalPosition:
        return self._complete(request, owner_token, "verify", receipt, response_frame)

    def complete_publish(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        receipt: PublishReceipt,
        response_frame: bytes | None = None,
    ) -> ReplicaJournalPosition:
        return self._complete(request, owner_token, "publish", receipt, response_frame)

    def complete_admission(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        receipt: AdmissionReceipt,
        response_frame: bytes | None = None,
    ) -> ReplicaJournalPosition:
        return self._complete(request, owner_token, "admission", receipt, response_frame)

    def complete_drain(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        receipt: DrainReceipt,
        response_frame: bytes | None = None,
    ) -> ReplicaJournalPosition:
        return self._complete(request, owner_token, "drain", receipt, response_frame)

    def complete_export(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        receipt: ExportReceipt,
        response_frame: bytes | None = None,
    ) -> ReplicaJournalPosition:
        return self._complete(request, owner_token, "export", receipt, response_frame)

    def complete_reclaim(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        receipt: ReclaimReceipt,
        response_frame: bytes | None = None,
    ) -> ReplicaJournalPosition:
        return self._complete(request, owner_token, "reclaim", receipt, response_frame)

    def _complete(
        self,
        request: BrokerRequest,
        owner_token: str,
        stage: str,
        receipt: object,
        response_frame: bytes | None,
    ) -> ReplicaJournalPosition:
        identity = self._identity(request)
        _token(owner_token, "owner token")
        receipt_json = _receipt_json(stage, receipt)
        if stage == "reclaim":
            if response_frame is None:
                raise ValueError("replica reclaim response is required")
            if not isinstance(receipt, ReclaimReceipt):
                raise TypeError("replica reclaim receipt type is invalid")
            terminal_response = _validate_response_frame(
                response_frame,
                request,
                public_key=self._response_public_key(request),
                state="completed",
                stage="reclaim",
                code=None,
                receipt_id=receipt.reclaim_receipt_id,
            )
        else:
            if response_frame is not None:
                raise ValueError("replica nonterminal response is not permitted")
            terminal_response = None
        existing = self._existing_exact_completion(
            request,
            owner_token=owner_token,
            stage=stage,
            event_type="stage_outcome",
            stage_status="completed",
            receipt_json=receipt_json,
            unresolved_reason=None,
            response_frame=terminal_response,
        )
        if existing is not None:
            return existing
        replay = self._require_active(request, owner_token=owner_token, stage=stage)
        if not replay.stage_started:
            raise ReplicaJournalConflictError("replica stage start is missing")
        self.validate_stage_receipt(
            _authenticate_request_frame(replay.request_frame, self._request_public_key),
            replay.authority,
            stage,
            receipt,
        )
        return self._append(
            identity,
            event_type="stage_outcome",
            stage=stage,
            owner_token=owner_token,
            stage_status="completed",
            receipt_json=receipt_json,
            response_frame=terminal_response,
        )

    def record_rejected(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
        status: str,
        receipt: RejectedReceipt,
        response_frame: bytes,
    ) -> ReplicaJournalPosition:
        identity = self._identity(request)
        _token(owner_token, "owner token")
        self.validate_rejection(stage, status, receipt)
        receipt_json = _rejected_json(receipt)
        response = _validate_response_frame(
            response_frame,
            request,
            public_key=self._response_public_key(request),
            state="rejected",
            stage=stage,
            code=status,
            receipt_id=receipt.receipt_id,
        )
        existing = self._existing_exact_completion(
            request,
            owner_token=owner_token,
            stage=stage,
            event_type="stage_rejected",
            stage_status=status,
            receipt_json=receipt_json,
            unresolved_reason=None,
            response_frame=response,
        )
        if existing is not None:
            return existing
        replay = self._require_active(request, owner_token=owner_token, stage=stage)
        if not replay.stage_started:
            raise ReplicaJournalConflictError("replica stage start is missing")
        return self._append(
            identity,
            event_type="stage_rejected",
            stage=stage,
            owner_token=owner_token,
            stage_status=status,
            receipt_json=receipt_json,
            response_frame=response,
        )

    def record_unresolved(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
        reason: str,
        response_frame: bytes,
    ) -> ReplicaJournalPosition:
        identity = self._identity(request)
        _token(owner_token, "owner token")
        self.validate_unresolved_reason(stage, reason)
        response = _validate_response_frame(
            response_frame,
            request,
            public_key=self._response_public_key(request),
            state="unresolved",
            stage=stage,
            code=reason,
            receipt_id=None,
        )
        existing = self._existing_exact_completion(
            request,
            owner_token=owner_token,
            stage=stage,
            event_type="stage_unresolved",
            stage_status=None,
            receipt_json=None,
            unresolved_reason=reason,
            response_frame=response,
        )
        if existing is not None:
            return existing
        replay = self._require_active(request, owner_token=owner_token, stage=stage)
        if not replay.stage_started:
            raise ReplicaJournalConflictError("replica stage start is missing")
        return self._append(
            identity,
            event_type="stage_unresolved",
            stage=stage,
            owner_token=owner_token,
            unresolved_reason=reason,
            response_frame=response,
        )

    def incomplete_lifecycles(self) -> tuple[IncompleteReplicaLifecycle, ...]:
        database = self._open()
        try:
            identities = database.execute(
                """
                SELECT database_incarnation, operation_id, request_type, MIN(event_id)
                FROM replica_journal_events GROUP BY 1, 2, 3 ORDER BY MIN(event_id)
                """
            ).fetchall()
            pending: list[IncompleteReplicaLifecycle] = []
            for database_id, operation_id, request_type, _first in identities:
                events = self._rows_for(
                    database,
                    (str(database_id), str(operation_id), str(request_type)),
                )
                replay = self._replay(events)
                if replay.owner_token is None or replay.terminal_state is not None:
                    continue
                if replay.next_stage_index >= len(_STAGES):
                    raise ReplicaJournalSyncError("replica nonterminal lifecycle has no next stage")
                pending.append(
                    IncompleteReplicaLifecycle(
                        request_frame=replay.request_frame,
                        owner_token=replay.owner_token,
                        owner_broker_incarnation=cast(str, replay.owner_broker_incarnation),
                        stage=_STAGES[replay.next_stage_index],
                        stage_started=replay.stage_started,
                        reason="broker_restart_unknown",
                        authority=replay.authority,
                    )
                )
            return tuple(pending)
        finally:
            database.close()

    def authenticated_request(self, lifecycle: IncompleteReplicaLifecycle) -> BrokerRequest:
        """Authenticate and return the request for an incomplete lifecycle."""
        if not isinstance(lifecycle, IncompleteReplicaLifecycle):
            raise TypeError("replica lifecycle is invalid")
        if not lifecycle.request_frame:
            raise ReplicaJournalSyncError("replica lifecycle request frame is invalid")
        try:
            return _authenticate_request_frame(
                lifecycle.request_frame,
                self._request_public_key,
            )
        except ValueError as error:
            raise ReplicaJournalSyncError(
                "replica lifecycle request authentication failed"
            ) from error

    @staticmethod
    def _validate_stage(stage: str) -> None:
        if not isinstance(stage, str) or stage not in _STAGE_INDEX:
            raise ValueError("replica stage is invalid")

    def _require_active(self, request: BrokerRequest, *, owner_token: str, stage: str) -> _Replay:
        self._validate_stage(stage)
        database = self._open()
        try:
            events = self._rows_for(database, self._identity(request))
            if not events:
                raise ReplicaJournalConflictError("replica acceptance is missing")
            _validate_request_frame(events[0].request_frame, request)
            replay = self._replay(events)
            if replay.terminal_state is not None:
                raise ReplicaJournalConflictError("replica lifecycle is already terminal")
            if replay.owner_token != owner_token:
                raise ReplicaJournalConflictError("replica lifecycle owner conflicts")
            if replay.next_stage_index >= len(_STAGES) or _STAGES[replay.next_stage_index] != stage:
                raise ReplicaJournalConflictError("replica stage order conflicts")
            return replay
        finally:
            database.close()

    def _existing_completion_or_start(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
        event_type: str,
    ) -> ReplicaJournalPosition | None:
        database = self._open()
        try:
            events = self._rows_for(database, self._identity(request))
            if events:
                _validate_request_frame(events[0].request_frame, request)
            matches = [
                event for event in events if event.event_type == event_type and event.stage == stage
            ]
            if not matches:
                return None
            event = matches[0]
            if event.owner_token != owner_token:
                raise ReplicaJournalConflictError("replica stage owner conflicts")
            return self._position(database, event)
        finally:
            database.close()

    def _existing_exact_completion(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
        event_type: str,
        stage_status: str | None,
        receipt_json: bytes | None,
        unresolved_reason: str | None,
        response_frame: bytes | None,
    ) -> ReplicaJournalPosition | None:
        database = self._open()
        try:
            events = self._rows_for(database, self._identity(request))
            if events:
                _validate_request_frame(events[0].request_frame, request)
            completions = [
                event
                for event in events
                if event.stage == stage
                and event.event_type in {"stage_outcome", "stage_rejected", "stage_unresolved"}
            ]
            if not completions:
                return None
            event = completions[0]
            exact = (
                event.event_type == event_type
                and event.owner_token == owner_token
                and event.stage_status == stage_status
                and event.receipt_json == receipt_json
                and event.unresolved_reason == unresolved_reason
                and event.response_frame == response_frame
                and event.response_frame_sha256
                == (None if response_frame is None else hashlib.sha256(response_frame).hexdigest())
            )
            if not exact:
                raise ReplicaJournalConflictError(
                    "replica stage completion conflicts with durable state"
                )
            return self._position(database, event)
        finally:
            database.close()

    def _append(
        self,
        identity: tuple[str, str, str],
        *,
        event_type: str,
        stage: str | None = None,
        request_frame: bytes | None = None,
        physical_target_id: str | None = None,
        replica_generation: int | None = None,
        execution_owner_id: str | None = None,
        owner_token: str | None = None,
        owner_broker_incarnation: str | None = None,
        stage_status: str | None = None,
        receipt_json: bytes | None = None,
        unresolved_reason: str | None = None,
        response_frame: bytes | None = None,
    ) -> ReplicaJournalPosition:
        request = None if request_frame is None else _request_from_frame(request_frame)
        append_receipt_id = secrets.token_hex(16)
        values: tuple[object, ...] = (
            append_receipt_id,
            *identity,
            event_type,
            stage,
            request_frame,
            None if request_frame is None else hashlib.sha256(request_frame).hexdigest(),
            None if request is None else request.broker_incarnation,
            None if request is None else request.nonce,
            None if request is None else request.connection_sequence,
            None if request is None else request.payload_digest,
            physical_target_id,
            replica_generation,
            execution_owner_id,
            owner_token,
            owner_broker_incarnation,
            stage_status,
            receipt_json,
            unresolved_reason,
            response_frame,
            None if response_frame is None else hashlib.sha256(response_frame).hexdigest(),
        )
        database = self._open()
        event_id: int | None = None
        commit_uncertain = False
        collision = False
        try:
            database.execute("BEGIN IMMEDIATE")
            cursor = database.execute(
                """
                INSERT INTO replica_journal_events(
                    append_receipt_id, database_incarnation, operation_id,
                    request_type, event_type, stage, request_frame,
                    request_frame_sha256, request_broker_incarnation,
                    request_nonce, request_connection_sequence,
                    request_payload_digest, physical_target_id,
                    replica_generation, execution_owner_id, owner_token,
                    owner_broker_incarnation, stage_status, receipt_json,
                    unresolved_reason, response_frame, response_frame_sha256
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                values,
            )
            event_id = cast(int, cursor.lastrowid)
            try:
                self._commit_transaction(database)
            except (OSError, sqlite3.Error):
                commit_uncertain = True
        except sqlite3.IntegrityError:
            collision = True
        except (OSError, sqlite3.Error) as exc:
            raise ReplicaJournalSyncError("replica journal append failed") from exc
        finally:
            database.close()
        if commit_uncertain or collision:
            return self._confirm_appended_event(
                identity=identity,
                append_receipt_id=append_receipt_id,
                values=values,
                collision=collision,
            )
        if event_id is None:
            raise ReplicaJournalSyncError("replica journal append position is missing")
        database = self._open()
        try:
            return ReplicaJournalPosition(self._journal_id(database), event_id, append_receipt_id)
        finally:
            database.close()

    def _commit_transaction(self, database: sqlite3.Connection) -> None:
        """Commit one append through an overridable fault boundary."""
        database.commit()

    def _inspect_intended_event(
        self,
        identity: tuple[str, str, str],
        append_receipt_id: str,
        values: tuple[object, ...],
    ) -> tuple[str, int | None, str | None]:
        selected = (
            "event_id, append_receipt_id, database_incarnation, operation_id, "
            "request_type, event_type, stage, request_frame, request_frame_sha256, "
            "request_broker_incarnation, request_nonce, request_connection_sequence, "
            "request_payload_digest, physical_target_id, replica_generation, "
            "execution_owner_id, owner_token, owner_broker_incarnation, stage_status, "
            "receipt_json, unresolved_reason, response_frame, response_frame_sha256"
        )
        database = self._open()
        try:
            row = database.execute(
                f"SELECT {selected} FROM replica_journal_events WHERE append_receipt_id = ?",
                (append_receipt_id,),
            ).fetchone()
            if row is not None:
                if tuple(row[1:]) == values:
                    return "present", cast(int, row[0]), cast(str, row[1])
                return "conflict", None, None
            event_type = cast(str, values[4])
            stage = cast(str | None, values[5])
            if event_type in {"accepted", "authority_claimed"}:
                clause = "event_type = ?"
                parameters: tuple[object, ...] = (*identity, event_type)
            elif event_type == "stage_started":
                clause = "event_type = ? AND stage = ?"
                parameters = (*identity, event_type, stage)
            else:
                clause = (
                    "event_type IN ('stage_outcome', 'stage_rejected', "
                    "'stage_unresolved') AND stage = ?"
                )
                parameters = (*identity, stage)
            winner = database.execute(
                f"SELECT {selected} FROM replica_journal_events "
                "WHERE database_incarnation = ? AND operation_id = ? "
                f"AND request_type = ? AND {clause} LIMIT 1",
                parameters,
            ).fetchone()
            if winner is None:
                return "absent", None, None
            if tuple(winner[2:]) == values[1:]:
                return "equivalent", cast(int, winner[0]), cast(str, winner[1])
            return "conflict", None, None
        except (OSError, sqlite3.Error, TypeError) as exc:
            raise ReplicaJournalSyncError("replica journal verification failed") from exc
        finally:
            database.close()

    def _confirm_appended_event(
        self,
        *,
        identity: tuple[str, str, str],
        append_receipt_id: str,
        values: tuple[object, ...],
        collision: bool,
    ) -> ReplicaJournalPosition:
        verdict, event_id, durable_receipt_id = self._inspect_intended_event(
            identity, append_receipt_id, values
        )
        if verdict in {"present", "equivalent"} and event_id is not None:
            if durable_receipt_id is None:
                raise ReplicaJournalSyncError("replica journal durable receipt ID is missing")
            database = self._open()
            try:
                return ReplicaJournalPosition(
                    self._journal_id(database), event_id, durable_receipt_id
                )
            finally:
                database.close()
        if verdict == "absent" and not collision:
            raise ReplicaJournalCommitAbsent("replica journal commit is absent after verification")
        if collision:
            raise ReplicaJournalConflictError(
                "replica journal transition conflicts with durable state"
            )
        raise ReplicaJournalSyncError("replica journal commit conflicts with persisted state")
