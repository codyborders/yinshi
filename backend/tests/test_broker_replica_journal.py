"""Check immutable broker replica authority, ordered transitions, and recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from yinshi.services.broker_protocol import (
    BROKER_PROTOCOL_VERSION,
    BrokerRequest,
    JsonValue,
    canonical_json,
    create_signed_request,
    create_signed_response,
    parse_signed_request,
)
from yinshi.services.broker_replica_journal import (
    REPLICA_JOURNAL_SCHEMA_STATEMENTS,
    AdmissionReceipt,
    ArtifactReference,
    BrokerReplicaJournal,
    DrainReceipt,
    ExportReceipt,
    IngestReceipt,
    PublishReceipt,
    ReclaimReceipt,
    RejectedReceipt,
    ReplicaAuthority,
    ReplicaJournalCommitAbsent,
    ReplicaJournalDecision,
    ReplicaJournalPosition,
    ReplicaJournalSyncError,
    VerifyReceipt,
)
from yinshi.services.replica_artifact_contract import (
    compute_replica_artifact_set_sha256,
)

APPLICATION_ID = "yinshi-desktop"
DATABASE_INCARNATION = "d" * 32
BROKER_INCARNATION = "b" * 32
OWNER_TOKEN = "owner_token_00000000000000000000"
REQUEST_PRIVATE_KEY = Ed25519PrivateKey.generate()
RESPONSE_PRIVATE_KEY = Ed25519PrivateKey.generate()
STAGES = ("ingest", "verify", "publish", "admission", "drain", "export", "reclaim")
AUTHORITY = ReplicaAuthority(
    physical_target_id="target_00000000000000000000000000",
    replica_generation=3,
    execution_owner_id="execution_owner_0000000000000000",
)
BUNDLE = ArtifactReference(
    artifact_id="bundle_00000000000000000000000000",
    sha256="1" * 64,
    byte_length=123,
)
WORKTREE = ArtifactReference(
    artifact_id="worktree_0000000000000000000000",
    sha256="2" * 64,
    byte_length=456,
)
INDEX_OBJECTS = ArtifactReference(
    artifact_id="index_objects_0000000000000000000",
    sha256="7" * 64,
    byte_length=789,
)
LIMITS_SHA256 = "8" * 64


def artifact_json(reference: ArtifactReference) -> dict[str, JsonValue]:
    return {
        "artifact_id": reference.artifact_id,
        "byte_length": reference.byte_length,
        "sha256": reference.sha256,
    }


def artifact_binding_json(
    role: str,
    media_type: str,
    reference: ArtifactReference,
) -> dict[str, object]:
    return {
        "role": role,
        "media_type": media_type,
        **artifact_json(reference),
    }


def artifact_set_sha256(
    operation_id: str,
    *,
    object_format: str = "sha256",
) -> str:
    return compute_replica_artifact_set_sha256(
        operation_id=operation_id,
        repository_id="repository_0000000000000000000000",
        workspace_id="workspace_000000000000000000000000",
        physical_target_id=AUTHORITY.physical_target_id,
        replica_generation=AUTHORITY.replica_generation,
        execution_owner_id=AUTHORITY.execution_owner_id,
        object_format=object_format,
        source_state_sha256="4" * 64,
        reconciliation_fingerprint="3" * 64,
        bundle=artifact_binding_json(
            "committed_bundle",
            "application/vnd.yinshi.git-bundle.v1",
            BUNDLE,
        ),
        worktree=artifact_binding_json(
            "worktree",
            "application/vnd.yinshi.replica-worktree.v1",
            WORKTREE,
        ),
        index_objects=artifact_binding_json(
            "index_objects",
            "application/vnd.yinshi.git-index-objects-pack.v1",
            INDEX_OBJECTS,
        ),
        limits_sha256=LIMITS_SHA256,
    )


ARTIFACT_SET_SHA256 = artifact_set_sha256("a" * 32)


def request_payload(operation_id: str = "a" * 32) -> dict[str, JsonValue]:
    return {
        "artifact_set_sha256": artifact_set_sha256(operation_id),
        "bundle": artifact_json(BUNDLE),
        "index_objects": artifact_json(INDEX_OBJECTS),
        "limits_sha256": LIMITS_SHA256,
        "object_format": "sha256",
        "reconciliation_fingerprint": "3" * 64,
        "repository_id": "repository_0000000000000000000000",
        "source_state_sha256": "4" * 64,
        "workspace_id": "workspace_000000000000000000000000",
        "worktree": artifact_json(WORKTREE),
    }


def signed_request(
    _key: Ed25519PrivateKey,
    *,
    operation_id: str = "a" * 32,
    sequence: int = 1,
    payload: dict[str, JsonValue] | None = None,
) -> tuple[BrokerRequest, bytes]:
    frame = create_signed_request(
        private_key=REQUEST_PRIVATE_KEY,
        protocol_version=BROKER_PROTOCOL_VERSION,
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        connection_sequence=sequence,
        operation_id=operation_id,
        request_type="replica.lifecycle",
        nonce=f"nonce_{sequence:011d}",
        payload=request_payload(operation_id) if payload is None else payload,
    )
    return parse_signed_request(frame, public_key=REQUEST_PRIVATE_KEY.public_key()), frame


def response_frame(
    request: BrokerRequest,
    *,
    state: str,
    stage: str,
    code: str | None = None,
    receipt_id: str | None = None,
    result_extra: dict[str, JsonValue] | None = None,
) -> bytes:
    result: dict[str, JsonValue] = {"stage": stage, "state": state}
    if code is not None:
        result["code"] = code
    if receipt_id is not None:
        result["receipt_id"] = receipt_id
    if result_extra is not None:
        result.update(result_extra)
    return create_signed_response(
        request,
        private_key=RESPONSE_PRIVATE_KEY,
        status="ok" if state == "completed" else "error",
        error=None if state == "completed" else code,
        result=result,
    )


def configured_journal(path: Path) -> BrokerReplicaJournal:
    return BrokerReplicaJournal(
        path,
        application_id=APPLICATION_ID,
        expected_limits_sha256=LIMITS_SHA256,
        request_public_key=REQUEST_PRIVATE_KEY.public_key(),
        response_public_keys={BROKER_INCARNATION: RESPONSE_PRIVATE_KEY.public_key()},
    )


def journal(tmp_path: Path) -> BrokerReplicaJournal:
    return configured_journal(tmp_path / "replica.sqlite3")


def receipt_for(stage: str, artifact_set: str = ARTIFACT_SET_SHA256) -> object:
    receipt_id = f"receipt_{stage}_000000000000000000"
    if stage == "ingest":
        return IngestReceipt(
            receipt_id=receipt_id,
            bundle=BUNDLE,
            worktree=WORKTREE,
            index_objects=INDEX_OBJECTS,
        )
    if stage == "verify":
        return VerifyReceipt(
            receipt_id=receipt_id,
            bundle_sha256=BUNDLE.sha256,
            worktree_sha256=WORKTREE.sha256,
            index_objects_sha256=INDEX_OBJECTS.sha256,
            source_state_sha256="4" * 64,
            reconciliation_fingerprint="3" * 64,
            artifact_set_sha256=artifact_set,
        )
    if stage == "publish":
        return PublishReceipt(
            receipt_id=receipt_id,
            physical_target_id=AUTHORITY.physical_target_id,
            replica_generation=AUTHORITY.replica_generation,
            execution_owner_id=AUTHORITY.execution_owner_id,
            publication_marker_sha256="5" * 64,
            synchronization_receipt_id="sync_000000000000000000000000000",
            artifact_set_sha256=artifact_set,
        )
    if stage == "admission":
        return AdmissionReceipt(
            receipt_id=receipt_id,
            execution_owner_id=AUTHORITY.execution_owner_id,
            runtime_unit_id="runtime_0000000000000000000000000",
            session_socket_sha256="6" * 64,
        )
    if stage == "drain":
        return DrainReceipt(
            drain_ack_receipt_id="drain_ack_0000000000000000000000",
            quiescence_receipt_id="quiescence_00000000000000000000",
        )
    if stage == "export":
        return ExportReceipt(
            export_receipt_id="export_000000000000000000000000",
            bundle=BUNDLE,
            worktree=WORKTREE,
            index_objects=INDEX_OBJECTS,
        )
    if stage == "reclaim":
        return ReclaimReceipt(reclaim_receipt_id="reclaim_0000000000000000000000")
    raise AssertionError(stage)


def complete(
    replica: BrokerReplicaJournal,
    request: BrokerRequest,
    stage: str,
    *,
    final_response: bytes | None = None,
) -> ReplicaJournalPosition:
    method = getattr(replica, f"complete_{stage}")
    return method(
        request,
        owner_token=OWNER_TOKEN,
        receipt=receipt_for(stage, str(request.payload["artifact_set_sha256"])),
        response_frame=final_response,
    )


def begin(replica: BrokerReplicaJournal, request: BrokerRequest, stage: str) -> None:
    replica.begin_stage(request, owner_token=OWNER_TOKEN, stage=stage)


def accept_and_claim(replica: BrokerReplicaJournal, request: BrokerRequest, frame: bytes) -> None:
    replica.accept(request, frame, AUTHORITY)
    replica.claim_authority(
        request,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )


def drive(
    replica: BrokerReplicaJournal,
    request: BrokerRequest,
    frame: bytes,
    *,
    through: str | None = None,
) -> None:
    accept_and_claim(replica, request, frame)
    if through is None:
        return
    for stage in STAGES:
        begin(replica, request, stage)
        if stage == through:
            return
        complete(replica, request, stage)


def rows(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as database:
        return database.execute(
            "SELECT event_id, append_receipt_id, event_type, stage, receipt_json, "
            "response_frame FROM replica_journal_events ORDER BY event_id"
        ).fetchall()


def test_schema_is_exact_and_all_rows_are_immutable(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    with sqlite3.connect(replica.path) as database:
        actual = {
            (kind, name)
            for kind, name in database.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        expected_names = {
            "replica_journal_meta",
            "replica_journal_events",
            "replica_journal_one_acceptance",
            "replica_journal_one_claim",
            "replica_journal_one_stage_start",
            "replica_journal_one_stage_completion",
            "replica_journal_one_terminal",
            "replica_journal_reject_update",
            "replica_journal_reject_delete",
            "replica_journal_require_transition",
            "replica_journal_meta_reject_update",
            "replica_journal_meta_reject_delete",
        }
        assert {name for _kind, name in actual} == expected_names
        assert len(REPLICA_JOURNAL_SCHEMA_STATEMENTS) == len(expected_names)
        metadata = database.execute(
            "SELECT application_id, schema_version, length(journal_id) FROM replica_journal_meta"
        ).fetchone()
        assert metadata == (APPLICATION_ID, 1, 97)
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("UPDATE replica_journal_meta SET schema_version = 2")

    request, frame = signed_request(Ed25519PrivateKey.generate())
    replica.accept(request, frame, AUTHORITY)
    with sqlite3.connect(replica.path) as database, pytest.raises(sqlite3.IntegrityError):
        database.execute("DELETE FROM replica_journal_events")


def test_accept_claim_and_full_lifecycle_are_exactly_replayable(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    accepted = replica.accept(request, frame, AUTHORITY)
    assert accepted == ReplicaJournalDecision(
        state="accepted", stage=None, response_frame=None, owner_broker_incarnation=None
    )
    assert replica.accept(request, frame, AUTHORITY) == accepted

    claimed = replica.claim_authority(
        request,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )
    assert claimed.owner_broker_incarnation == BROKER_INCARNATION
    assert (
        replica.claim_authority(
            request,
            owner_token=OWNER_TOKEN,
            broker_incarnation=BROKER_INCARNATION,
        )
        == claimed
    )

    positions: list[ReplicaJournalPosition] = []
    reclaim_receipt = receipt_for("reclaim")
    assert isinstance(reclaim_receipt, ReclaimReceipt)
    terminal_response = response_frame(
        request,
        state="completed",
        stage="reclaim",
        receipt_id=reclaim_receipt.reclaim_receipt_id,
    )
    for stage in STAGES:
        start = replica.begin_stage(request, owner_token=OWNER_TOKEN, stage=stage)
        assert replica.begin_stage(request, owner_token=OWNER_TOKEN, stage=stage) == start
        terminal = terminal_response if stage == "reclaim" else None
        position = complete(replica, request, stage, final_response=terminal)
        assert complete(replica, request, stage, final_response=terminal) == position
        positions.extend((start, position))

    assert len({item.append_receipt_id for item in positions}) == len(positions)
    assert replica.status(request) == ReplicaJournalDecision(
        state="completed",
        stage="reclaim",
        response_frame=terminal_response,
        owner_broker_incarnation=BROKER_INCARNATION,
    )
    assert configured_journal(replica.path).status(request) == replica.status(request)


def test_transitions_require_claim_start_prior_success_and_same_owner(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    replica.accept(request, frame, AUTHORITY)
    with pytest.raises(ReplicaJournalSyncError):
        begin(replica, request, "ingest")
    replica.claim_authority(
        request,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )
    with pytest.raises(ReplicaJournalSyncError):
        begin(replica, request, "verify")
    begin(replica, request, "ingest")
    with pytest.raises(ReplicaJournalSyncError):
        complete(replica, request, "verify")
    with pytest.raises(ReplicaJournalSyncError):
        replica.complete_ingest(
            request,
            owner_token="other_owner_00000000000000000000",
            receipt=receipt_for("ingest"),
        )


def test_unresolved_and_rejected_are_terminal(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    request, frame = signed_request(key)
    replica = journal(tmp_path)
    drive(replica, request, frame, through="drain")
    unresolved_response = response_frame(
        request,
        state="unresolved",
        stage="drain",
        code="acknowledgment_unknown",
    )
    unresolved = replica.record_unresolved(
        request,
        owner_token=OWNER_TOKEN,
        stage="drain",
        reason="acknowledgment_unknown",
        response_frame=unresolved_response,
    )
    assert (
        replica.record_unresolved(
            request,
            owner_token=OWNER_TOKEN,
            stage="drain",
            reason="acknowledgment_unknown",
            response_frame=unresolved_response,
        )
        == unresolved
    )
    assert replica.status(request).state == "unresolved"
    with pytest.raises(ReplicaJournalSyncError):
        complete(replica, request, "drain")

    rejected_request, rejected_frame = signed_request(key, operation_id="c" * 32, sequence=2)
    drive(replica, rejected_request, rejected_frame, through="verify")
    replica.record_rejected(
        rejected_request,
        owner_token=OWNER_TOKEN,
        stage="verify",
        status="verification_rejected",
        receipt=RejectedReceipt(receipt_id="rejected_000000000000000000000"),
        response_frame=response_frame(
            rejected_request,
            state="rejected",
            stage="verify",
            code="verification_rejected",
            receipt_id="rejected_000000000000000000000",
        ),
    )
    assert replica.status(rejected_request).state == "rejected"
    with pytest.raises(ReplicaJournalSyncError):
        begin(replica, rejected_request, "publish")


def test_restart_reports_only_claimed_nonterminal_work(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    key = Ed25519PrivateKey.generate()
    unclaimed, unclaimed_frame = signed_request(key)
    replica.accept(unclaimed, unclaimed_frame, AUTHORITY)
    claimed, claimed_frame = signed_request(key, operation_id="e" * 32, sequence=2)
    drive(replica, claimed, claimed_frame, through="verify")

    reopened = configured_journal(replica.path)
    pending = reopened.incomplete_lifecycles()
    assert len(pending) == 1
    assert pending[0].request_frame == claimed_frame
    assert pending[0].stage == "verify"
    assert pending[0].stage_started is True
    assert pending[0].reason == "broker_restart_unknown"
    assert reopened.status(unclaimed).state == "accepted"


def test_artifact_bytes_never_enter_sqlite(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    drive(replica, request, frame, through="ingest")
    complete(replica, request, "ingest")
    database_bytes = replica.path.read_bytes()
    assert bytes.fromhex(BUNDLE.sha256) not in database_bytes
    assert b"artifact-content-sentinel" not in database_bytes
    receipt = json.loads(bytes(rows(replica.path)[-1][4]))
    assert receipt["bundle"] == {
        "artifact_id": BUNDLE.artifact_id,
        "byte_length": BUNDLE.byte_length,
        "sha256": BUNDLE.sha256,
    }


class FaultJournal(BrokerReplicaJournal):
    def __init__(self, path: Path, *, application_id: str) -> None:
        self.fault: str | None = None
        super().__init__(
            path,
            application_id=application_id,
            expected_limits_sha256=LIMITS_SHA256,
            request_public_key=REQUEST_PRIVATE_KEY.public_key(),
            response_public_keys={BROKER_INCARNATION: RESPONSE_PRIVATE_KEY.public_key()},
        )

    def _commit_transaction(self, database: sqlite3.Connection) -> None:
        if self.fault == "before":
            self.fault = None
            raise sqlite3.OperationalError("commit unavailable")
        if self.fault == "after":
            self.fault = None
            database.commit()
            raise sqlite3.OperationalError("commit response unavailable")
        super()._commit_transaction(database)


def test_commit_unknown_uses_fresh_exact_row_check(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    request, frame = signed_request(key)
    before = FaultJournal(tmp_path / "before.sqlite3", application_id=APPLICATION_ID)
    before.fault = "before"
    with pytest.raises(ReplicaJournalCommitAbsent):
        before.accept(request, frame, AUTHORITY)
    assert before.status(request).state == "absent"

    after = FaultJournal(tmp_path / "after.sqlite3", application_id=APPLICATION_ID)
    after.fault = "after"
    assert after.accept(request, frame, AUTHORITY).state == "accepted"
    assert len(rows(after.path)) == 1
    assert after.accept(request, frame, AUTHORITY).state == "accepted"
    assert len(rows(after.path)) == 1


def test_open_rejects_schema_and_history_corruption(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    drive(replica, request, frame, through="verify")
    with sqlite3.connect(replica.path) as database:
        database.execute("DROP TRIGGER replica_journal_reject_update")
        database.execute(
            "UPDATE replica_journal_events SET stage = 'publish' "
            "WHERE event_type = 'stage_started' AND stage = 'verify'"
        )
        database.commit()
    with pytest.raises(ReplicaJournalSyncError):
        configured_journal(replica.path)


def _schema_statement(prefix: str) -> str:
    return next(
        statement
        for statement in REPLICA_JOURNAL_SCHEMA_STATEMENTS
        if " ".join(statement.split()).startswith(prefix)
    )


def test_open_rejects_identity_frame_and_sequence_corruption(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()

    identity = journal(tmp_path / "identity")
    request, frame = signed_request(key)
    identity.accept(request, frame, AUTHORITY)
    with sqlite3.connect(identity.path) as database:
        database.execute("DROP TRIGGER replica_journal_reject_update")
        database.execute(
            "UPDATE replica_journal_events SET database_incarnation = ?",
            ("z" * 32,),
        )
        database.execute(_schema_statement("CREATE TRIGGER replica_journal_reject_update"))
        database.commit()
    with pytest.raises(ReplicaJournalSyncError):
        configured_journal(identity.path)

    changed_frame = journal(tmp_path / "changed-frame")
    changed_request, changed = signed_request(key, operation_id="7" * 32)
    changed_frame.accept(changed_request, changed, AUTHORITY)
    tampered_value = json.loads(changed)
    signature = tampered_value["signature"]
    tampered_value["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
    tampered = canonical_json(tampered_value)
    with sqlite3.connect(changed_frame.path) as database:
        database.execute("DROP TRIGGER replica_journal_reject_update")
        database.execute(
            "UPDATE replica_journal_events SET request_frame = ?, request_frame_sha256 = ? "
            "WHERE event_type = 'accepted'",
            (tampered, hashlib.sha256(tampered).hexdigest()),
        )
        database.execute(_schema_statement("CREATE TRIGGER replica_journal_reject_update"))
        database.commit()
    with pytest.raises(ReplicaJournalSyncError):
        configured_journal(changed_frame.path)

    sequence = journal(tmp_path / "sequence")
    first, first_frame = signed_request(key, operation_id="8" * 32)
    second, second_frame = signed_request(key, operation_id="9" * 32, sequence=2)
    sequence.accept(first, first_frame, AUTHORITY)
    sequence.accept(second, second_frame, AUTHORITY)
    with sqlite3.connect(sequence.path) as database:
        database.execute("DROP TRIGGER replica_journal_reject_delete")
        database.execute("DELETE FROM replica_journal_events WHERE event_id = 1")
        database.execute(_schema_statement("CREATE TRIGGER replica_journal_reject_delete"))
        database.commit()
    with pytest.raises(ReplicaJournalSyncError):
        configured_journal(sequence.path)


def test_replay_rejects_claimed_broker_different_from_authenticated_request(
    tmp_path: Path,
) -> None:
    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    replica.accept(request, frame, AUTHORITY)
    replica.claim_authority(request, owner_token=OWNER_TOKEN, broker_incarnation=BROKER_INCARNATION)
    with sqlite3.connect(replica.path) as database:
        database.execute("DROP TRIGGER replica_journal_reject_update")
        database.execute(
            "UPDATE replica_journal_events SET owner_broker_incarnation = ? "
            "WHERE event_type = 'authority_claimed'",
            ("c" * 32,),
        )
        database.execute(_schema_statement("CREATE TRIGGER replica_journal_reject_update"))
        database.commit()
    with pytest.raises(ReplicaJournalSyncError, match="owner broker"):
        configured_journal(replica.path)


def test_export_receipt_rejects_duplicate_artifact_ids() -> None:
    with pytest.raises(ValueError, match="must be distinct"):
        ExportReceipt(
            "receipt_export_0000000000000000",
            BUNDLE,
            BUNDLE,
            INDEX_OBJECTS,
        )


def test_replay_rejects_duplicate_export_artifact_ids(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    drive(replica, request, frame, through="export")
    complete(replica, request, "export")
    with sqlite3.connect(replica.path) as database:
        database.execute("DROP TRIGGER replica_journal_reject_update")
        raw = database.execute(
            "SELECT receipt_json FROM replica_journal_events "
            "WHERE event_type = 'stage_outcome' AND stage = 'export'"
        ).fetchone()[0]
        changed = json.loads(raw)
        changed["worktree"] = changed["bundle"]
        database.execute(
            "UPDATE replica_journal_events SET receipt_json = ? "
            "WHERE event_type = 'stage_outcome' AND stage = 'export'",
            (canonical_json(changed),),
        )
        database.execute(_schema_statement("CREATE TRIGGER replica_journal_reject_update"))
        database.commit()
    with pytest.raises(ReplicaJournalSyncError):
        configured_journal(replica.path)


def test_request_payload_rejects_inline_or_unknown_content(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    key = Ed25519PrivateKey.generate()
    payload = request_payload()
    payload["content"] = "artifact-content-sentinel"
    request, frame = signed_request(key, payload=payload)
    with pytest.raises(ValueError):
        replica.accept(request, frame, AUTHORITY)
    assert b"artifact-content-sentinel" not in replica.path.read_bytes()


def test_terminal_response_requires_protocol_binding(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    drive(replica, request, frame, through="drain")
    with pytest.raises(ValueError):
        replica.record_unresolved(
            request,
            owner_token=OWNER_TOKEN,
            stage="drain",
            reason="acknowledgment_unknown",
            response_frame=b'{"nonsense":true}',
        )


def test_terminal_response_matches_disposition_and_blocks_inline_content(
    tmp_path: Path,
) -> None:
    key = Ed25519PrivateKey.generate()

    unresolved_journal = journal(tmp_path / "unresolved")
    unresolved_request, unresolved_frame = signed_request(key)
    drive(unresolved_journal, unresolved_request, unresolved_frame, through="drain")
    successful = response_frame(
        unresolved_request,
        state="completed",
        stage="reclaim",
        receipt_id="reclaim_0000000000000000000000",
    )
    with pytest.raises(ValueError):
        unresolved_journal.record_unresolved(
            unresolved_request,
            owner_token=OWNER_TOKEN,
            stage="drain",
            reason="acknowledgment_unknown",
            response_frame=successful,
        )
    with pytest.raises(ValueError):
        unresolved_journal.record_rejected(
            unresolved_request,
            owner_token=OWNER_TOKEN,
            stage="drain",
            status="drain_rejected",
            receipt=RejectedReceipt(receipt_id="rejected_000000000000000000000"),
            response_frame=successful,
        )

    reclaim_journal = journal(tmp_path / "reclaim")
    reclaim_request, reclaim_frame = signed_request(key, operation_id="6" * 32, sequence=2)
    drive(reclaim_journal, reclaim_request, reclaim_frame, through="reclaim")
    failed = response_frame(
        reclaim_request,
        state="unresolved",
        stage="reclaim",
        code="presence_unknown",
    )
    with pytest.raises(ValueError):
        complete(reclaim_journal, reclaim_request, "reclaim", final_response=failed)

    inline_frames = (
        response_frame(
            unresolved_request,
            state="unresolved",
            stage="drain",
            code="acknowledgment_unknown",
            result_extra={"artifact_bytes": "artifact-content-sentinel"},
        ),
        response_frame(
            unresolved_request,
            state="unresolved",
            stage="drain",
            code="acknowledgment_unknown",
            result_extra={"detail": {"artifact_bytes": "artifact-content-sentinel"}},
        ),
        create_signed_response(
            unresolved_request,
            private_key=RESPONSE_PRIVATE_KEY,
            status="error",
            error="artifact-content-sentinel",
            result={
                "code": "acknowledgment_unknown",
                "stage": "drain",
                "state": "unresolved",
            },
        ),
    )
    for inline in inline_frames:
        with pytest.raises(ValueError):
            unresolved_journal.record_unresolved(
                unresolved_request,
                owner_token=OWNER_TOKEN,
                stage="drain",
                reason="acknowledgment_unknown",
                response_frame=inline,
            )
    assert b"artifact-content-sentinel" not in unresolved_journal.path.read_bytes()


def test_replay_rejects_terminal_response_semantic_corruption(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    drive(replica, request, frame, through="drain")
    valid = response_frame(
        request,
        state="unresolved",
        stage="drain",
        code="acknowledgment_unknown",
    )
    replica.record_unresolved(
        request,
        owner_token=OWNER_TOKEN,
        stage="drain",
        reason="acknowledgment_unknown",
        response_frame=valid,
    )
    corrupted = response_frame(
        request,
        state="unresolved",
        stage="drain",
        code="acknowledgment_unknown",
        result_extra={"artifact_bytes": "artifact-content-sentinel"},
    )
    with sqlite3.connect(replica.path) as database:
        database.execute("DROP TRIGGER replica_journal_reject_update")
        database.execute(
            "UPDATE replica_journal_events SET response_frame = ?, "
            "response_frame_sha256 = ? WHERE event_type = 'stage_unresolved'",
            (corrupted, hashlib.sha256(corrupted).hexdigest()),
        )
        database.execute(_schema_statement("CREATE TRIGGER replica_journal_reject_update"))
        database.commit()
    with pytest.raises(ReplicaJournalSyncError):
        configured_journal(replica.path)


def test_commit_absence_matches_stage_unique_key(tmp_path: Path) -> None:
    replica = FaultJournal(tmp_path / "stage.sqlite3", application_id=APPLICATION_ID)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    drive(replica, request, frame, through="verify")
    replica.fault = "before"
    with pytest.raises(ReplicaJournalCommitAbsent):
        complete(replica, request, "verify")
    assert replica.status(request).stage == "verify"


def test_concurrent_exact_accept_returns_one_durable_decision(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite3"
    first = configured_journal(path)
    second = configured_journal(path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    barrier = threading.Barrier(2)
    decisions: list[ReplicaJournalDecision] = []
    failures: list[BaseException] = []

    def call(replica: BrokerReplicaJournal) -> None:
        try:
            barrier.wait(timeout=2.0)
            decisions.append(replica.accept(request, frame, AUTHORITY))
        except (
            ReplicaJournalSyncError,
            TypeError,
            ValueError,
            threading.BrokenBarrierError,
        ) as exc:
            failures.append(exc)

    threads = [
        threading.Thread(target=call, args=(first,)),
        threading.Thread(target=call, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert failures == []
    assert [decision.state for decision in decisions] == ["accepted", "accepted"]
    assert len(rows(path)) == 1


@pytest.mark.parametrize(
    ("factory", "arguments"),
    [
        (ReplicaAuthority, ("short", 1, AUTHORITY.execution_owner_id)),
        (ReplicaAuthority, (AUTHORITY.physical_target_id, 0, AUTHORITY.execution_owner_id)),
        (ArtifactReference, ("short", "1" * 64, 1)),
        (ArtifactReference, (BUNDLE.artifact_id, "x" * 64, 1)),
        (ArtifactReference, (BUNDLE.artifact_id, "1" * 64, -1)),
    ],
)
def test_model_bounds_are_independent(factory: object, arguments: tuple[object, ...]) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory(*arguments)  # type: ignore[operator]


def test_unresolved_reason_is_stage_specific(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    drive(replica, request, frame, through="drain")
    with pytest.raises(ValueError):
        replica.record_unresolved(
            request,
            owner_token=OWNER_TOKEN,
            stage="drain",
            reason="foreign_reason",
            response_frame=response_frame(
                request,
                state="unresolved",
                stage="drain",
                code="foreign_reason",
            ),
        )


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_fixed_artifact_set_accepts_both_git_object_formats(
    tmp_path: Path,
    object_format: str,
) -> None:
    replica = journal(tmp_path)
    payload = request_payload()
    payload["object_format"] = object_format
    payload["artifact_set_sha256"] = artifact_set_sha256(
        "a" * 32,
        object_format=object_format,
    )
    request, frame = signed_request(Ed25519PrivateKey.generate(), payload=payload)
    assert replica.accept(request, frame, AUTHORITY).state == "accepted"


@pytest.mark.parametrize("object_format", ["SHA256", "md5", "", 256, None])
def test_fixed_artifact_set_rejects_invalid_object_formats(
    tmp_path: Path,
    object_format: JsonValue,
) -> None:
    replica = journal(tmp_path)
    payload = request_payload()
    payload["object_format"] = object_format
    request, frame = signed_request(Ed25519PrivateKey.generate(), payload=payload)
    with pytest.raises(ValueError):
        replica.accept(request, frame, AUTHORITY)
    assert rows(replica.path) == []


def test_fixed_artifact_set_rejects_missing_null_and_extra_fields(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    invalid: list[dict[str, JsonValue]] = []
    for field in request_payload():
        payload = request_payload()
        del payload[field]
        invalid.append(payload)
    for field in ("bundle", "index_objects", "worktree"):
        payload = request_payload()
        payload[field] = None
        invalid.append(payload)
    payload = request_payload()
    payload["content"] = "artifact-content-sentinel"
    invalid.append(payload)
    payload = request_payload()
    bundle = dict(artifact_json(BUNDLE))
    bundle["content"] = "artifact-content-sentinel"
    payload["bundle"] = bundle
    invalid.append(payload)
    for payload in invalid:
        replica = journal(tmp_path / hashlib.sha256(repr(payload).encode()).hexdigest())
        request, frame = signed_request(key, payload=payload)
        with pytest.raises(ValueError):
            replica.accept(request, frame, AUTHORITY)
        assert rows(replica.path) == []


def test_configured_limit_profile_is_required_for_accept_and_replay(
    tmp_path: Path,
) -> None:
    empty = journal(tmp_path / "empty")
    with pytest.raises(ReplicaJournalSyncError, match="configured"):
        BrokerReplicaJournal(
            empty.path,
            application_id=APPLICATION_ID,
            expected_limits_sha256="9" * 64,
            request_public_key=REQUEST_PRIVATE_KEY.public_key(),
            response_public_keys={BROKER_INCARNATION: RESPONSE_PRIVATE_KEY.public_key()},
        )

    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    replica.accept(request, frame, AUTHORITY)
    with pytest.raises(ReplicaJournalSyncError, match="configured"):
        BrokerReplicaJournal(
            replica.path,
            application_id=APPLICATION_ID,
            expected_limits_sha256="9" * 64,
            request_public_key=REQUEST_PRIVATE_KEY.public_key(),
            response_public_keys={BROKER_INCARNATION: RESPONSE_PRIVATE_KEY.public_key()},
        )

    wrong_limits = request_payload()
    wrong_limits["limits_sha256"] = "9" * 64
    wrong_request, wrong_frame = signed_request(
        Ed25519PrivateKey.generate(),
        operation_id="f" * 32,
        payload=wrong_limits,
    )
    with pytest.raises(ReplicaJournalSyncError, match="limit profile"):
        replica.accept(wrong_request, wrong_frame, AUTHORITY)


def test_accept_rejects_unknown_request_and_response_signers(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    foreign_key = Ed25519PrivateKey.generate()
    foreign_frame = create_signed_request(
        private_key=foreign_key,
        protocol_version=BROKER_PROTOCOL_VERSION,
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        connection_sequence=1,
        operation_id="a" * 32,
        request_type="replica.lifecycle",
        nonce="nonce_00000000001",
        payload=request_payload(),
    )
    foreign_request = parse_signed_request(
        foreign_frame,
        public_key=foreign_key.public_key(),
    )
    with pytest.raises(ReplicaJournalSyncError, match="authentication"):
        replica.accept(foreign_request, foreign_frame, AUTHORITY)

    unsupported_broker_frame = create_signed_request(
        private_key=REQUEST_PRIVATE_KEY,
        protocol_version=BROKER_PROTOCOL_VERSION,
        broker_incarnation="c" * 32,
        database_incarnation=DATABASE_INCARNATION,
        connection_sequence=1,
        operation_id="b" * 32,
        request_type="replica.lifecycle",
        nonce="nonce_00000000002",
        payload=request_payload("b" * 32),
    )
    unsupported_request = parse_signed_request(
        unsupported_broker_frame,
        public_key=REQUEST_PRIVATE_KEY.public_key(),
    )
    with pytest.raises(ReplicaJournalSyncError, match="signer"):
        replica.accept(unsupported_request, unsupported_broker_frame, AUTHORITY)


def test_accept_recomputes_artifact_set_and_rejects_duplicate_artifact_ids(
    tmp_path: Path,
) -> None:
    key = Ed25519PrivateKey.generate()
    wrong_digest = request_payload()
    wrong_digest["artifact_set_sha256"] = "f" * 64
    request, frame = signed_request(key, payload=wrong_digest)
    with pytest.raises(ReplicaJournalSyncError, match="artifact set identity"):
        journal(tmp_path / "digest").accept(request, frame, AUTHORITY)

    duplicate = request_payload()
    duplicate["worktree"] = artifact_json(BUNDLE)
    request, frame = signed_request(key, payload=duplicate)
    with pytest.raises(ValueError, match="must be distinct"):
        journal(tmp_path / "duplicate").accept(request, frame, AUTHORITY)


def test_verify_and_publish_bind_authenticated_artifact_set(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    accept_and_claim(replica, request, frame)
    begin(replica, request, "ingest")
    with pytest.raises(ValueError):
        replica.complete_ingest(
            request,
            owner_token=OWNER_TOKEN,
            receipt=IngestReceipt(
                receipt_id="receipt_ingest_000000000000000000",
                bundle=BUNDLE,
                worktree=WORKTREE,
                index_objects=ArtifactReference(
                    artifact_id=INDEX_OBJECTS.artifact_id,
                    sha256="9" * 64,
                    byte_length=INDEX_OBJECTS.byte_length,
                ),
            ),
        )
    complete(replica, request, "ingest")
    begin(replica, request, "verify")
    wrong_verify = receipt_for("verify")
    assert isinstance(wrong_verify, VerifyReceipt)
    with pytest.raises(ValueError):
        replica.complete_verify(
            request,
            owner_token=OWNER_TOKEN,
            receipt=VerifyReceipt(
                receipt_id=wrong_verify.receipt_id,
                bundle_sha256=wrong_verify.bundle_sha256,
                worktree_sha256=wrong_verify.worktree_sha256,
                index_objects_sha256=wrong_verify.index_objects_sha256,
                source_state_sha256=wrong_verify.source_state_sha256,
                reconciliation_fingerprint=wrong_verify.reconciliation_fingerprint,
                artifact_set_sha256="9" * 64,
            ),
        )
    complete(replica, request, "verify")
    begin(replica, request, "publish")
    wrong_publish = receipt_for("publish")
    assert isinstance(wrong_publish, PublishReceipt)
    invalid_publishes = (
        PublishReceipt(
            receipt_id=wrong_publish.receipt_id,
            physical_target_id=wrong_publish.physical_target_id,
            replica_generation=wrong_publish.replica_generation,
            execution_owner_id=wrong_publish.execution_owner_id,
            publication_marker_sha256=wrong_publish.publication_marker_sha256,
            synchronization_receipt_id=wrong_publish.synchronization_receipt_id,
            artifact_set_sha256="9" * 64,
        ),
        PublishReceipt(
            receipt_id=wrong_publish.receipt_id,
            physical_target_id=wrong_publish.physical_target_id,
            replica_generation=wrong_publish.replica_generation,
            execution_owner_id="foreign_execution_owner_0000000000",
            publication_marker_sha256=wrong_publish.publication_marker_sha256,
            synchronization_receipt_id=wrong_publish.synchronization_receipt_id,
            artifact_set_sha256=wrong_publish.artifact_set_sha256,
        ),
    )
    for invalid in invalid_publishes:
        with pytest.raises(ValueError):
            replica.complete_publish(
                request,
                owner_token=OWNER_TOKEN,
                receipt=invalid,
            )


def test_replay_authenticates_terminal_response_signature(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    drive(replica, request, frame, through="reclaim")
    complete(
        replica,
        request,
        "reclaim",
        final_response=response_frame(
            request,
            state="completed",
            stage="reclaim",
            receipt_id=receipt_for("reclaim").reclaim_receipt_id,
        ),
    )
    terminal = json.loads(bytes(rows(replica.path)[-1][5]))
    signature = terminal["signature"]
    terminal["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
    changed = canonical_json(terminal)
    with sqlite3.connect(replica.path) as database:
        database.execute("DROP TRIGGER replica_journal_reject_update")
        database.execute(
            "UPDATE replica_journal_events SET response_frame = ?, "
            "response_frame_sha256 = ? WHERE event_type = 'stage_outcome' "
            "AND stage = 'reclaim'",
            (changed, hashlib.sha256(changed).hexdigest()),
        )
        database.execute(_schema_statement("CREATE TRIGGER replica_journal_reject_update"))
        database.commit()
    with pytest.raises(ReplicaJournalSyncError, match="validation failed"):
        configured_journal(replica.path)


def test_replay_rejects_legacy_optional_artifact_request(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    replica = journal(tmp_path)
    request, frame = signed_request(key)
    replica.accept(request, frame, AUTHORITY)
    legacy = request_payload()
    del legacy["artifact_set_sha256"]
    del legacy["index_objects"]
    del legacy["object_format"]
    del legacy["limits_sha256"]
    legacy["worktree"] = None
    _legacy_request, legacy_frame = signed_request(key, payload=legacy)
    with sqlite3.connect(replica.path) as database:
        database.execute("DROP TRIGGER replica_journal_reject_update")
        database.execute(
            "UPDATE replica_journal_events "
            "SET request_frame = ?, request_frame_sha256 = ?, request_payload_digest = ?",
            (
                legacy_frame,
                hashlib.sha256(legacy_frame).hexdigest(),
                _legacy_request.payload_digest,
            ),
        )
        database.execute(_schema_statement("CREATE TRIGGER replica_journal_reject_update"))
        database.commit()
    with pytest.raises(ReplicaJournalSyncError):
        configured_journal(replica.path)


@pytest.mark.parametrize(
    ("stage", "through", "removed"),
    [
        ("verify", "publish", ("artifact_set_sha256", "index_objects_sha256")),
        ("publish", "admission", ("artifact_set_sha256", "execution_owner_id")),
        ("export", "reclaim", ("index_objects",)),
    ],
)
def test_replay_rejects_legacy_stage_receipts(
    tmp_path: Path,
    stage: str,
    through: str,
    removed: tuple[str, ...],
) -> None:
    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    drive(replica, request, frame, through=through)
    value = asdict(receipt_for(stage))
    for field in removed:
        del value[field]
    legacy_receipt = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    with sqlite3.connect(replica.path) as database:
        database.execute("DROP TRIGGER replica_journal_reject_update")
        database.execute(
            "UPDATE replica_journal_events SET receipt_json = ? "
            "WHERE event_type = 'stage_outcome' AND stage = ?",
            (legacy_receipt, stage),
        )
        database.execute(_schema_statement("CREATE TRIGGER replica_journal_reject_update"))
        database.commit()
    with pytest.raises(ReplicaJournalSyncError):
        configured_journal(replica.path)


def test_replay_rejects_legacy_optional_ingest_receipt(tmp_path: Path) -> None:
    replica = journal(tmp_path)
    request, frame = signed_request(Ed25519PrivateKey.generate())
    drive(replica, request, frame, through="verify")
    legacy_receipt = json.dumps(
        {
            "bundle": artifact_json(BUNDLE),
            "receipt_id": "receipt_ingest_000000000000000000",
            "worktree": None,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    with sqlite3.connect(replica.path) as database:
        database.execute("DROP TRIGGER replica_journal_reject_update")
        database.execute(
            "UPDATE replica_journal_events SET receipt_json = ? "
            "WHERE event_type = 'stage_outcome' AND stage = 'ingest'",
            (legacy_receipt,),
        )
        database.execute(_schema_statement("CREATE TRIGGER replica_journal_reject_update"))
        database.commit()
    with pytest.raises(ReplicaJournalSyncError):
        configured_journal(replica.path)
