"""Check replica journal V2 pause, continuation, replay, and legacy safety."""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from yinshi.services import broker_replica_journal_v2 as replica_journal_v2
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
    AdmissionReceipt,
    ArtifactReference,
    DrainReceipt,
    ExportReceipt,
    IngestReceipt,
    PublishReceipt,
    ReclaimReceipt,
    RejectedReceipt,
    ReplicaAuthority,
    VerifyReceipt,
)
from yinshi.services.broker_replica_journal_v2 import (
    LEGACY_REPLICA_JOURNAL_V1_SCHEMA_STATEMENTS,
    REPLICA_JOURNAL_V2_SCHEMA_STATEMENTS,
    BrokerReplicaJournalV2,
    LegacyReplicaJournalStateError,
    ReplicaDrainContinuation,
    ReplicaJournalV2CommitAbsent,
    ReplicaJournalV2ConflictError,
    ReplicaJournalV2Error,
    ReplicaJournalV2SyncError,
    inspect_legacy_replica_v1_journal,
    replica_drain_continuation_from_request,
)
from yinshi.services.replica_artifact_contract import (
    compute_replica_artifact_set_sha256,
)

APPLICATION_ID = "yinshi-desktop"
DATABASE_INCARNATION = "d" * 32
BROKER_INCARNATION = "b" * 32
FOREIGN_INCARNATION = "c" * 32
OWNER_TOKEN = "owner_token_00000000000000000000"
LIMITS_SHA256 = "8" * 64
REQUEST_KEY = Ed25519PrivateKey.generate()
RESPONSE_KEY = Ed25519PrivateKey.generate()
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
EXPORTED_BUNDLE = ArtifactReference(
    artifact_id="export_bundle_0000000000000000000000",
    sha256="a" * 64,
    byte_length=321,
)
EXPORTED_WORKTREE = ArtifactReference(
    artifact_id="export_worktree_000000000000000000",
    sha256="b" * 64,
    byte_length=654,
)
EXPORTED_INDEX_OBJECTS = ArtifactReference(
    artifact_id="export_index_00000000000000000000000",
    sha256="c" * 64,
    byte_length=987,
)
EXPECTED_SCHEMA_SQL_SHA256 = {
    ("index", "replica_journal_v2_one_acceptance"): (
        "49add2b183474fda09e7d9f5b63b264f77233e8337da91fe6cbaa55bacf01116"
    ),
    ("index", "replica_journal_v2_one_claim"): (
        "df543906b641941996523b70ad9a6aaa9816903e33d0a21a27eaec0c924bb226"
    ),
    ("index", "replica_journal_v2_one_drain_continuation"): (
        "3d239fbecebc8ccd7252e613edd7d68fb88135ef43e468718153d0f6ae3a0e83"
    ),
    ("index", "replica_journal_v2_one_pause"): (
        "de0110c7a78b20f91e809e5430476b003108cbc398e79cd9c4ff5f68179ea5e9"
    ),
    ("index", "replica_journal_v2_one_stage_completion"): (
        "67e3755828b1b50fef82d15a539feb03ba662a6e8aa94d65adc905bf5f33d084"
    ),
    ("index", "replica_journal_v2_one_stage_start"): (
        "9034151d6cc13e8ab563374ce44c563edded7984b6264414b61a3ae106f2275b"
    ),
    ("index", "replica_journal_v2_one_terminal"): (
        "175edb51bfd626946928827249905e5f1881e977a938c46422317a612824724d"
    ),
    ("table", "replica_journal_v2_events"): (
        "c1330882031fe8a31de3a57dd1c681ab241d44cf28e994c10b9b32bef8a2b9ea"
    ),
    ("table", "replica_journal_v2_meta"): (
        "a010c7aeb69af77de0dcc45e6efb7a0c91868975d8bec19fa04d96f960f1d486"
    ),
    ("trigger", "replica_journal_v2_meta_reject_delete"): (
        "71a4d1ac0710ea2f7b80316bdc420a21131924cbfb12dad436323d5befc02911"
    ),
    ("trigger", "replica_journal_v2_meta_reject_update"): (
        "827fa5011058f78426573824b58ddd6b73e0a8b92f9bffca6cbceb0d7c48d5ec"
    ),
    ("trigger", "replica_journal_v2_reject_delete"): (
        "98cd67677d3d9983fb8f3bdca5be7f11d99eb3a7a390cc3c2945dde6dff468c9"
    ),
    ("trigger", "replica_journal_v2_reject_update"): (
        "4395dd0daf38ce515d78e6625746b03aed20562bdc0568820d986024c0e64dbf"
    ),
    ("trigger", "replica_journal_v2_require_transition"): (
        "1e18e0e5feab84d8af991ecf9fb717f308cd3b2f6ee179a4f10c765a84ac08fd"
    ),
}


def artifact_json(reference: ArtifactReference) -> dict[str, JsonValue]:
    return {
        "artifact_id": reference.artifact_id,
        "byte_length": reference.byte_length,
        "sha256": reference.sha256,
    }


def artifact_binding(role: str, media_type: str, reference: ArtifactReference) -> dict[str, object]:
    return {"role": role, "media_type": media_type, **artifact_json(reference)}


def artifact_set_sha256(operation_id: str) -> str:
    return compute_replica_artifact_set_sha256(
        operation_id=operation_id,
        repository_id="repository_0000000000000000000000",
        workspace_id="workspace_000000000000000000000000",
        physical_target_id=AUTHORITY.physical_target_id,
        replica_generation=AUTHORITY.replica_generation,
        execution_owner_id=AUTHORITY.execution_owner_id,
        object_format="sha256",
        source_state_sha256="4" * 64,
        reconciliation_fingerprint="3" * 64,
        bundle=artifact_binding("committed_bundle", "application/vnd.yinshi.git-bundle.v1", BUNDLE),
        worktree=artifact_binding(
            "worktree", "application/vnd.yinshi.replica-worktree.v1", WORKTREE
        ),
        index_objects=artifact_binding(
            "index_objects",
            "application/vnd.yinshi.git-index-objects-pack.v1",
            INDEX_OBJECTS,
        ),
        limits_sha256=LIMITS_SHA256,
    )


def initial_payload(operation_id: str) -> dict[str, JsonValue]:
    return {
        "artifact_set_sha256": artifact_set_sha256(operation_id),
        "authority": {
            "execution_owner_id": AUTHORITY.execution_owner_id,
            "physical_target_id": AUTHORITY.physical_target_id,
            "replica_generation": AUTHORITY.replica_generation,
        },
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
    *,
    operation_id: str = "a" * 32,
    sequence: int = 1,
    broker_incarnation: str = BROKER_INCARNATION,
    request_type: str = "replica.lifecycle",
    payload: dict[str, JsonValue] | None = None,
) -> tuple[BrokerRequest, bytes]:
    frame = create_signed_request(
        private_key=REQUEST_KEY,
        protocol_version=BROKER_PROTOCOL_VERSION,
        broker_incarnation=broker_incarnation,
        database_incarnation=DATABASE_INCARNATION,
        connection_sequence=sequence,
        operation_id=operation_id,
        request_type=request_type,
        nonce=f"nonce_{sequence:011d}",
        payload=initial_payload(operation_id) if payload is None else payload,
    )
    return parse_signed_request(frame, public_key=REQUEST_KEY.public_key()), frame


def response_frame(
    request: BrokerRequest,
    *,
    state: str,
    stage: str,
    code: str | None = None,
    receipt_id: str | None = None,
    key: Ed25519PrivateKey = RESPONSE_KEY,
) -> bytes:
    result: dict[str, JsonValue] = {"stage": stage, "state": state}
    if code is not None:
        result["code"] = code
    if receipt_id is not None:
        result["receipt_id"] = receipt_id
    return create_signed_response(
        request,
        private_key=key,
        status="ok" if state in {"active", "completed"} else "error",
        error=None if state in {"active", "completed"} else code,
        result=result,
    )


def configured(path: Path) -> BrokerReplicaJournalV2:
    return BrokerReplicaJournalV2(
        path,
        application_id=APPLICATION_ID,
        expected_limits_sha256=LIMITS_SHA256,
        request_public_key=REQUEST_KEY.public_key(),
        response_public_key=RESPONSE_KEY.public_key(),
    )


def receipt_for(stage: str, operation_id: str = "a" * 32) -> object:
    receipt_id = f"receipt_{stage}_000000000000000000"
    if stage == "ingest":
        return IngestReceipt(receipt_id, BUNDLE, WORKTREE, INDEX_OBJECTS)
    if stage == "verify":
        return VerifyReceipt(
            receipt_id,
            BUNDLE.sha256,
            WORKTREE.sha256,
            INDEX_OBJECTS.sha256,
            "4" * 64,
            "3" * 64,
            artifact_set_sha256(operation_id),
        )
    if stage == "publish":
        return PublishReceipt(
            receipt_id,
            AUTHORITY.physical_target_id,
            AUTHORITY.replica_generation,
            AUTHORITY.execution_owner_id,
            "5" * 64,
            "sync_000000000000000000000000000",
            artifact_set_sha256(operation_id),
        )
    if stage == "admission":
        return AdmissionReceipt(
            receipt_id,
            AUTHORITY.execution_owner_id,
            "runtime_0000000000000000000000000",
            "6" * 64,
        )
    if stage == "drain":
        return DrainReceipt(
            "drain_ack_0000000000000000000000",
            "quiescence_00000000000000000000",
        )
    if stage == "export":
        return ExportReceipt(
            "export_000000000000000000000000",
            EXPORTED_BUNDLE,
            EXPORTED_WORKTREE,
            EXPORTED_INDEX_OBJECTS,
        )
    if stage == "reclaim":
        return ReclaimReceipt("reclaim_0000000000000000000000")
    raise AssertionError(stage)


def event_rows(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as database:
        return database.execute(
            "SELECT event_type, stage, request_frame, receipt_json, response_frame "
            "FROM replica_journal_v2_events ORDER BY event_id"
        ).fetchall()


def _assert_file_unchanged(
    path: Path,
    expected_content: bytes,
    expected_stat: os.stat_result,
) -> None:
    actual_stat = path.stat()
    assert stat.S_IMODE(actual_stat.st_mode) == stat.S_IMODE(expected_stat.st_mode)
    assert actual_stat.st_atime_ns == expected_stat.st_atime_ns
    assert actual_stat.st_mtime_ns == expected_stat.st_mtime_ns
    assert path.read_bytes() == expected_content


def _restore_update_trigger(database: sqlite3.Connection) -> None:
    database.execute(
        next(
            statement
            for statement in REPLICA_JOURNAL_V2_SCHEMA_STATEMENTS
            if "CREATE TRIGGER replica_journal_v2_reject_update" in statement
        )
    )


def _corrupt_event_field(
    path: Path,
    *,
    event_type: str,
    column: str,
    value: object,
    stage: str | None = None,
) -> None:
    with sqlite3.connect(path) as database:
        database.execute("DROP TRIGGER replica_journal_v2_reject_update")
        database.execute(
            f"UPDATE replica_journal_v2_events SET {column} = ? "
            "WHERE event_type = ? AND (? IS NULL OR stage = ?)",
            (value, event_type, stage, stage),
        )
        _restore_update_trigger(database)


def accept_and_claim(journal: BrokerReplicaJournalV2, request: BrokerRequest, frame: bytes) -> None:
    journal.accept_initial(request, frame, AUTHORITY)
    journal.claim_authority(
        request,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )


def complete_initial_through_pause(
    journal: BrokerReplicaJournalV2, request: BrokerRequest, frame: bytes
) -> bytes:
    accept_and_claim(journal, request, frame)
    for stage in ("ingest", "verify", "publish"):
        start = journal.begin_stage(request, owner_token=OWNER_TOKEN, stage=stage)
        assert journal.begin_stage(request, owner_token=OWNER_TOKEN, stage=stage) == start
        position = journal.complete_stage(
            request,
            owner_token=OWNER_TOKEN,
            stage=stage,
            receipt=receipt_for(stage, request.operation_id),
        )
        assert (
            journal.complete_stage(
                request,
                owner_token=OWNER_TOKEN,
                stage=stage,
                receipt=receipt_for(stage, request.operation_id),
            )
            == position
        )
    journal.begin_stage(request, owner_token=OWNER_TOKEN, stage="admission")
    admission = receipt_for("admission", request.operation_id)
    assert isinstance(admission, AdmissionReceipt)
    active = response_frame(
        request,
        state="active",
        stage="admission",
        receipt_id=admission.receipt_id,
    )
    pause = journal.pause_after_admission(
        request,
        owner_token=OWNER_TOKEN,
        receipt=admission,
        response_frame=active,
    )
    assert (
        journal.pause_after_admission(
            request,
            owner_token=OWNER_TOKEN,
            receipt=admission,
            response_frame=active,
        )
        == pause
    )
    return active


def drain_request(
    initial_request: BrokerRequest,
    initial_frame: bytes,
    *,
    broker_incarnation: str = BROKER_INCARNATION,
    sequence: int = 2,
    payload_changes: dict[str, JsonValue] | None = None,
) -> tuple[BrokerRequest, bytes, ReplicaDrainContinuation]:
    admission = receipt_for("admission", initial_request.operation_id)
    assert isinstance(admission, AdmissionReceipt)
    payload: dict[str, JsonValue] = {
        "admission_receipt_id": admission.receipt_id,
        "application_drain_intent_receipt_id": "drain_intent_0000000000000000000",
        "authority": {
            "execution_owner_id": AUTHORITY.execution_owner_id,
            "physical_target_id": AUTHORITY.physical_target_id,
            "replica_generation": AUTHORITY.replica_generation,
        },
        "initial_request_sha256": hashlib.sha256(initial_frame).hexdigest(),
    }
    if payload_changes:
        payload.update(payload_changes)
    request, frame = signed_request(
        operation_id=initial_request.operation_id,
        sequence=sequence,
        broker_incarnation=broker_incarnation,
        request_type="replica.drain",
        payload=payload,
    )
    return request, frame, replica_drain_continuation_from_request(request)


def complete_through_export(
    journal: BrokerReplicaJournalV2,
    initial: BrokerRequest,
    initial_frame: bytes,
    export_receipt: ExportReceipt,
) -> BrokerRequest:
    complete_initial_through_pause(journal, initial, initial_frame)
    drain, drain_frame, continuation = drain_request(initial, initial_frame)
    journal.accept_drain_continuation(drain, drain_frame, continuation)
    journal.begin_stage(drain, owner_token=OWNER_TOKEN, stage="drain")
    journal.complete_stage(
        drain,
        owner_token=OWNER_TOKEN,
        stage="drain",
        receipt=receipt_for("drain"),
    )
    journal.begin_stage(drain, owner_token=OWNER_TOKEN, stage="export")
    journal.complete_stage(
        drain,
        owner_token=OWNER_TOKEN,
        stage="export",
        receipt=export_receipt,
    )
    return drain


def test_schema_identity_config_and_append_only_guards(tmp_path: Path) -> None:
    journal = configured(tmp_path / "v2.sqlite3")
    with sqlite3.connect(journal.path) as database:
        objects = database.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        actual_schema_hashes = {
            (str(kind), str(name)): hashlib.sha256(
                " ".join(str(sql).split()).rstrip(";").encode("ascii")
            ).hexdigest()
            for kind, name, sql in objects
        }
        assert actual_schema_hashes == EXPECTED_SCHEMA_SQL_SHA256
        meta = database.execute(
            "SELECT application_id, schema_version, length(journal_id) FROM replica_journal_v2_meta"
        ).fetchone()
        assert meta == (APPLICATION_ID, 2, 97)
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("UPDATE replica_journal_v2_meta SET schema_version = 3")

    journal.require_response_signer(RESPONSE_KEY)
    with pytest.raises(ReplicaJournalV2SyncError):
        journal.require_response_signer(Ed25519PrivateKey.generate())
    with pytest.raises(ReplicaJournalV2SyncError, match="configured"):
        BrokerReplicaJournalV2(
            journal.path,
            application_id=APPLICATION_ID,
            expected_limits_sha256=LIMITS_SHA256,
            request_public_key=REQUEST_KEY.public_key(),
            response_public_key=Ed25519PrivateKey.generate().public_key(),
        )


def test_initial_pause_continuation_and_completion_are_exactly_replayable(
    tmp_path: Path,
) -> None:
    journal = configured(tmp_path / "v2.sqlite3")
    initial, initial_frame = signed_request()
    assert journal.initial_status(initial).state == "absent"
    active_frame = complete_initial_through_pause(journal, initial, initial_frame)
    assert journal.accept_initial(initial, initial_frame, AUTHORITY).response_frame == active_frame
    assert journal.initial_status(initial).state == "active"
    assert journal.completed_receipts(initial).admission == receipt_for("admission")

    drain, drain_frame, continuation = drain_request(initial, initial_frame)
    assert journal.drain_status(drain).state == "absent"
    accepted = journal.accept_drain_continuation(drain, drain_frame, continuation)
    assert accepted.state == "claimed"
    assert journal.accept_drain_continuation(drain, drain_frame, continuation) == accepted

    for stage in ("drain", "export", "reclaim"):
        start = journal.begin_stage(drain, owner_token=OWNER_TOKEN, stage=stage)
        assert journal.begin_stage(drain, owner_token=OWNER_TOKEN, stage=stage) == start
        receipt = receipt_for(stage)
        terminal = None
        if isinstance(receipt, ReclaimReceipt):
            terminal = response_frame(
                drain,
                state="completed",
                stage="reclaim",
                receipt_id=receipt.reclaim_receipt_id,
            )
        completed = journal.complete_stage(
            drain,
            owner_token=OWNER_TOKEN,
            stage=stage,
            receipt=receipt,
            response_frame=terminal,
        )
        assert (
            journal.complete_stage(
                drain,
                owner_token=OWNER_TOKEN,
                stage=stage,
                receipt=receipt,
                response_frame=terminal,
            )
            == completed
        )
    assert journal.initial_status(initial).state == "active"
    assert journal.drain_status(drain).state == "completed"
    assert journal.completed_receipts(drain).export == receipt_for("export")
    assert journal.completed_receipts(drain).reclaim == receipt_for("reclaim")
    reopened = configured(journal.path)
    assert reopened.completed_receipts(drain).export == receipt_for("export")
    assert reopened.drain_status(drain).state == "completed"
    rows = event_rows(journal.path)
    assert len(rows) == 18
    assert [row[0] for row in rows].count("lifecycle_paused") == 1
    assert [row[0] for row in rows].count("drain_continuation_accepted") == 1


def test_export_persists_new_role_bound_references_and_rejects_malformed_replay(
    tmp_path: Path,
) -> None:
    journal = configured(tmp_path / "export.sqlite3")
    initial, initial_frame = signed_request()
    export_receipt = receipt_for("export")
    assert isinstance(export_receipt, ExportReceipt)
    assert export_receipt.bundle != BUNDLE
    drain = complete_through_export(
        journal,
        initial,
        initial_frame,
        export_receipt,
    )
    assert configured(journal.path).completed_receipts(drain).export == export_receipt

    valid_receipt = {
        "bundle": artifact_json(EXPORTED_BUNDLE),
        "export_receipt_id": export_receipt.export_receipt_id,
        "index_objects": artifact_json(EXPORTED_INDEX_OBJECTS),
        "worktree": artifact_json(EXPORTED_WORKTREE),
    }
    missing_role = dict(valid_receipt)
    del missing_role["bundle"]
    duplicate_identity = dict(valid_receipt)
    duplicate_identity["worktree"] = artifact_json(EXPORTED_BUNDLE)
    malformed_metadata = dict(valid_receipt)
    malformed_metadata["index_objects"] = {
        **artifact_json(EXPORTED_INDEX_OBJECTS),
        "byte_length": -1,
    }
    malformed_receipts = (
        canonical_json(missing_role),
        canonical_json(duplicate_identity),
        canonical_json(malformed_metadata),
        b" " + canonical_json(valid_receipt),
    )
    for index, malformed in enumerate(malformed_receipts):
        candidate = tmp_path / f"malformed-export-{index}.sqlite3"
        shutil.copy2(journal.path, candidate)
        _corrupt_event_field(
            candidate,
            event_type="stage_outcome",
            column="receipt_json",
            value=malformed,
            stage="export",
        )
        with pytest.raises(ReplicaJournalV2SyncError):
            configured(candidate)


def test_continuation_requires_pause_binding_and_same_lifecycle_authority(
    tmp_path: Path,
) -> None:
    journal = configured(tmp_path / "v2.sqlite3")
    initial, initial_frame = signed_request()
    accept_and_claim(journal, initial, initial_frame)
    drain, drain_frame, continuation = drain_request(initial, initial_frame)
    with pytest.raises(ReplicaJournalV2ConflictError, match="pause"):
        journal.accept_drain_continuation(drain, drain_frame, continuation)
    with pytest.raises(ReplicaJournalV2ConflictError, match="continuation"):
        journal.begin_stage(drain, owner_token=OWNER_TOKEN, stage="drain")

    complete_initial_through_pause(configured(tmp_path / "paused.sqlite3"), initial, initial_frame)
    paused = configured(tmp_path / "paused.sqlite3")
    foreign, foreign_frame, foreign_continuation = drain_request(
        initial, initial_frame, broker_incarnation=FOREIGN_INCARNATION
    )
    before = len(event_rows(paused.path))
    with pytest.raises(ReplicaJournalV2ConflictError, match="durable pause"):
        paused.accept_drain_continuation(foreign, foreign_frame, foreign_continuation)
    assert len(event_rows(paused.path)) == before


def test_continuation_payload_is_exact_and_conflicts_precede_writes(tmp_path: Path) -> None:
    journal = configured(tmp_path / "v2.sqlite3")
    initial, initial_frame = signed_request()
    complete_initial_through_pause(journal, initial, initial_frame)
    invalid_payloads: list[dict[str, JsonValue]] = [
        {"extra": "forbidden"},
        {"initial_request_sha256": "9" * 64},
        {"admission_receipt_id": "other_receipt_000000000000000000"},
    ]
    before = len(event_rows(journal.path))
    for index, changes in enumerate(invalid_payloads, start=2):
        if "extra" in changes:
            with pytest.raises(ValueError, match="fields"):
                drain_request(initial, initial_frame, sequence=index, payload_changes=changes)
            continue
        request, frame, continuation = drain_request(
            initial, initial_frame, sequence=index, payload_changes=changes
        )
        with pytest.raises(ReplicaJournalV2ConflictError):
            journal.accept_drain_continuation(request, frame, continuation)
    assert len(event_rows(journal.path)) == before


def test_pause_is_atomic_and_drain_start_requires_durable_continuation(tmp_path: Path) -> None:
    initial, frame = signed_request()
    journal = configured(tmp_path / "v2.sqlite3")
    accept_and_claim(journal, initial, frame)
    for stage in ("ingest", "verify", "publish"):
        journal.begin_stage(initial, owner_token=OWNER_TOKEN, stage=stage)
        journal.complete_stage(
            initial,
            owner_token=OWNER_TOKEN,
            stage=stage,
            receipt=receipt_for(stage),
        )
    journal.begin_stage(initial, owner_token=OWNER_TOKEN, stage="admission")
    admission = receipt_for("admission")
    assert isinstance(admission, AdmissionReceipt)
    active = response_frame(
        initial, state="active", stage="admission", receipt_id=admission.receipt_id
    )
    with pytest.raises(ReplicaJournalV2ConflictError):
        journal.complete_stage(
            initial,
            owner_token=OWNER_TOKEN,
            stage="admission",
            receipt=admission,
        )
    journal.pause_after_admission(
        initial,
        owner_token=OWNER_TOKEN,
        receipt=admission,
        response_frame=active,
    )
    rows = event_rows(journal.path)
    assert rows[-2][0:2] == ("stage_outcome", "admission")
    assert rows[-1][0] == "lifecycle_paused"


def test_foreign_incarnation_pause_is_exposed_then_can_fail_closed(tmp_path: Path) -> None:
    journal = configured(tmp_path / "v2.sqlite3")
    initial, frame = signed_request()
    complete_initial_through_pause(journal, initial, frame)
    pending = journal.incomplete_lifecycles()
    assert len(pending) == 1
    assert pending[0].paused is True
    assert pending[0].owner_broker_incarnation == BROKER_INCARNATION
    assert journal.authenticated_initial_request(pending[0]) == initial
    unresolved = response_frame(
        initial,
        state="unresolved",
        stage="drain",
        code="broker_restart_unknown",
    )
    position = journal.mark_stage_unresolved(
        initial,
        owner_token=OWNER_TOKEN,
        stage="drain",
        reason="broker_restart_unknown",
        response_frame=unresolved,
    )
    assert (
        journal.mark_stage_unresolved(
            initial,
            owner_token=OWNER_TOKEN,
            stage="drain",
            reason="broker_restart_unknown",
            response_frame=unresolved,
        )
        == position
    )
    assert journal.initial_status(initial).state == "active"
    assert journal.incomplete_lifecycles() == ()

    continued = configured(tmp_path / "continued.sqlite3")
    complete_initial_through_pause(continued, initial, frame)
    drain, drain_frame, continuation = drain_request(initial, frame)
    continued.accept_drain_continuation(drain, drain_frame, continuation)
    continued_response = response_frame(
        drain,
        state="unresolved",
        stage="drain",
        code="broker_restart_unknown",
    )
    continued.mark_stage_unresolved(
        drain,
        owner_token=OWNER_TOKEN,
        stage="drain",
        reason="broker_restart_unknown",
        response_frame=continued_response,
    )
    assert continued.drain_status(drain).state == "unresolved"
    assert (
        continued.initial_status(initial).response_frame
        == continued.active_lifecycle(initial).response_frame
    )


def test_rejections_and_unresolved_are_terminal_and_request_bound(tmp_path: Path) -> None:
    journal = configured(tmp_path / "v2.sqlite3")
    initial, frame = signed_request()
    accept_and_claim(journal, initial, frame)
    journal.begin_stage(initial, owner_token=OWNER_TOKEN, stage="ingest")
    receipt = RejectedReceipt("rejected_000000000000000000000")
    response = response_frame(
        initial,
        state="rejected",
        stage="ingest",
        code="artifact_rejected",
        receipt_id=receipt.receipt_id,
    )
    journal.reject_stage(
        initial,
        owner_token=OWNER_TOKEN,
        stage="ingest",
        status="artifact_rejected",
        receipt=receipt,
        response_frame=response,
    )
    assert journal.initial_status(initial).state == "rejected"
    with pytest.raises(ReplicaJournalV2ConflictError, match="terminal"):
        journal.begin_stage(initial, owner_token=OWNER_TOKEN, stage="verify")
    bad_key_response = response_frame(
        initial,
        state="rejected",
        stage="ingest",
        code="artifact_rejected",
        receipt_id=receipt.receipt_id,
        key=Ed25519PrivateKey.generate(),
    )
    with pytest.raises(ValueError, match="authentication"):
        journal.reject_stage(
            initial,
            owner_token=OWNER_TOKEN,
            stage="ingest",
            status="artifact_rejected",
            receipt=receipt,
            response_frame=bad_key_response,
        )


def _race_two(calls: tuple[Callable[[], object], Callable[[], object]]) -> list[object]:
    barrier = threading.Barrier(2)

    def run(call: Callable[[], object]) -> object:
        barrier.wait(timeout=5.0)
        try:
            return call()
        except ReplicaJournalV2Error as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, call) for call in calls]
        return [future.result(timeout=15.0) for future in futures]


def _rejection_call(
    journal: BrokerReplicaJournalV2,
    request: BrokerRequest,
    receipt: RejectedReceipt,
    response: bytes,
) -> Callable[[], object]:
    def call() -> object:
        return journal.reject_stage(
            request,
            owner_token=OWNER_TOKEN,
            stage="ingest",
            status="artifact_rejected",
            receipt=receipt,
            response_frame=response,
        )

    return call


def _unresolved_call(
    journal: BrokerReplicaJournalV2,
    request: BrokerRequest,
    response: bytes,
) -> Callable[[], object]:
    def call() -> object:
        return journal.mark_stage_unresolved(
            request,
            owner_token=OWNER_TOKEN,
            stage="ingest",
            reason="timeout_unknown",
            response_frame=response,
        )

    return call


def test_barrier_races_converge_on_single_durable_transition(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite3"
    first = configured(path)
    second = configured(path)
    initial, initial_frame = signed_request()

    accepted = _race_two(
        (
            lambda: first.accept_initial(initial, initial_frame, AUTHORITY),
            lambda: second.accept_initial(initial, initial_frame, AUTHORITY),
        )
    )
    assert not any(isinstance(result, Exception) for result in accepted)
    assert len(event_rows(path)) == 1

    claimed = _race_two(
        (
            lambda: first.claim_authority(
                initial,
                owner_token=OWNER_TOKEN,
                broker_incarnation=BROKER_INCARNATION,
            ),
            lambda: second.claim_authority(
                initial,
                owner_token=OWNER_TOKEN,
                broker_incarnation=BROKER_INCARNATION,
            ),
        )
    )
    assert not any(isinstance(result, Exception) for result in claimed)
    starts = _race_two(
        (
            lambda: first.begin_stage(initial, owner_token=OWNER_TOKEN, stage="ingest"),
            lambda: second.begin_stage(initial, owner_token=OWNER_TOKEN, stage="ingest"),
        )
    )
    assert starts[0] == starts[1]

    completed = _race_two(
        (
            lambda: first.complete_stage(
                initial,
                owner_token=OWNER_TOKEN,
                stage="ingest",
                receipt=receipt_for("ingest"),
            ),
            lambda: second.complete_stage(
                initial,
                owner_token=OWNER_TOKEN,
                stage="ingest",
                receipt=receipt_for("ingest"),
            ),
        )
    )
    assert completed[0] == completed[1]
    for stage in ("verify", "publish"):
        first.begin_stage(initial, owner_token=OWNER_TOKEN, stage=stage)
        first.complete_stage(
            initial,
            owner_token=OWNER_TOKEN,
            stage=stage,
            receipt=receipt_for(stage),
        )
    first.begin_stage(initial, owner_token=OWNER_TOKEN, stage="admission")
    admission = receipt_for("admission")
    assert isinstance(admission, AdmissionReceipt)
    active = response_frame(
        initial,
        state="active",
        stage="admission",
        receipt_id=admission.receipt_id,
    )
    paused = _race_two(
        (
            lambda: first.pause_after_admission(
                initial,
                owner_token=OWNER_TOKEN,
                receipt=admission,
                response_frame=active,
            ),
            lambda: second.pause_after_admission(
                initial,
                owner_token=OWNER_TOKEN,
                receipt=admission,
                response_frame=active,
            ),
        )
    )
    assert paused[0] == paused[1]
    drain, drain_frame, continuation = drain_request(initial, initial_frame)
    continued = _race_two(
        (
            lambda: first.accept_drain_continuation(drain, drain_frame, continuation),
            lambda: second.accept_drain_continuation(drain, drain_frame, continuation),
        )
    )
    assert continued[0] == continued[1]
    drain_starts = _race_two(
        (
            lambda: first.begin_stage(drain, owner_token=OWNER_TOKEN, stage="drain"),
            lambda: second.begin_stage(drain, owner_token=OWNER_TOKEN, stage="drain"),
        )
    )
    assert drain_starts[0] == drain_starts[1]

    rejected_receipt = RejectedReceipt("race_rejected_00000000000000000")
    rejected_response = response_frame(
        drain,
        state="rejected",
        stage="drain",
        code="drain_rejected",
        receipt_id=rejected_receipt.receipt_id,
    )
    unresolved_response = response_frame(
        drain,
        state="unresolved",
        stage="drain",
        code="timeout_unknown",
    )
    terminal = _race_two(
        (
            lambda: first.reject_stage(
                drain,
                owner_token=OWNER_TOKEN,
                stage="drain",
                status="drain_rejected",
                receipt=rejected_receipt,
                response_frame=rejected_response,
            ),
            lambda: second.mark_stage_unresolved(
                drain,
                owner_token=OWNER_TOKEN,
                stage="drain",
                reason="timeout_unknown",
                response_frame=unresolved_response,
            ),
        )
    )
    assert sum(isinstance(result, ReplicaJournalV2ConflictError) for result in terminal) == 1
    assert configured(path).drain_status(drain).state in {"rejected", "unresolved"}

    for index, terminal_kind in enumerate(("rejected", "unresolved"), start=1):
        terminal_path = tmp_path / f"identical-{terminal_kind}.sqlite3"
        left = configured(terminal_path)
        right = configured(terminal_path)
        terminal_request, terminal_frame = signed_request(
            operation_id=f"{index}" * 32,
            sequence=10 + index,
        )
        accept_and_claim(left, terminal_request, terminal_frame)
        left.begin_stage(
            terminal_request,
            owner_token=OWNER_TOKEN,
            stage="ingest",
        )
        if terminal_kind == "rejected":
            exact_receipt = RejectedReceipt("identical_rejected_000000000000")
            exact_response = response_frame(
                terminal_request,
                state="rejected",
                stage="ingest",
                code="artifact_rejected",
                receipt_id=exact_receipt.receipt_id,
            )
            exact_terminal = _race_two(
                (
                    _rejection_call(
                        left,
                        terminal_request,
                        exact_receipt,
                        exact_response,
                    ),
                    _rejection_call(
                        right,
                        terminal_request,
                        exact_receipt,
                        exact_response,
                    ),
                )
            )
        else:
            exact_response = response_frame(
                terminal_request,
                state="unresolved",
                stage="ingest",
                code="timeout_unknown",
            )
            exact_terminal = _race_two(
                (
                    _unresolved_call(left, terminal_request, exact_response),
                    _unresolved_call(right, terminal_request, exact_response),
                )
            )
        assert exact_terminal[0] == exact_terminal[1]


class FaultJournal(BrokerReplicaJournalV2):
    def __init__(self, path: Path) -> None:
        self.fault: str | None = None
        super().__init__(
            path,
            application_id=APPLICATION_ID,
            expected_limits_sha256=LIMITS_SHA256,
            request_public_key=REQUEST_KEY.public_key(),
            response_public_key=RESPONSE_KEY.public_key(),
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


def _crash_uncommitted_append(
    tmp_path: Path,
    journal: BrokerReplicaJournalV2,
    identity: tuple[str, str],
    values: list[tuple[object, ...]],
) -> Path:
    payload_path = tmp_path / f"crash-{len(values)}.pickle"
    payload_path.write_bytes(
        pickle.dumps(
            {
                "path": str(journal.path),
                "identity": identity,
                "values": values,
                "request_public_key": REQUEST_KEY.public_key().public_bytes(
                    serialization.Encoding.Raw,
                    serialization.PublicFormat.Raw,
                ),
                "response_public_key": RESPONSE_KEY.public_key().public_bytes(
                    serialization.Encoding.Raw,
                    serialization.PublicFormat.Raw,
                ),
            }
        )
    )
    ready_path = Path(f"{payload_path}.ready")
    script = """
import pickle
import sys
import time
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from yinshi.services.broker_replica_journal_v2 import BrokerReplicaJournalV2

with open(sys.argv[1], "rb") as payload_file:
    payload = pickle.load(payload_file)
journal = BrokerReplicaJournalV2(
    Path(payload["path"]),
    application_id="yinshi-desktop",
    expected_limits_sha256="8" * 64,
    request_public_key=Ed25519PublicKey.from_public_bytes(payload["request_public_key"]),
    response_public_key=Ed25519PublicKey.from_public_bytes(payload["response_public_key"]),
)
database = journal._open()
database.execute("PRAGMA cache_spill = OFF")
database.execute("PRAGMA cache_size = -262144")
database.execute("BEGIN IMMEDIATE")
for values in payload["values"]:
    database.execute(journal._insert_sql(), values)
database.execute("CREATE TABLE crash_padding(value BLOB NOT NULL)")
for _index in range(96):
    database.execute("INSERT INTO crash_padding VALUES (randomblob(1048576))")
Path(sys.argv[2]).write_text("ready", encoding="ascii")
time.sleep(0.05)
database.commit()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(payload_path), str(ready_path)],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    rollback_path = Path(f"{journal.path}-journal")
    deadline = time.monotonic() + 30.0
    while not ready_path.exists() and process.poll() is None:
        if time.monotonic() >= deadline:
            process.kill()
            raise AssertionError("crash subprocess did not prepare its transaction")
        time.sleep(0.001)
    rollback_magic = b"\xd9\xd5\x05\xf9 \xa1c\xd7"
    while process.poll() is None:
        try:
            with rollback_path.open("rb") as rollback_file:
                hot = rollback_file.read(len(rollback_magic)) == rollback_magic
        except FileNotFoundError:
            hot = False
        if hot:
            process.kill()
            break
        if time.monotonic() >= deadline:
            process.kill()
            raise AssertionError("crash subprocess did not produce a hot rollback journal")
        time.sleep(0.0001)
    stdout, stderr = process.communicate(timeout=10.0)
    assert process.returncode is not None and process.returncode < 0, (stdout, stderr)
    with rollback_path.open("rb") as rollback_file:
        assert rollback_file.read(len(rollback_magic)) == rollback_magic
    return rollback_path


def _commit_wal_state(
    tmp_path: Path,
    journal: BrokerReplicaJournalV2,
    *,
    values: list[tuple[object, ...]],
    future_user_version: bool = False,
) -> tuple[Path, Path]:
    payload_path = tmp_path / f"wal-{journal.path.name}.pickle"
    payload_path.write_bytes(
        pickle.dumps(
            {
                "path": str(journal.path),
                "insert_sql": journal._insert_sql(),
                "values": values,
                "future_user_version": future_user_version,
            }
        )
    )
    script = """
import os
import pickle
import sqlite3
import sys

with open(sys.argv[1], "rb") as payload_file:
    payload = pickle.load(payload_file)
database = sqlite3.connect(payload["path"])
assert database.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
database.execute("PRAGMA wal_autocheckpoint = 0")
database.execute("PRAGMA synchronous = FULL")
database.execute("BEGIN IMMEDIATE")
for values in payload["values"]:
    database.execute(payload["insert_sql"], values)
if payload["future_user_version"]:
    database.execute("PRAGMA user_version = 99")
database.commit()
os._exit(92)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(payload_path)],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert completed.returncode == 92, completed.stderr
    wal_path = Path(f"{journal.path}-wal")
    shm_path = Path(f"{journal.path}-shm")
    assert wal_path.stat().st_size > 32
    assert shm_path.stat().st_size > 0
    return wal_path, shm_path


def _checkpoint_wal_and_crash(journal: BrokerReplicaJournalV2) -> Path:
    wal_path = Path(f"{journal.path}-wal")
    shm_path = Path(f"{journal.path}-shm")
    # Some supported SQLite versions unlink -shm during the WAL-to-DELETE
    # transition. Preserve the exact shm bytes and mode observed at the
    # checkpoint boundary so the shm-only fixture stays deterministic.
    shm_bytes = shm_path.read_bytes()
    shm_mode = stat.S_IMODE(shm_path.stat().st_mode)
    script = """
import os
import sqlite3
import sys

database = sqlite3.connect(sys.argv[1])
assert database.execute("PRAGMA journal_mode = DELETE").fetchone() == ("delete",)
os._exit(93)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(journal.path)],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert completed.returncode == 93, completed.stderr
    assert not wal_path.exists()
    if not shm_path.exists():
        shm_path.write_bytes(shm_bytes)
        os.chmod(shm_path, shm_mode)
    assert shm_path.stat().st_size > 0
    return shm_path


def _create_shm_only_state(
    tmp_path: Path,
    journal: BrokerReplicaJournalV2,
) -> tuple[Path, BrokerRequest]:
    initial, initial_frame = signed_request()
    accept_and_claim(journal, initial, initial_frame)
    values = journal._event_values(
        journal._identity(initial),
        event_type="stage_started",
        stage="ingest",
        owner_token=OWNER_TOKEN,
    )
    _commit_wal_state(tmp_path, journal, values=[values])
    return _checkpoint_wal_and_crash(journal), initial


def _freeze_file_states(paths: tuple[Path, ...]) -> dict[Path, tuple[bytes, os.stat_result]]:
    result: dict[Path, tuple[bytes, os.stat_result]] = {}
    for index, path in enumerate(paths):
        content = path.read_bytes()
        os.utime(
            path,
            ns=(946686000000000000 + index, 946686100000000000 + index),
        )
        result[path] = (content, path.stat())
    return result


def _assert_file_states_unchanged(
    states: dict[Path, tuple[bytes, os.stat_result]],
) -> None:
    for path, (content, metadata) in states.items():
        _assert_file_unchanged(path, content, metadata)


def test_valid_committed_wal_is_privately_validated_then_recovered(tmp_path: Path) -> None:
    journal = configured(tmp_path / "valid-wal.sqlite3")
    initial, initial_frame = signed_request()
    accept_and_claim(journal, initial, initial_frame)
    values = journal._event_values(
        journal._identity(initial),
        event_type="stage_started",
        stage="ingest",
        owner_token=OWNER_TOKEN,
    )
    wal_path, shm_path = _commit_wal_state(
        tmp_path,
        journal,
        values=[values],
    )

    recovered = configured(journal.path)

    assert recovered.initial_status(initial).state == "in_flight"
    assert not wal_path.exists()
    assert not shm_path.exists()


def test_future_committed_wal_is_rejected_without_mutation(tmp_path: Path) -> None:
    journal = configured(tmp_path / "future-wal.sqlite3")
    wal_path, shm_path = _commit_wal_state(
        tmp_path,
        journal,
        values=[],
        future_user_version=True,
    )
    states = _freeze_file_states((journal.path, wal_path, shm_path))

    with pytest.raises(ReplicaJournalV2SyncError, match="version"):
        configured(journal.path)

    _assert_file_states_unchanged(states)


def test_malformed_committed_wal_is_rejected_without_mutation(tmp_path: Path) -> None:
    journal = configured(tmp_path / "malformed-wal.sqlite3")
    initial, initial_frame = signed_request()
    accept_and_claim(journal, initial, initial_frame)
    values = journal._event_values(
        journal._identity(initial),
        event_type="stage_started",
        stage="ingest",
        owner_token=OWNER_TOKEN,
    )
    wal_path, shm_path = _commit_wal_state(
        tmp_path,
        journal,
        values=[values],
    )
    wal_bytes = bytearray(wal_path.read_bytes())
    wal_bytes[-1] ^= 0xFF
    wal_path.write_bytes(wal_bytes)
    states = _freeze_file_states((journal.path, wal_path, shm_path))

    with pytest.raises(ReplicaJournalV2SyncError):
        configured(journal.path)

    _assert_file_states_unchanged(states)


def test_shm_only_restart_recovers_after_crash_at_checkpoint_boundary(
    tmp_path: Path,
) -> None:
    journal = configured(tmp_path / "checkpoint-crash.sqlite3")
    shm_path, initial = _create_shm_only_state(tmp_path, journal)

    recovered = configured(journal.path)

    assert recovered.initial_status(initial).state == "in_flight"
    assert not shm_path.exists()


def test_empty_wal_is_cleaned_after_exact_main_validation(tmp_path: Path) -> None:
    journal = configured(tmp_path / "empty-wal.sqlite3")
    wal_path = Path(f"{journal.path}-wal")
    wal_path.touch(mode=0o600)

    recovered = configured(journal.path)

    assert recovered.path == journal.path
    assert not wal_path.exists()


def test_shm_only_unlink_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = configured(tmp_path / "shm-unlink-retry.sqlite3")
    shm_path, _ = _create_shm_only_state(tmp_path, journal)
    states = _freeze_file_states((journal.path, shm_path))
    original_unlink = Path.unlink
    failed = False

    def fail_shm_unlink(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal failed
        if path == shm_path and not failed:
            failed = True
            raise OSError("injected SHM unlink failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_shm_unlink)

    with pytest.raises(ReplicaJournalV2SyncError, match="SHM cleanup"):
        configured(journal.path)

    _assert_file_states_unchanged(states)
    recovered = configured(journal.path)
    assert recovered.path == journal.path
    assert failed
    assert not shm_path.exists()


def test_shm_only_parent_fsync_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = configured(tmp_path / "shm-fsync-retry.sqlite3")
    shm_path, _ = _create_shm_only_state(tmp_path, journal)
    original_fsync = replica_journal_v2.os.fsync
    parent_sync_calls = 0

    def fail_first_parent_fsync(file_descriptor: int) -> None:
        nonlocal parent_sync_calls
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            parent_sync_calls += 1
            if parent_sync_calls == 1:
                raise OSError("injected parent fsync failure")
        original_fsync(file_descriptor)

    monkeypatch.setattr(replica_journal_v2.os, "fsync", fail_first_parent_fsync)

    with pytest.raises(ReplicaJournalV2SyncError, match="SHM cleanup"):
        configured(journal.path)

    assert not shm_path.exists()
    recovered = configured(journal.path)
    assert recovered.path == journal.path
    assert parent_sync_calls >= 2


def test_shm_only_rejects_unsafe_sidecar_without_mutation(tmp_path: Path) -> None:
    journal = configured(tmp_path / "unsafe-shm.sqlite3")
    shm_path = Path(f"{journal.path}-shm")
    shm_path.write_bytes(bytes(32 * 1024))
    shm_path.chmod(0o644)
    states = _freeze_file_states((journal.path, shm_path))

    with pytest.raises(ReplicaJournalV2SyncError, match="SHM state is unsafe"):
        configured(journal.path)

    _assert_file_states_unchanged(states)


def test_shm_only_rejects_malformed_main_without_mutation(tmp_path: Path) -> None:
    journal = configured(tmp_path / "malformed-shm-main.sqlite3")
    shm_path, _ = _create_shm_only_state(tmp_path, journal)
    journal.path.write_bytes(b"malformed replica journal V2 main")
    states = _freeze_file_states((journal.path, shm_path))

    with pytest.raises(ReplicaJournalV2SyncError):
        configured(journal.path)

    _assert_file_states_unchanged(states)


def test_hot_rollback_recovery_discards_uncommitted_single_event(tmp_path: Path) -> None:
    journal = configured(tmp_path / "single-crash.sqlite3")
    initial, initial_frame = signed_request()
    accept_and_claim(journal, initial, initial_frame)
    before_rows = event_rows(journal.path)
    values = journal._event_values(
        journal._identity(initial),
        event_type="stage_started",
        stage="ingest",
        owner_token=OWNER_TOKEN,
    )
    rollback_path = _crash_uncommitted_append(
        tmp_path,
        journal,
        journal._identity(initial),
        [values],
    )

    recovered = configured(journal.path)

    assert not rollback_path.exists()
    assert event_rows(recovered.path) == before_rows
    assert recovered.initial_status(initial).state == "claimed"


def test_hot_rollback_recovery_discards_atomic_pause_transaction(tmp_path: Path) -> None:
    journal = configured(tmp_path / "pause-crash.sqlite3")
    initial, initial_frame = signed_request()
    accept_and_claim(journal, initial, initial_frame)
    for stage in ("ingest", "verify", "publish"):
        journal.begin_stage(initial, owner_token=OWNER_TOKEN, stage=stage)
        journal.complete_stage(
            initial,
            owner_token=OWNER_TOKEN,
            stage=stage,
            receipt=receipt_for(stage),
        )
    journal.begin_stage(initial, owner_token=OWNER_TOKEN, stage="admission")
    admission = receipt_for("admission")
    assert isinstance(admission, AdmissionReceipt)
    active = response_frame(
        initial,
        state="active",
        stage="admission",
        receipt_id=admission.receipt_id,
    )
    identity = journal._identity(initial)
    outcome = journal._event_values(
        identity,
        event_type="stage_outcome",
        stage="admission",
        owner_token=OWNER_TOKEN,
        stage_status="completed",
        receipt_json=canonical_json(
            {
                "execution_owner_id": admission.execution_owner_id,
                "receipt_id": admission.receipt_id,
                "runtime_unit_id": admission.runtime_unit_id,
                "session_socket_sha256": admission.session_socket_sha256,
            }
        ),
    )
    pause = journal._event_values(
        identity,
        event_type="lifecycle_paused",
        owner_token=OWNER_TOKEN,
        response_frame=active,
    )
    before_rows = event_rows(journal.path)
    rollback_path = _crash_uncommitted_append(
        tmp_path,
        journal,
        identity,
        [outcome, pause],
    )

    recovered = configured(journal.path)

    assert not rollback_path.exists()
    assert event_rows(recovered.path) == before_rows
    assert recovered.initial_status(initial).state == "in_flight"
    assert recovered.completed_receipts(initial).admission is None


def test_fresh_connection_verifies_absent_present_and_atomic_pause(tmp_path: Path) -> None:
    initial, frame = signed_request()
    before = FaultJournal(tmp_path / "before.sqlite3")
    before.fault = "before"
    with pytest.raises(ReplicaJournalV2CommitAbsent):
        before.accept_initial(initial, frame, AUTHORITY)
    assert before.initial_status(initial).state == "absent"

    after = FaultJournal(tmp_path / "after.sqlite3")
    after.fault = "after"
    assert after.accept_initial(initial, frame, AUTHORITY).state == "accepted"
    assert len(event_rows(after.path)) == 1

    pause = FaultJournal(tmp_path / "pause.sqlite3")
    accept_and_claim(pause, initial, frame)
    for stage in ("ingest", "verify", "publish"):
        pause.begin_stage(initial, owner_token=OWNER_TOKEN, stage=stage)
        pause.complete_stage(
            initial,
            owner_token=OWNER_TOKEN,
            stage=stage,
            receipt=receipt_for(stage),
        )
    pause.begin_stage(initial, owner_token=OWNER_TOKEN, stage="admission")
    admission = receipt_for("admission")
    assert isinstance(admission, AdmissionReceipt)
    active = response_frame(
        initial, state="active", stage="admission", receipt_id=admission.receipt_id
    )
    before_count = len(event_rows(pause.path))
    pause.fault = "before"
    with pytest.raises(ReplicaJournalV2CommitAbsent):
        pause.pause_after_admission(
            initial,
            owner_token=OWNER_TOKEN,
            receipt=admission,
            response_frame=active,
        )
    assert len(event_rows(pause.path)) == before_count
    pause.fault = "after"
    pause.pause_after_admission(
        initial,
        owner_token=OWNER_TOKEN,
        receipt=admission,
        response_frame=active,
    )
    assert len(event_rows(pause.path)) == before_count + 2


def test_v2_initializes_only_absent_or_zero_length_files(tmp_path: Path) -> None:
    absent = tmp_path / "absent.sqlite3"
    assert configured(absent).initial_status(signed_request()[0]).state == "absent"

    empty = tmp_path / "empty.sqlite3"
    empty.write_bytes(b"")
    os.chmod(empty, 0o640)
    assert configured(empty).initial_status(signed_request()[0]).state == "absent"
    assert empty.stat().st_size > 0
    assert stat.S_IMODE(empty.stat().st_mode) == 0o600


def test_v2_rejects_nonzero_schema_free_and_future_files_without_mutation(
    tmp_path: Path,
) -> None:
    schema_free = tmp_path / "schema-free.sqlite3"
    with sqlite3.connect(schema_free) as database:
        database.execute("VACUUM")

    future = tmp_path / "future.sqlite3"
    with sqlite3.connect(future) as database:
        database.execute("PRAGMA user_version = 99")

    future_v2 = configured(tmp_path / "future-v2.sqlite3").path
    with sqlite3.connect(future_v2) as database:
        database.execute("PRAGMA user_version = 99")

    freelist = tmp_path / "freelist.sqlite3"
    sentinel = b"artifact-content-sentinel-v2-freelist" * 200
    with sqlite3.connect(freelist) as database:
        database.execute("PRAGMA secure_delete = OFF")
        database.execute("CREATE TABLE discarded_artifact(value BLOB NOT NULL)")
        database.execute("INSERT INTO discarded_artifact VALUES (?)", (sentinel,))
        database.execute("DROP TABLE discarded_artifact")
    assert b"artifact-content-sentinel-v2-freelist" in freelist.read_bytes()
    contaminated_rollback = Path(f"{freelist}-journal")
    contaminated_rollback.write_bytes(b"\xd9\xd5\x05\xf9 \xa1c\xd7" + b"\0" * 1016)

    for index, candidate in enumerate((schema_free, future, future_v2, freelist)):
        expected_content = candidate.read_bytes()
        os.chmod(candidate, 0o640)
        os.utime(
            candidate,
            ns=(946684800000000000 + index, 946684900000000000 + index),
        )
        expected_stat = candidate.stat()
        rollback_state: tuple[bytes, os.stat_result] | None = None
        rollback_path = Path(f"{candidate}-journal")
        if rollback_path.exists():
            rollback_content = rollback_path.read_bytes()
            os.utime(
                rollback_path,
                ns=(946685000000000000 + index, 946685100000000000 + index),
            )
            rollback_state = (rollback_content, rollback_path.stat())
        with pytest.raises(ReplicaJournalV2SyncError):
            configured(candidate)
        _assert_file_unchanged(candidate, expected_content, expected_stat)
        if rollback_state is not None:
            _assert_file_unchanged(
                rollback_path,
                rollback_state[0],
                rollback_state[1],
            )


def _create_legacy_v1(path: Path, *, application_id: str = APPLICATION_ID) -> None:
    with sqlite3.connect(path) as database:
        for statement in LEGACY_REPLICA_JOURNAL_V1_SCHEMA_STATEMENTS:
            database.execute(statement)
        database.execute(
            "INSERT INTO replica_journal_meta VALUES (0, ?, 1, ?)",
            (application_id, "0" * 32 + "_" + "1" * 64),
        )


def test_legacy_inspector_is_immutable_and_rejects_unsafe_states(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    inspect_legacy_replica_v1_journal(missing, application_id=APPLICATION_ID)
    assert not missing.exists()

    path = tmp_path / "legacy.sqlite3"
    _create_legacy_v1(path)
    expected_content = path.read_bytes()
    os.chmod(path, 0o640)
    os.utime(path, ns=(946684800000000000, 946684900000000000))
    expected_stat = path.stat()
    inspect_legacy_replica_v1_journal(path, application_id=APPLICATION_ID)
    _assert_file_unchanged(path, expected_content, expected_stat)

    for index, suffix in enumerate(("-journal", "-wal", "-shm")):
        sidecar = Path(f"{path}{suffix}")
        sidecar.write_bytes(f"unresolved-{suffix}".encode("ascii"))
        sidecar_content = sidecar.read_bytes()
        os.utime(
            sidecar,
            ns=(946685000000000000 + index, 946685100000000000 + index),
        )
        sidecar_stat = sidecar.stat()
        with pytest.raises(LegacyReplicaJournalStateError, match="sidecar"):
            inspect_legacy_replica_v1_journal(path, application_id=APPLICATION_ID)
        _assert_file_unchanged(sidecar, sidecar_content, sidecar_stat)
        sidecar.unlink()

    for suffix in ("-journal", "-wal", "-shm"):
        orphan = tmp_path / f"orphan{suffix}.sqlite3"
        sidecar = Path(f"{orphan}{suffix}")
        sidecar.write_bytes(f"orphan-{suffix}".encode("ascii"))
        sidecar_content = sidecar.read_bytes()
        os.utime(sidecar, ns=(946685200000000000, 946685300000000000))
        sidecar_stat = sidecar.stat()
        with pytest.raises(LegacyReplicaJournalStateError, match="orphaned"):
            inspect_legacy_replica_v1_journal(orphan, application_id=APPLICATION_ID)
        _assert_file_unchanged(sidecar, sidecar_content, sidecar_stat)


@pytest.mark.parametrize(
    "failure_point",
    ("hash", "metadata", "sqlite", "second_hash", "final_identity"),
)
def test_legacy_inspector_restores_timestamps_after_post_open_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    path = tmp_path / f"legacy-{failure_point}.sqlite3"
    _create_legacy_v1(path)
    expected_content = path.read_bytes()
    os.utime(path, ns=(946684800000000000, 946684900000000000))
    expected_stat = path.stat()
    monkeypatch.delattr(replica_journal_v2.os, "O_NOATIME", raising=False)
    if failure_point in {"hash", "second_hash"}:
        original_hash = replica_journal_v2._hash_file_descriptor
        hash_calls = 0
        failure_call = 1 if failure_point == "hash" else 2

        def hash_then_fail(file_descriptor: int, size: int) -> str:
            nonlocal hash_calls
            hash_calls += 1
            result = original_hash(file_descriptor, size)
            if hash_calls == failure_call:
                raise OSError("injected post-read hash failure")
            return result

        monkeypatch.setattr(
            replica_journal_v2,
            "_hash_file_descriptor",
            hash_then_fail,
        )
    elif failure_point in {"metadata", "final_identity"}:
        original_fstat = replica_journal_v2.os.fstat
        fstat_calls = 0

        failure_call = 2 if failure_point == "metadata" else 5

        def fail_selected_fstat(file_descriptor: int) -> os.stat_result:
            nonlocal fstat_calls
            fstat_calls += 1
            if fstat_calls == failure_call:
                raise OSError("injected post-read metadata failure")
            return original_fstat(file_descriptor)

        monkeypatch.setattr(replica_journal_v2.os, "fstat", fail_selected_fstat)
    else:

        def fail_sqlite_connection(file_descriptor: int) -> sqlite3.Connection:
            raise OSError(f"injected SQLite failure for descriptor {file_descriptor}")

        monkeypatch.setattr(
            replica_journal_v2,
            "_connect_pinned_immutable",
            fail_sqlite_connection,
        )
    with pytest.raises(LegacyReplicaJournalStateError):
        inspect_legacy_replica_v1_journal(path, application_id=APPLICATION_ID)
    _assert_file_unchanged(path, expected_content, expected_stat)


@pytest.mark.parametrize("suffix", ("-journal", "-wal", "-shm"))
def test_legacy_inspector_rechecks_sidecars_after_pinned_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    path = tmp_path / "legacy-race.sqlite3"
    _create_legacy_v1(path)
    expected_content = path.read_bytes()
    os.utime(path, ns=(946684800000000000, 946684900000000000))
    expected_stat = path.stat()
    sidecar = Path(f"{path}{suffix}")
    original_connect = replica_journal_v2._connect_pinned_immutable

    def connect_and_create_sidecar(file_descriptor: int) -> sqlite3.Connection:
        sidecar.write_bytes(b"appeared-during-inspection")
        return original_connect(file_descriptor)

    monkeypatch.setattr(
        replica_journal_v2,
        "_connect_pinned_immutable",
        connect_and_create_sidecar,
    )
    with pytest.raises(LegacyReplicaJournalStateError, match="sidecar"):
        inspect_legacy_replica_v1_journal(path, application_id=APPLICATION_ID)
    _assert_file_unchanged(path, expected_content, expected_stat)
    assert sidecar.read_bytes() == b"appeared-during-inspection"


def test_legacy_inspector_rejects_accepted_schema_version_and_application(
    tmp_path: Path,
) -> None:
    accepted = tmp_path / "accepted.sqlite3"
    _create_legacy_v1(accepted)
    initial, frame = signed_request()
    with sqlite3.connect(accepted) as database:
        database.execute(
            "INSERT INTO replica_journal_events("
            "append_receipt_id, database_incarnation, operation_id, request_type, "
            "event_type, request_frame, request_frame_sha256, "
            "request_broker_incarnation, request_nonce, request_connection_sequence, "
            "request_payload_digest, physical_target_id, replica_generation, "
            "execution_owner_id) VALUES (?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "0" * 32,
                initial.database_incarnation,
                initial.operation_id,
                initial.request_type,
                frame,
                hashlib.sha256(frame).hexdigest(),
                initial.broker_incarnation,
                initial.nonce,
                initial.connection_sequence,
                initial.payload_digest,
                AUTHORITY.physical_target_id,
                AUTHORITY.replica_generation,
                AUTHORITY.execution_owner_id,
            ),
        )
    with pytest.raises(LegacyReplicaJournalStateError, match="accepted operation"):
        inspect_legacy_replica_v1_journal(accepted, application_id=APPLICATION_ID)

    wrong_app = tmp_path / "wrong-app.sqlite3"
    _create_legacy_v1(wrong_app, application_id="other")
    with pytest.raises(LegacyReplicaJournalStateError, match="metadata"):
        inspect_legacy_replica_v1_journal(wrong_app, application_id=APPLICATION_ID)

    malformed = tmp_path / "malformed.sqlite3"
    malformed.write_bytes(b"not sqlite")
    with pytest.raises(LegacyReplicaJournalStateError):
        inspect_legacy_replica_v1_journal(malformed, application_id=APPLICATION_ID)


def test_replay_enforces_full_event_null_matrix(tmp_path: Path) -> None:
    initial, initial_frame = signed_request()
    successful = configured(tmp_path / "successful.sqlite3")
    complete_initial_through_pause(successful, initial, initial_frame)
    drain, drain_frame, continuation = drain_request(initial, initial_frame)
    successful.accept_drain_continuation(drain, drain_frame, continuation)
    for stage in ("drain", "export", "reclaim"):
        successful.begin_stage(drain, owner_token=OWNER_TOKEN, stage=stage)
        receipt = receipt_for(stage)
        response = None
        if isinstance(receipt, ReclaimReceipt):
            response = response_frame(
                drain,
                state="completed",
                stage="reclaim",
                receipt_id=receipt.reclaim_receipt_id,
            )
        successful.complete_stage(
            drain,
            owner_token=OWNER_TOKEN,
            stage=stage,
            receipt=receipt,
            response_frame=response,
        )

    rejected = configured(tmp_path / "rejected.sqlite3")
    accept_and_claim(rejected, initial, initial_frame)
    rejected.begin_stage(initial, owner_token=OWNER_TOKEN, stage="ingest")
    rejected_receipt = RejectedReceipt("matrix_rejected_000000000000000")
    rejected.reject_stage(
        initial,
        owner_token=OWNER_TOKEN,
        stage="ingest",
        status="artifact_rejected",
        receipt=rejected_receipt,
        response_frame=response_frame(
            initial,
            state="rejected",
            stage="ingest",
            code="artifact_rejected",
            receipt_id=rejected_receipt.receipt_id,
        ),
    )

    unresolved = configured(tmp_path / "unresolved.sqlite3")
    accept_and_claim(unresolved, initial, initial_frame)
    unresolved.begin_stage(initial, owner_token=OWNER_TOKEN, stage="ingest")
    unresolved.mark_stage_unresolved(
        initial,
        owner_token=OWNER_TOKEN,
        stage="ingest",
        reason="timeout_unknown",
        response_frame=response_frame(
            initial,
            state="unresolved",
            stage="ingest",
            code="timeout_unknown",
        ),
    )

    nullable_columns = (
        "stage",
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
        "initial_request_sha256",
        "admission_receipt_id",
        "application_drain_intent_receipt_id",
        "owner_token",
        "owner_broker_incarnation",
        "stage_status",
        "receipt_json",
        "unresolved_reason",
        "response_frame",
        "response_frame_sha256",
    )
    request_columns = {
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
    }
    expected_nonnull = {
        "accepted": request_columns,
        "authority_claimed": {"owner_token", "owner_broker_incarnation"},
        "stage_started": {"stage", "owner_token"},
        "stage_rejected": {
            "stage",
            "owner_token",
            "stage_status",
            "receipt_json",
            "response_frame",
            "response_frame_sha256",
        },
        "stage_unresolved": {
            "stage",
            "owner_token",
            "unresolved_reason",
            "response_frame",
            "response_frame_sha256",
        },
        "lifecycle_paused": {
            "owner_token",
            "response_frame",
            "response_frame_sha256",
        },
        "drain_continuation_accepted": request_columns
        | {
            "initial_request_sha256",
            "admission_receipt_id",
            "application_drain_intent_receipt_id",
            "owner_token",
        },
    }
    for path in (successful.path, rejected.path, unresolved.path):
        with sqlite3.connect(path) as database:
            rows = database.execute(
                "SELECT event_type, " + ", ".join(nullable_columns) + " "
                "FROM replica_journal_v2_events"
            ).fetchall()
        for row in rows:
            event_type = str(row[0])
            actual_nonnull = {
                column
                for column, value in zip(nullable_columns, row[1:], strict=True)
                if value is not None
            }
            if event_type == "stage_outcome":
                expected = {"stage", "owner_token", "stage_status", "receipt_json"}
                if row[1] == "reclaim":
                    expected |= {"response_frame", "response_frame_sha256"}
            else:
                expected = expected_nonnull[event_type]
            assert actual_nonnull == expected

    corruptions = (
        (successful.path, "accepted", None, "owner_token", OWNER_TOKEN),
        (successful.path, "authority_claimed", None, "request_frame", b"x"),
        (successful.path, "stage_started", "ingest", "receipt_json", b"{}"),
        (
            successful.path,
            "stage_outcome",
            "ingest",
            "unresolved_reason",
            "timeout_unknown",
        ),
        (successful.path, "lifecycle_paused", None, "stage", "admission"),
        (
            successful.path,
            "drain_continuation_accepted",
            None,
            "owner_broker_incarnation",
            BROKER_INCARNATION,
        ),
        (rejected.path, "stage_rejected", "ingest", "request_type", "replica.lifecycle"),
        (unresolved.path, "stage_unresolved", "ingest", "receipt_json", b"{}"),
    )
    for index, (source, event_type, stage, column, value) in enumerate(corruptions):
        candidate = tmp_path / f"null-matrix-{index}.sqlite3"
        shutil.copy2(source, candidate)
        _corrupt_event_field(
            candidate,
            event_type=event_type,
            stage=stage,
            column=column,
            value=value,
        )
        with pytest.raises(ReplicaJournalV2SyncError):
            configured(candidate)


def test_open_rejects_schema_field_hash_signature_and_order_corruption(
    tmp_path: Path,
) -> None:
    initial, frame = signed_request()
    schema = configured(tmp_path / "schema.sqlite3")
    with sqlite3.connect(schema.path) as database:
        database.execute("DROP INDEX replica_journal_v2_one_pause")
    with pytest.raises(ReplicaJournalV2SyncError, match="schema"):
        configured(schema.path)

    digest = configured(tmp_path / "digest.sqlite3")
    digest.accept_initial(initial, frame, AUTHORITY)
    with sqlite3.connect(digest.path) as database:
        database.execute("DROP TRIGGER replica_journal_v2_reject_update")
        database.execute(
            "UPDATE replica_journal_v2_events SET request_frame_sha256 = ?",
            ("9" * 64,),
        )
        database.execute(
            next(
                statement
                for statement in REPLICA_JOURNAL_V2_SCHEMA_STATEMENTS
                if "CREATE TRIGGER replica_journal_v2_reject_update" in statement
            )
        )
    with pytest.raises(ReplicaJournalV2SyncError):
        configured(digest.path)

    fields = configured(tmp_path / "fields.sqlite3")
    fields.accept_initial(initial, frame, AUTHORITY)
    with sqlite3.connect(fields.path) as database:
        database.execute("DROP TRIGGER replica_journal_v2_reject_update")
        database.execute(
            "UPDATE replica_journal_v2_events SET owner_token = ?",
            (OWNER_TOKEN,),
        )
        database.execute(
            next(
                statement
                for statement in REPLICA_JOURNAL_V2_SCHEMA_STATEMENTS
                if "CREATE TRIGGER replica_journal_v2_reject_update" in statement
            )
        )
    with pytest.raises(ReplicaJournalV2SyncError, match="fields"):
        configured(fields.path)

    signature = configured(tmp_path / "signature.sqlite3")
    signature.accept_initial(initial, frame, AUTHORITY)
    frame_object = json.loads(frame)
    frame_object["signature"] = "A" * 86
    bad_signature_frame = canonical_json(frame_object)
    with sqlite3.connect(signature.path) as database:
        database.execute("DROP TRIGGER replica_journal_v2_reject_update")
        database.execute(
            "UPDATE replica_journal_v2_events "
            "SET request_frame = ?, request_frame_sha256 = ? "
            "WHERE event_type = 'accepted'",
            (
                bad_signature_frame,
                hashlib.sha256(bad_signature_frame).hexdigest(),
            ),
        )
        _restore_update_trigger(database)
    with pytest.raises(ReplicaJournalV2SyncError):
        configured(signature.path)

    reordered = configured(tmp_path / "reordered.sqlite3")
    accept_and_claim(reordered, initial, frame)
    with sqlite3.connect(reordered.path) as database:
        database.execute("DROP TRIGGER replica_journal_v2_reject_update")
        database.execute(
            "UPDATE replica_journal_v2_events SET event_id = -event_id WHERE event_id IN (1, 2)"
        )
        database.execute(
            "UPDATE replica_journal_v2_events "
            "SET event_id = CASE event_id WHEN -1 THEN 2 WHEN -2 THEN 1 END "
            "WHERE event_id IN (-1, -2)"
        )
        _restore_update_trigger(database)
    with pytest.raises(ReplicaJournalV2SyncError, match="acceptance"):
        configured(reordered.path)


def test_sqlite_contains_no_artifact_bytes_or_unsupported_events(tmp_path: Path) -> None:
    journal = configured(tmp_path / "v2.sqlite3")
    initial, frame = signed_request()
    complete_initial_through_pause(journal, initial, frame)
    database_bytes = journal.path.read_bytes()
    assert b"artifact-content-sentinel" not in database_bytes
    assert bytes.fromhex(BUNDLE.sha256) not in database_bytes

    contaminated = configured(tmp_path / "contaminated.sqlite3")
    contaminated.accept_initial(initial, frame, AUTHORITY)
    contaminated.claim_authority(
        initial,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )
    contaminated.begin_stage(initial, owner_token=OWNER_TOKEN, stage="ingest")
    contaminated.complete_stage(
        initial,
        owner_token=OWNER_TOKEN,
        stage="ingest",
        receipt=receipt_for("ingest"),
    )
    _corrupt_event_field(
        contaminated.path,
        event_type="stage_outcome",
        stage="ingest",
        column="receipt_json",
        value=b"artifact-content-sentinel",
    )
    assert b"artifact-content-sentinel" in contaminated.path.read_bytes()
    with pytest.raises(ReplicaJournalV2SyncError):
        configured(contaminated.path)

    with (
        sqlite3.connect(journal.path) as database,
        pytest.raises(sqlite3.IntegrityError),
    ):
        database.execute(
            "INSERT INTO replica_journal_v2_events("
            "append_receipt_id, database_incarnation, operation_id, event_type) "
            "VALUES (?, ?, ?, 'resumed')",
            ("f" * 32, DATABASE_INCARNATION, "f" * 32),
        )


def test_v1_source_hash_remains_frozen() -> None:
    source = Path(__file__).parents[1] / "src/yinshi/services/broker_replica_journal.py"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == (
        "04a0ee45c0ca058cb465674a8e7130a747fea439a254725df4b78de05a0157a4"
    )
