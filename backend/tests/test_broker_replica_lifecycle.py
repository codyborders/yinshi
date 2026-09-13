"""Check durable broker coordination for replica lifecycle stages."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from yinshi.services.broker_protocol import (
    BROKER_PROTOCOL_VERSION,
    BrokerRequest,
    JsonValue,
    create_signed_request,
    parse_signed_request,
    verify_broker_response,
)
from yinshi.services.broker_replica_journal import (
    AdmissionReceipt,
    ArtifactReference,
    BrokerReplicaJournal,
    DrainReceipt,
    ExportReceipt,
    IngestReceipt,
    PublishReceipt,
    ReclaimReceipt,
    ReplicaAuthority,
    ReplicaJournalCommitAbsent,
    VerifyReceipt,
)
from yinshi.services.broker_replica_lifecycle import (
    BrokerReplicaLifecycleCoordinator,
    BrokerReplicaOwnerActive,
    ReplicaLifecycleContext,
    ReplicaLifecycleEffects,
    StageOutcomeUnknown,
    StageReconciliation,
    StageRejected,
)
from yinshi.services.replica_artifact_contract import compute_replica_artifact_set_sha256

APPLICATION_ID = "yinshi-desktop"
DATABASE_INCARNATION = "d" * 32
BROKER_INCARNATION = "b" * 32
RESTARTED_BROKER_INCARNATION = "c" * 32
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
RECLAIM_RECEIPT_ID = "reclaim_0000000000000000000000"


def _artifact_json(reference: ArtifactReference) -> dict[str, JsonValue]:
    return {
        "artifact_id": reference.artifact_id,
        "byte_length": reference.byte_length,
        "sha256": reference.sha256,
    }


def _artifact_set_sha256() -> str:
    return compute_replica_artifact_set_sha256(
        operation_id="a" * 32,
        repository_id="repository_0000000000000000000000",
        workspace_id="workspace_000000000000000000000000",
        physical_target_id=AUTHORITY.physical_target_id,
        replica_generation=AUTHORITY.replica_generation,
        execution_owner_id=AUTHORITY.execution_owner_id,
        object_format="sha256",
        source_state_sha256="4" * 64,
        reconciliation_fingerprint="3" * 64,
        bundle={
            "role": "committed_bundle",
            "media_type": "application/vnd.yinshi.git-bundle.v1",
            **_artifact_json(BUNDLE),
        },
        worktree={
            "role": "worktree",
            "media_type": "application/vnd.yinshi.replica-worktree.v1",
            **_artifact_json(WORKTREE),
        },
        index_objects={
            "role": "index_objects",
            "media_type": "application/vnd.yinshi.git-index-objects-pack.v1",
            **_artifact_json(INDEX_OBJECTS),
        },
        limits_sha256=LIMITS_SHA256,
    )


ARTIFACT_SET_SHA256 = _artifact_set_sha256()


def _signed_request() -> tuple[BrokerRequest, bytes]:
    frame = create_signed_request(
        private_key=REQUEST_PRIVATE_KEY,
        protocol_version=BROKER_PROTOCOL_VERSION,
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        connection_sequence=1,
        operation_id="a" * 32,
        request_type="replica.lifecycle",
        nonce="nonce_00000000001",
        payload={
            "artifact_set_sha256": ARTIFACT_SET_SHA256,
            "bundle": _artifact_json(BUNDLE),
            "index_objects": _artifact_json(INDEX_OBJECTS),
            "limits_sha256": LIMITS_SHA256,
            "object_format": "sha256",
            "reconciliation_fingerprint": "3" * 64,
            "repository_id": "repository_0000000000000000000000",
            "source_state_sha256": "4" * 64,
            "workspace_id": "workspace_000000000000000000000000",
            "worktree": _artifact_json(WORKTREE),
        },
    )
    request = parse_signed_request(frame, public_key=REQUEST_PRIVATE_KEY.public_key())
    return request, frame


def _receipt(stage: str) -> object:
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
            artifact_set_sha256=ARTIFACT_SET_SHA256,
        )
    if stage == "publish":
        return PublishReceipt(
            receipt_id=receipt_id,
            physical_target_id=AUTHORITY.physical_target_id,
            replica_generation=AUTHORITY.replica_generation,
            execution_owner_id=AUTHORITY.execution_owner_id,
            publication_marker_sha256="5" * 64,
            synchronization_receipt_id="sync_000000000000000000000000000",
            artifact_set_sha256=ARTIFACT_SET_SHA256,
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
        return ReclaimReceipt(reclaim_receipt_id=RECLAIM_RECEIPT_ID)
    raise AssertionError(stage)


def _journal(tmp_path: Path) -> BrokerReplicaJournal:
    return BrokerReplicaJournal(
        tmp_path / "replica.sqlite3",
        application_id=APPLICATION_ID,
        expected_limits_sha256=LIMITS_SHA256,
        request_public_key=REQUEST_PRIVATE_KEY.public_key(),
        response_public_keys={
            BROKER_INCARNATION: RESPONSE_PRIVATE_KEY.public_key(),
            RESTARTED_BROKER_INCARNATION: RESPONSE_PRIVATE_KEY.public_key(),
        },
    )


class _Effect:
    def __init__(
        self,
        stage: str,
        trace: list[tuple[str, str]],
        *,
        apply_result: object | None = None,
        reconcile_result: object | None = None,
    ) -> None:
        self.stage = stage
        self.trace = trace
        self.apply_result = apply_result or StageReconciliation(
            outcome="completed", receipt=_receipt(stage)
        )
        self.reconcile_result = reconcile_result
        self.started = asyncio.Event()

    async def apply(self, context: ReplicaLifecycleContext) -> StageReconciliation:
        self.trace.append(("apply", self.stage))
        self.started.set()
        return await self._dispatch(self.apply_result, context)

    async def reconcile(self, context: ReplicaLifecycleContext) -> StageReconciliation:
        self.trace.append(("reconcile", self.stage))
        if self.reconcile_result is None:
            raise AssertionError("unexpected reconciliation")
        return await self._dispatch(self.reconcile_result, context)

    @staticmethod
    async def _dispatch(value: object, context: ReplicaLifecycleContext) -> StageReconciliation:
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, StageReconciliation):
            return value
        result = await value(context)  # type: ignore[operator]
        assert isinstance(result, StageReconciliation)
        return result


def _effects(
    trace: list[tuple[str, str]],
    overrides: dict[str, _Effect] | None = None,
) -> dict[str, _Effect]:
    result = {stage: _Effect(stage, trace) for stage in STAGES}
    if overrides is not None:
        result.update(overrides)
    return result


def _coordinator(
    replica: BrokerReplicaJournal,
    effects: dict[str, _Effect],
    *,
    broker_incarnation: str = BROKER_INCARNATION,
    effect_timeout_seconds: float = 5.0,
    owner_wait_seconds: float = 0.2,
) -> BrokerReplicaLifecycleCoordinator:
    return BrokerReplicaLifecycleCoordinator(
        replica,
        broker_incarnation=broker_incarnation,
        broker_private_keys={
            BROKER_INCARNATION: RESPONSE_PRIVATE_KEY,
            RESTARTED_BROKER_INCARNATION: RESPONSE_PRIVATE_KEY,
        },
        effects=ReplicaLifecycleEffects(
            ingest=effects["ingest"],
            verify=effects["verify"],
            publish=effects["publish"],
            admission=effects["admission"],
            drain=effects["drain"],
            export=effects["export"],
            reclaim=effects["reclaim"],
        ),
        effect_timeout_seconds=effect_timeout_seconds,
        owner_wait_seconds=owner_wait_seconds,
        owner_poll_seconds=0.01,
    )


def _events(path: Path) -> list[tuple[str, str | None]]:
    with sqlite3.connect(path) as database:
        return [
            (str(event_type), stage)
            for event_type, stage in database.execute(
                "SELECT event_type, stage FROM replica_journal_events ORDER BY event_id"
            ).fetchall()
        ]


def _result(frame: bytes | None) -> dict[str, object]:
    assert frame is not None
    value = json.loads(frame)
    assert isinstance(value["result"], dict)
    return value["result"]


async def test_runs_exact_stage_order_with_start_before_each_effect(tmp_path: Path) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    trace: list[tuple[str, str]] = []
    response = await _coordinator(replica, _effects(trace)).run(request, frame, AUTHORITY)

    assert trace == [("apply", stage) for stage in STAGES]
    events = _events(replica.path)
    assert events[:2] == [("accepted", None), ("authority_claimed", None)]
    for stage in STAGES:
        start = events.index(("stage_started", stage))
        complete = events.index(("stage_outcome", stage))
        assert start < complete
    assert _result(response) == {
        "stage": "reclaim",
        "state": "completed",
        "receipt_id": RECLAIM_RECEIPT_ID,
    }
    verify_broker_response(
        response,
        public_key=RESPONSE_PRIVATE_KEY.public_key(),
        expected_request=request,
    )


async def test_concurrent_duplicate_runs_each_stage_once(tmp_path: Path) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    trace: list[tuple[str, str]] = []
    release = asyncio.Event()
    duplicate_started = asyncio.Event()
    apply_count = 0

    async def blocking(_context: ReplicaLifecycleContext) -> StageReconciliation:
        nonlocal apply_count
        apply_count += 1
        if apply_count > 1:
            duplicate_started.set()
        await release.wait()
        return StageReconciliation(outcome="completed", receipt=_receipt("ingest"))

    ingest = _Effect("ingest", trace, apply_result=blocking)
    coordinator = _coordinator(replica, _effects(trace, {"ingest": ingest}))
    first = asyncio.create_task(coordinator.run(request, frame, AUTHORITY))
    await asyncio.wait_for(ingest.started.wait(), timeout=1)
    second = asyncio.create_task(coordinator.run(request, frame, AUTHORITY))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(duplicate_started.wait(), timeout=0.05)
    finally:
        release.set()
    first_response, second_response = await asyncio.gather(first, second)
    assert first_response == second_response
    assert trace.count(("apply", "ingest")) == 1
    assert trace == [("apply", stage) for stage in STAGES]


async def test_terminal_replay_runs_no_effect(tmp_path: Path) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    first_trace: list[tuple[str, str]] = []
    first = await _coordinator(replica, _effects(first_trace)).run(request, frame, AUTHORITY)
    replay_trace: list[tuple[str, str]] = []
    replay = await _coordinator(replica, _effects(replay_trace)).run(request, frame, AUTHORITY)
    assert replay == first
    assert replay_trace == []


def test_wrong_response_signer_fails_before_effects(tmp_path: Path) -> None:
    replica = _journal(tmp_path)
    trace: list[tuple[str, str]] = []
    stage_effects = _effects(trace)
    with pytest.raises(ValueError, match="response signer"):
        BrokerReplicaLifecycleCoordinator(
            replica,
            broker_incarnation=BROKER_INCARNATION,
            broker_private_keys={BROKER_INCARNATION: Ed25519PrivateKey.generate()},
            effects=ReplicaLifecycleEffects(
                ingest=stage_effects["ingest"],
                verify=stage_effects["verify"],
                publish=stage_effects["publish"],
                admission=stage_effects["admission"],
                drain=stage_effects["drain"],
                export=stage_effects["export"],
                reclaim=stage_effects["reclaim"],
            ),
        )
    assert trace == []


@pytest.mark.parametrize(
    "failure",
    [
        StageOutcomeUnknown("INVALID REASON"),
        StageRejected("INVALID STATUS", "reject_receipt_000000000000000"),
        StageRejected("artifact_rejected", "short"),
    ],
)
async def test_malformed_terminal_effect_result_becomes_stage_unknown(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    trace: list[tuple[str, str]] = []
    effect = _Effect("ingest", trace, apply_result=failure)
    response = await _coordinator(replica, _effects(trace, {"ingest": effect})).run(
        request, frame, AUTHORITY
    )
    assert _result(response) == {
        "stage": "ingest",
        "state": "unresolved",
        "code": "transfer_unknown",
    }


async def test_semantically_invalid_receipt_becomes_stage_unknown(tmp_path: Path) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    trace: list[tuple[str, str]] = []
    wrong_publish = replace(
        cast(PublishReceipt, _receipt("publish")),
        physical_target_id="different_target_00000000000000000",
    )
    effect = _Effect(
        "publish",
        trace,
        apply_result=StageReconciliation(outcome="completed", receipt=wrong_publish),
    )
    response = await _coordinator(replica, _effects(trace, {"publish": effect})).run(
        request, frame, AUTHORITY
    )
    assert _result(response) == {
        "stage": "publish",
        "state": "unresolved",
        "code": "publication_unknown",
    }
    assert trace[-1] == ("apply", "publish")


async def test_rejection_and_unknown_are_terminal(tmp_path: Path) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    trace: list[tuple[str, str]] = []
    rejected = _Effect(
        "ingest",
        trace,
        apply_result=StageRejected("artifact_rejected", "reject_receipt_000000000000000"),
    )
    response = await _coordinator(replica, _effects(trace, {"ingest": rejected})).run(
        request, frame, AUTHORITY
    )
    assert _result(response) == {
        "stage": "ingest",
        "state": "rejected",
        "code": "artifact_rejected",
        "receipt_id": "reject_receipt_000000000000000",
    }

    second_replica = _journal(tmp_path / "unknown")
    second_request, second_frame = _signed_request()
    second_trace: list[tuple[str, str]] = []
    unknown = _Effect(
        "ingest",
        second_trace,
        apply_result=StageOutcomeUnknown("transfer_unknown"),
    )
    second = await _coordinator(second_replica, _effects(second_trace, {"ingest": unknown})).run(
        second_request, second_frame, AUTHORITY
    )
    assert _result(second) == {
        "stage": "ingest",
        "state": "unresolved",
        "code": "transfer_unknown",
    }


@pytest.mark.parametrize(
    ("stage", "wrong_receipt", "reason"),
    [
        ("ingest", _receipt("verify"), "transfer_unknown"),
        ("verify", _receipt("publish"), "verification_unknown"),
        ("publish", _receipt("admission"), "publication_unknown"),
        ("admission", _receipt("drain"), "admission_unknown"),
        ("drain", _receipt("export"), "acknowledgment_unknown"),
        ("export", _receipt("reclaim"), "transfer_unknown"),
        ("reclaim", _receipt("ingest"), "effect_unknown"),
    ],
)
async def test_wrong_receipt_type_becomes_stage_unknown(
    tmp_path: Path,
    stage: str,
    wrong_receipt: object,
    reason: str,
) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    trace: list[tuple[str, str]] = []
    wrong = _Effect(
        stage,
        trace,
        apply_result=StageReconciliation(outcome="completed", receipt=wrong_receipt),
    )
    response = await _coordinator(replica, _effects(trace, {stage: wrong})).run(
        request, frame, AUTHORITY
    )
    assert _result(response) == {
        "stage": stage,
        "state": "unresolved",
        "code": reason,
    }
    assert trace[-1] == ("apply", stage)
    assert all(
        recorded_stage not in STAGES[STAGES.index(stage) + 1 :] for _, recorded_stage in trace
    )


async def test_timeout_does_not_persist_late_completion(tmp_path: Path) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    trace: list[tuple[str, str]] = []

    async def late(_context: ReplicaLifecycleContext) -> StageReconciliation:
        await asyncio.sleep(0.2)
        return StageReconciliation(outcome="completed", receipt=_receipt("ingest"))

    effect = _Effect("ingest", trace, apply_result=late)
    coordinator = _coordinator(
        replica,
        _effects(trace, {"ingest": effect}),
        effect_timeout_seconds=0.02,
    )
    response = await coordinator.run(request, frame, AUTHORITY)
    snapshot = _events(replica.path)
    assert _result(response)["code"] == "timeout_unknown"
    await asyncio.sleep(0.25)
    assert _events(replica.path) == snapshot


@pytest.mark.parametrize("finish_mode", ["timeout", "cancel"])
async def test_retry_waits_for_active_effect_after_unresolved_commit_absence(
    tmp_path: Path,
    finish_mode: str,
) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    trace: list[tuple[str, str]] = []
    release = asyncio.Event()

    async def late(_context: ReplicaLifecycleContext) -> StageReconciliation:
        await release.wait()
        return StageReconciliation(outcome="completed", receipt=_receipt("ingest"))

    ingest = _Effect(
        "ingest",
        trace,
        apply_result=late,
        reconcile_result=StageReconciliation(
            outcome="completed",
            receipt=_receipt("ingest"),
        ),
    )
    coordinator = _coordinator(
        replica,
        _effects(trace, {"ingest": ingest}),
        effect_timeout_seconds=0.02 if finish_mode == "timeout" else 5.0,
    )
    original_record_unresolved = replica.record_unresolved
    call_count = 0

    def fail_first_unresolved(*args: object, **kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ReplicaJournalCommitAbsent("forced unresolved commit absence")
        return original_record_unresolved(*args, **kwargs)  # type: ignore[arg-type]

    replica.record_unresolved = fail_first_unresolved  # type: ignore[method-assign]
    first = asyncio.create_task(coordinator.run(request, frame, AUTHORITY))
    await asyncio.wait_for(ingest.started.wait(), timeout=1)
    if finish_mode == "cancel":
        first.cancel()
    with pytest.raises(ReplicaJournalCommitAbsent):
        await first

    retry = asyncio.create_task(coordinator.run(request, frame, AUTHORITY))
    await asyncio.sleep(0.05)
    assert trace == [("apply", "ingest")]
    release.set()
    response = await asyncio.wait_for(retry, timeout=1)
    assert trace[0:2] == [("apply", "ingest"), ("reconcile", "ingest")]
    assert trace.count(("apply", "ingest")) == 1
    assert _result(response)["state"] == "completed"


async def test_cancellation_records_unknown_then_reraises(tmp_path: Path) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    trace: list[tuple[str, str]] = []

    async def late(_context: ReplicaLifecycleContext) -> StageReconciliation:
        await asyncio.sleep(0.2)
        return StageReconciliation(outcome="completed", receipt=_receipt("ingest"))

    effect = _Effect("ingest", trace, apply_result=late)
    coordinator = _coordinator(replica, _effects(trace, {"ingest": effect}))
    task = asyncio.create_task(coordinator.run(request, frame, AUTHORITY))
    await asyncio.wait_for(effect.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _result(replica.status(request).response_frame)["code"] == "cancellation_unknown"


async def test_other_owner_wait_is_bounded(tmp_path: Path) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    replica.accept(request, frame, AUTHORITY)
    replica.claim_authority(
        request,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )
    trace: list[tuple[str, str]] = []
    with pytest.raises(BrokerReplicaOwnerActive):
        await _coordinator(replica, _effects(trace), owner_wait_seconds=0.03).run(
            request, frame, AUTHORITY
        )
    assert trace == []


@pytest.mark.parametrize(
    ("reconciliation", "expected_first"),
    [
        (
            StageReconciliation(outcome="completed", receipt=_receipt("ingest")),
            ("reconcile", "ingest"),
        ),
        (StageReconciliation(outcome="not_applied"), ("reconcile", "ingest")),
    ],
)
async def test_started_stage_recovery_reconciles_then_finishes(
    tmp_path: Path,
    reconciliation: StageReconciliation,
    expected_first: tuple[str, str],
) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    replica.accept(request, frame, AUTHORITY)
    replica.claim_authority(
        request,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )
    replica.begin_stage(request, owner_token=OWNER_TOKEN, stage="ingest")
    trace: list[tuple[str, str]] = []
    ingest = _Effect("ingest", trace, reconcile_result=reconciliation)
    await _coordinator(replica, _effects(trace, {"ingest": ingest})).recover_incomplete()
    assert trace[0] == expected_first
    assert replica.status(request).state == "completed"


@pytest.mark.parametrize(
    ("reconciliation", "expect_apply", "expected_state"),
    [
        (
            StageReconciliation(outcome="completed", receipt=_receipt("ingest")),
            False,
            "completed",
        ),
        (StageReconciliation(outcome="not_applied"), True, "completed"),
        (
            StageReconciliation(outcome="unknown", reason="verification_unknown"),
            False,
            "unresolved",
        ),
    ],
)
async def test_request_retry_reconciles_durably_started_stage(
    tmp_path: Path,
    reconciliation: StageReconciliation,
    expect_apply: bool,
    expected_state: str,
) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    trace: list[tuple[str, str]] = []
    coordinator = _coordinator(replica, _effects(trace))
    replica.accept(request, frame, AUTHORITY)
    replica.claim_authority(
        request,
        owner_token=coordinator._owner_token,
        broker_incarnation=BROKER_INCARNATION,
    )
    replica.begin_stage(
        request,
        owner_token=coordinator._owner_token,
        stage="ingest",
    )
    ingest = _Effect("ingest", trace, reconcile_result=reconciliation)
    coordinator._effects["ingest"] = ingest
    response = await coordinator.run(request, frame, AUTHORITY)
    assert trace[0] == ("reconcile", "ingest")
    assert (("apply", "ingest") in trace) is expect_apply
    assert _result(response)["state"] == expected_state


async def test_unknown_recovery_blocks_later_stages(tmp_path: Path) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    replica.accept(request, frame, AUTHORITY)
    replica.claim_authority(
        request,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )
    replica.begin_stage(request, owner_token=OWNER_TOKEN, stage="ingest")
    trace: list[tuple[str, str]] = []
    ingest = _Effect(
        "ingest",
        trace,
        reconcile_result=StageReconciliation(outcome="unknown", reason="verification_unknown"),
    )
    await _coordinator(replica, _effects(trace, {"ingest": ingest})).recover_incomplete()
    assert trace == [("reconcile", "ingest")]
    assert _result(replica.status(request).response_frame)["code"] == "verification_unknown"


async def test_different_broker_incarnation_recovers_as_unknown(tmp_path: Path) -> None:
    replica = _journal(tmp_path)
    request, frame = _signed_request()
    replica.accept(request, frame, AUTHORITY)
    replica.claim_authority(
        request,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )
    trace: list[tuple[str, str]] = []
    coordinator = _coordinator(
        replica,
        _effects(trace),
        broker_incarnation=RESTARTED_BROKER_INCARNATION,
    )
    await coordinator.recover_incomplete()
    assert trace == []
    assert _result(replica.status(request).response_frame)["code"] == "broker_restart_unknown"


@pytest.mark.parametrize("mode", ["raise", "return"])
async def test_malformed_effect_invocation_becomes_stage_unknown(
    tmp_path: Path,
    mode: str,
) -> None:
    class MalformedEffect:
        def apply(self, _context: ReplicaLifecycleContext) -> object:
            if mode == "raise":
                raise RuntimeError("malformed adapter")
            return object()

        async def reconcile(self, _context: ReplicaLifecycleContext) -> StageReconciliation:
            raise AssertionError("unexpected reconciliation")

    replica = _journal(tmp_path)
    request, frame = _signed_request()
    trace: list[tuple[str, str]] = []
    effects = _effects(trace)
    effects["ingest"] = cast(_Effect, MalformedEffect())
    response = await _coordinator(replica, effects).run(request, frame, AUTHORITY)
    assert _result(response) == {
        "stage": "ingest",
        "state": "unresolved",
        "code": "transfer_unknown",
    }


async def test_custom_awaitable_effect_is_supported(tmp_path: Path) -> None:
    class CustomAwaitable:
        def __await__(self):
            async def result() -> StageReconciliation:
                return StageReconciliation(
                    outcome="completed",
                    receipt=_receipt("ingest"),
                )

            return result().__await__()

    class CustomEffect:
        def apply(self, _context: ReplicaLifecycleContext) -> CustomAwaitable:
            return CustomAwaitable()

        async def reconcile(self, _context: ReplicaLifecycleContext) -> StageReconciliation:
            raise AssertionError("unexpected reconciliation")

    replica = _journal(tmp_path)
    request, frame = _signed_request()
    trace: list[tuple[str, str]] = []
    effects = _effects(trace)
    effects["ingest"] = cast(_Effect, CustomEffect())
    response = await _coordinator(replica, effects).run(request, frame, AUTHORITY)
    assert _result(response)["state"] == "completed"


def test_stage_result_validation_is_fail_closed() -> None:
    with pytest.raises(ValueError):
        StageReconciliation(outcome="completed")
    with pytest.raises(ValueError):
        StageReconciliation(outcome="not_applied", receipt=_receipt("ingest"))
    with pytest.raises(ValueError):
        StageReconciliation(outcome="unknown")
