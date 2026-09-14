"""Check paused replica lifecycle V2 coordination and recovery."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.test_broker_replica_journal_v2 import (
    AUTHORITY,
    BROKER_INCARNATION,
    FOREIGN_INCARNATION,
    OWNER_TOKEN,
    RESPONSE_KEY,
    configured,
    drain_request,
    event_rows,
    receipt_for,
    signed_request,
)
from yinshi.services.broker_protocol import (
    BrokerProtocolError,
    BrokerRequest,
    verify_broker_response,
)
from yinshi.services.broker_replica_journal import (
    AdmissionReceipt,
    ExportReceipt,
    IngestReceipt,
    ReplicaAuthority,
)
from yinshi.services.broker_replica_journal_v2 import (
    BrokerReplicaJournalV2,
    CompletedReplicaReceipts,
    ReplicaDrainContinuation,
    ReplicaJournalV2CommitAbsent,
    ReplicaJournalV2ConflictError,
    ReplicaJournalV2SyncError,
    replica_drain_continuation_from_request,
)
from yinshi.services.broker_replica_lifecycle import (
    BrokerReplicaLifecycleError,
    BrokerReplicaOwnerActive,
    StageOutcomeUnknown,
    StageReconciliation,
    StageRejected,
)
from yinshi.services.broker_replica_lifecycle_v2 import (
    REPLICA_LIFECYCLE_V2_STAGES,
    BrokerReplicaLifecycleCoordinatorV2,
    ReplicaLifecycleContextV2,
    ReplicaLifecycleEffectsV2,
    ReplicaStageEffectV2,
)

STAGES = REPLICA_LIFECYCLE_V2_STAGES
EffectResult = (
    StageReconciliation[object]
    | BaseException
    | Callable[[ReplicaLifecycleContextV2], Awaitable[StageReconciliation[object]]]
)


class ImmediateUnknownEffect:
    def __init__(
        self,
        stage: str,
        trace: list[tuple[str, str]],
        reason: object,
    ) -> None:
        self.stage = stage
        self.trace = trace
        self.reason = reason

    def apply(
        self,
        _context: ReplicaLifecycleContextV2,
    ) -> StageReconciliation[object]:
        self.trace.append(("apply", self.stage))
        raise StageOutcomeUnknown(cast(str, self.reason))

    def reconcile(
        self,
        _context: ReplicaLifecycleContextV2,
    ) -> StageReconciliation[object]:
        self.trace.append(("reconcile", self.stage))
        raise StageOutcomeUnknown(cast(str, self.reason))


class Effect:
    def __init__(
        self,
        stage: str,
        trace: list[tuple[str, str]],
        contexts: list[tuple[str, ReplicaLifecycleContextV2]],
        *,
        apply_result: EffectResult | None = None,
        reconcile_result: EffectResult | None = None,
    ) -> None:
        self.stage = stage
        self.trace = trace
        self.contexts = contexts
        self.apply_result = apply_result or StageReconciliation(
            outcome="completed",
            receipt=receipt_for(stage),
        )
        self.reconcile_result = reconcile_result or StageReconciliation(
            outcome="completed",
            receipt=receipt_for(stage),
        )
        self.started = asyncio.Event()

    async def apply(
        self,
        context: ReplicaLifecycleContextV2,
    ) -> StageReconciliation[object]:
        self.trace.append(("apply", self.stage))
        self.contexts.append((self.stage, context))
        self.started.set()
        return await self._resolve(self.apply_result, context)

    async def reconcile(
        self,
        context: ReplicaLifecycleContextV2,
    ) -> StageReconciliation[object]:
        self.trace.append(("reconcile", self.stage))
        self.contexts.append((self.stage, context))
        self.started.set()
        return await self._resolve(self.reconcile_result, context)

    @staticmethod
    async def _resolve(
        result: EffectResult,
        context: ReplicaLifecycleContextV2,
    ) -> StageReconciliation[object]:
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return await result(context)
        return result


def effects(
    trace: list[tuple[str, str]],
    contexts: list[tuple[str, ReplicaLifecycleContextV2]],
    overrides: dict[str, Effect] | None = None,
) -> tuple[ReplicaLifecycleEffectsV2, dict[str, Effect]]:
    configured_effects = {
        stage: (overrides or {}).get(stage, Effect(stage, trace, contexts)) for stage in STAGES
    }
    return (
        ReplicaLifecycleEffectsV2(
            ingest=cast(Effect, configured_effects["ingest"]),
            verify=cast(Effect, configured_effects["verify"]),
            publish=cast(Effect, configured_effects["publish"]),
            admission=cast(Effect, configured_effects["admission"]),
            drain=cast(Effect, configured_effects["drain"]),
            export=cast(Effect, configured_effects["export"]),
            reclaim=cast(Effect, configured_effects["reclaim"]),
        ),
        configured_effects,
    )


def coordinator(
    journal: BrokerReplicaJournalV2,
    trace: list[tuple[str, str]],
    contexts: list[tuple[str, ReplicaLifecycleContextV2]],
    *,
    overrides: dict[str, Effect] | None = None,
    broker_incarnation: str = BROKER_INCARNATION,
    effect_timeout_seconds: float = 0.3,
    owner_wait_seconds: float = 0.1,
) -> tuple[BrokerReplicaLifecycleCoordinatorV2, dict[str, Effect]]:
    configured_effects, stage_effects = effects(trace, contexts, overrides)
    return (
        BrokerReplicaLifecycleCoordinatorV2(
            journal,
            broker_incarnation=broker_incarnation,
            broker_private_key=RESPONSE_KEY,
            effects=configured_effects,
            effect_timeout_seconds=effect_timeout_seconds,
            owner_wait_seconds=owner_wait_seconds,
            owner_poll_seconds=0.005,
        ),
        stage_effects,
    )


def result(frame: bytes, request: BrokerRequest) -> dict[str, object]:
    verified = verify_broker_response(
        frame,
        public_key=RESPONSE_KEY.public_key(),
        expected_request=request,
    )
    assert isinstance(verified.result, dict)
    return cast(dict[str, object], verified.result)


def event_types(path: Path) -> list[tuple[str, str | None]]:
    with sqlite3.connect(path) as database:
        return cast(
            list[tuple[str, str | None]],
            database.execute(
                "SELECT event_type, stage FROM replica_journal_v2_events ORDER BY event_id"
            ).fetchall(),
        )


def seed_started_initial_stage(
    journal: BrokerReplicaJournalV2,
    stage: str,
) -> tuple[BrokerRequest, bytes]:
    initial, initial_frame = signed_request()
    journal.accept_initial(initial, initial_frame, AUTHORITY)
    journal.claim_authority(
        initial,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )
    for completed_stage in STAGES[: STAGES.index(stage)]:
        journal.begin_stage(initial, owner_token=OWNER_TOKEN, stage=completed_stage)
        journal.complete_stage(
            initial,
            owner_token=OWNER_TOKEN,
            stage=completed_stage,
            receipt=receipt_for(completed_stage),
        )
    journal.begin_stage(initial, owner_token=OWNER_TOKEN, stage=stage)
    return initial, initial_frame


def malformed_unknown_effect(
    *,
    stage: str,
    trace: list[tuple[str, str]],
    contexts: list[tuple[str, ReplicaLifecycleContextV2]],
    reason: object,
    synchronous: bool,
    path: str,
) -> ReplicaStageEffectV2[object]:
    if synchronous:
        return cast(
            ReplicaStageEffectV2[object],
            ImmediateUnknownEffect(stage, trace, reason),
        )
    unknown = StageOutcomeUnknown(cast(str, reason))
    return cast(
        ReplicaStageEffectV2[object],
        Effect(
            stage,
            trace,
            contexts,
            apply_result=unknown if path == "apply" else None,
            reconcile_result=unknown if path == "reconcile" else None,
        ),
    )


async def start_to_pause(
    journal: BrokerReplicaJournalV2,
    *,
    trace: list[tuple[str, str]] | None = None,
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] | None = None,
) -> tuple[
    BrokerReplicaLifecycleCoordinatorV2,
    BrokerRequest,
    bytes,
    bytes,
    list[tuple[str, str]],
    list[tuple[str, ReplicaLifecycleContextV2]],
]:
    actual_trace = [] if trace is None else trace
    actual_contexts = [] if contexts is None else contexts
    lifecycle, _ = coordinator(journal, actual_trace, actual_contexts)
    request, request_frame = signed_request()
    active = await lifecycle.start(request, request_frame, AUTHORITY)
    return (
        lifecycle,
        request,
        request_frame,
        active,
        actual_trace,
        actual_contexts,
    )


def test_v2_stage_contract_separates_initial_and_continuation_work() -> None:
    assert STAGES == (
        "ingest",
        "verify",
        "publish",
        "admission",
        "drain",
        "export",
        "reclaim",
    )


async def test_initial_pauses_then_continuation_finishes_with_exact_replays(
    tmp_path: Path,
) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    lifecycle, initial, initial_frame, active, trace, contexts = await start_to_pause(journal)
    assert trace == [("apply", stage) for stage in STAGES[:4]]
    assert result(active, initial) == {
        "stage": "admission",
        "state": "active",
        "receipt_id": cast(AdmissionReceipt, receipt_for("admission")).receipt_id,
    }
    assert journal.initial_status(initial).response_frame == active
    assert all(stage not in {"drain", "export", "reclaim"} for _, stage in trace)
    assert event_types(journal.path) == [
        ("accepted", None),
        ("authority_claimed", None),
        ("stage_started", "ingest"),
        ("stage_outcome", "ingest"),
        ("stage_started", "verify"),
        ("stage_outcome", "verify"),
        ("stage_started", "publish"),
        ("stage_outcome", "publish"),
        ("stage_started", "admission"),
        ("stage_outcome", "admission"),
        ("lifecycle_paused", None),
    ]

    drain, drain_frame, continuation = drain_request(initial, initial_frame)
    terminal = await lifecycle.continue_drain(drain, drain_frame, continuation)
    assert trace == [("apply", stage) for stage in STAGES]
    assert result(terminal, drain) == {
        "stage": "reclaim",
        "state": "completed",
        "receipt_id": receipt_for("reclaim").reclaim_receipt_id,
    }
    assert event_types(journal.path)[-7:] == [
        ("drain_continuation_accepted", None),
        ("stage_started", "drain"),
        ("stage_outcome", "drain"),
        ("stage_started", "export"),
        ("stage_outcome", "export"),
        ("stage_started", "reclaim"),
        ("stage_outcome", "reclaim"),
    ]
    before_retry = list(trace)
    assert await lifecycle.start(initial, initial_frame, AUTHORITY) == active
    assert await lifecycle.continue_drain(drain, drain_frame, continuation) == terminal
    assert trace == before_retry

    for stage, context in contexts[:4]:
        assert context.request == initial
        assert context.drain_request is None
        assert context.drain_continuation is None
        completed = STAGES[: STAGES.index(stage)]
        assert all(getattr(context.completed_receipts, name) is not None for name in completed)
        assert all(
            getattr(context.completed_receipts, name) is None
            for name in STAGES[STAGES.index(stage) :]
        )
    for stage, context in contexts[4:]:
        assert context.request == initial
        assert context.drain_request == drain
        assert context.drain_continuation == continuation
        completed = STAGES[: STAGES.index(stage)]
        assert all(getattr(context.completed_receipts, name) is not None for name in completed)
    assert journal.completed_receipts(drain) == CompletedReplicaReceipts(
        ingest=receipt_for("ingest"),
        verify=receipt_for("verify"),
        publish=receipt_for("publish"),
        admission=receipt_for("admission"),
        drain=receipt_for("drain"),
        export=receipt_for("export"),
        reclaim=receipt_for("reclaim"),
    )


async def test_continuation_binding_conflicts_invoke_no_drain_effects(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    lifecycle, initial, initial_frame, _active, trace, _contexts = await start_to_pause(journal)
    drain, drain_frame, continuation = drain_request(initial, initial_frame)
    malformed = replace(
        continuation,
        application_drain_intent_receipt_id="other_intent_receipt_0000000000000",
    )
    before = event_rows(journal.path)
    with pytest.raises(ReplicaJournalV2ConflictError):
        await lifecycle.continue_drain(drain, drain_frame, malformed)
    assert trace == [("apply", stage) for stage in STAGES[:4]]
    assert event_rows(journal.path) == before

    conflicting, conflicting_frame, conflicting_continuation = drain_request(
        initial,
        initial_frame,
        sequence=3,
        payload_changes={"initial_request_sha256": "9" * 64},
    )
    with pytest.raises(ReplicaJournalV2ConflictError):
        await lifecycle.continue_drain(
            conflicting,
            conflicting_frame,
            conflicting_continuation,
        )
    assert event_rows(journal.path) == before


async def test_same_incarnation_pause_recovery_waits_for_continuation(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    _lifecycle, _initial, _frame, _active, _trace, _contexts = await start_to_pause(journal)
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    restarted, _ = coordinator(journal, trace, contexts)
    await restarted.recover_incomplete()
    assert trace == []
    assert journal.incomplete_lifecycles()[0].paused is True


async def test_foreign_paused_owner_becomes_drain_unknown_without_stage_start(
    tmp_path: Path,
) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    _lifecycle, initial, _frame, active, _trace, _contexts = await start_to_pause(journal)
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    restarted, _ = coordinator(
        journal,
        trace,
        contexts,
        broker_incarnation=FOREIGN_INCARNATION,
    )
    await restarted.recover_incomplete()
    assert trace == []
    assert journal.incomplete_lifecycles() == ()
    assert journal.initial_status(initial).response_frame == active
    events = event_types(journal.path)
    assert ("stage_started", "drain") not in events
    assert events[-1] == ("stage_unresolved", "drain")


async def test_accepted_continuation_crash_window_resumes_same_incarnation(
    tmp_path: Path,
) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    _old, initial, initial_frame, _active, _old_trace, _contexts = await start_to_pause(journal)
    drain, drain_frame, continuation = drain_request(initial, initial_frame)
    journal.accept_drain_continuation(drain, drain_frame, continuation)
    trace: list[tuple[str, str]] = []
    recovery_contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    restarted, _ = coordinator(journal, trace, recovery_contexts)
    await restarted.recover_incomplete()
    assert trace == [("apply", stage) for stage in STAGES[4:]]
    terminal = journal.drain_status(drain).response_frame
    assert terminal is not None
    assert result(terminal, drain)["state"] == "completed"


async def test_foreign_accepted_pause_fails_closed_without_drain_start(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    _old, initial, initial_frame, _active, _old_trace, _contexts = await start_to_pause(journal)
    drain, drain_frame, continuation = drain_request(initial, initial_frame)
    journal.accept_drain_continuation(drain, drain_frame, continuation)
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    restarted, _ = coordinator(
        journal,
        trace,
        contexts,
        broker_incarnation=FOREIGN_INCARNATION,
    )
    await restarted.recover_incomplete()
    assert trace == []
    assert ("stage_started", "drain") not in event_types(journal.path)
    response = journal.drain_status(drain).response_frame
    assert response is not None
    assert result(response, drain)["code"] == "broker_restart_unknown"


@pytest.mark.parametrize(
    ("reconciliation", "expect_apply", "expected_state"),
    [
        (
            StageReconciliation(outcome="completed", receipt=receipt_for("ingest")),
            False,
            "active",
        ),
        (StageReconciliation(outcome="not_applied"), True, "active"),
        (
            StageReconciliation(outcome="unknown", reason="verification_unknown"),
            False,
            "unresolved",
        ),
    ],
)
async def test_started_recovery_completed_not_applied_and_unknown(
    tmp_path: Path,
    reconciliation: StageReconciliation[object],
    expect_apply: bool,
    expected_state: str,
) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    initial, initial_frame = signed_request()
    journal.accept_initial(initial, initial_frame, AUTHORITY)
    journal.claim_authority(
        initial,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )
    journal.begin_stage(initial, owner_token=OWNER_TOKEN, stage="ingest")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    ingest = Effect(
        "ingest",
        trace,
        contexts,
        reconcile_result=reconciliation,
    )
    restarted, _ = coordinator(journal, trace, contexts, overrides={"ingest": ingest})
    await restarted.recover_incomplete()
    assert trace[0] == ("reconcile", "ingest")
    assert (("apply", "ingest") in trace) is expect_apply
    decision = journal.initial_status(initial)
    assert decision.state == expected_state
    if expected_state == "unresolved":
        assert trace == [("reconcile", "ingest")]


@pytest.mark.parametrize(
    ("path", "synchronous", "stage", "reason", "expected_reason"),
    [
        ("apply", True, "verify", "", "verification_unknown"),
        ("apply", False, "publish", None, "publication_unknown"),
        ("apply", True, "admission", [], "admission_unknown"),
        ("apply", False, "ingest", "runtime_live", "transfer_unknown"),
        ("reconcile", True, "publish", "", "publication_unknown"),
        ("reconcile", False, "verify", None, "verification_unknown"),
        ("reconcile", True, "admission", {}, "admission_unknown"),
        ("reconcile", False, "ingest", "runtime_live", "transfer_unknown"),
    ],
)
async def test_malformed_stage_unknown_normalizes_before_durable_response(
    tmp_path: Path,
    path: str,
    synchronous: bool,
    stage: str,
    reason: object,
    expected_reason: str,
) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    lifecycle, _ = coordinator(journal, trace, contexts)
    lifecycle._effects[stage] = malformed_unknown_effect(
        stage=stage,
        trace=trace,
        contexts=contexts,
        reason=reason,
        synchronous=synchronous,
        path=path,
    )
    if path == "apply":
        initial, initial_frame = signed_request()
        response = await lifecycle.start(initial, initial_frame, AUTHORITY)
    else:
        initial, initial_frame = seed_started_initial_stage(journal, stage)
        await lifecycle.recover_incomplete()
        response = journal.initial_status(initial).response_frame
        assert response is not None
    assert result(response, initial) == {
        "stage": stage,
        "state": "unresolved",
        "code": expected_reason,
    }
    assert trace[-1] == (path, stage)
    assert all(STAGES.index(called_stage) <= STAGES.index(stage) for _, called_stage in trace)
    before_retry = list(trace)
    assert await lifecycle.start(initial, initial_frame, AUTHORITY) == response
    assert trace == before_retry


@pytest.mark.parametrize(
    ("path", "synchronous", "stage", "reason"),
    [
        ("apply", True, "ingest", "verification_unknown"),
        ("reconcile", False, "publish", "synchronization_unknown"),
    ],
)
async def test_valid_stage_specific_unknown_reason_remains_exact(
    tmp_path: Path,
    path: str,
    synchronous: bool,
    stage: str,
    reason: str,
) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    lifecycle, _ = coordinator(journal, trace, contexts)
    lifecycle._effects[stage] = malformed_unknown_effect(
        stage=stage,
        trace=trace,
        contexts=contexts,
        reason=reason,
        synchronous=synchronous,
        path=path,
    )
    if path == "apply":
        initial, initial_frame = signed_request()
        response = await lifecycle.start(initial, initial_frame, AUTHORITY)
    else:
        initial, initial_frame = seed_started_initial_stage(journal, stage)
        await lifecycle.recover_incomplete()
        response = journal.initial_status(initial).response_frame
        assert response is not None
    assert result(response, initial)["code"] == reason
    before_retry = list(trace)
    assert await lifecycle.start(initial, initial_frame, AUTHORITY) == response
    assert trace == before_retry


async def test_malformed_unknown_persistence_failure_still_escapes(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    lifecycle, _ = coordinator(journal, trace, contexts)
    lifecycle._effects["ingest"] = malformed_unknown_effect(
        stage="ingest",
        trace=trace,
        contexts=contexts,
        reason=None,
        synchronous=True,
        path="apply",
    )
    initial, initial_frame = signed_request()

    def fail_persistence(*_args: object, **_kwargs: object) -> object:
        raise ReplicaJournalV2CommitAbsent("forced malformed unknown persistence failure")

    journal.mark_stage_unresolved = fail_persistence  # type: ignore[method-assign]
    with pytest.raises(ReplicaJournalV2CommitAbsent, match="forced malformed unknown"):
        await lifecycle.start(initial, initial_frame, AUTHORITY)
    assert trace == [("apply", "ingest")]
    assert journal.initial_status(initial).state == "in_flight"


async def test_foreign_started_state_becomes_unresolved_without_effects(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    initial, initial_frame = signed_request()
    journal.accept_initial(initial, initial_frame, AUTHORITY)
    journal.claim_authority(
        initial,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )
    journal.begin_stage(initial, owner_token=OWNER_TOKEN, stage="ingest")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    restarted, _ = coordinator(
        journal,
        trace,
        contexts,
        broker_incarnation=FOREIGN_INCARNATION,
    )
    await restarted.recover_incomplete()
    assert trace == []
    response = journal.initial_status(initial).response_frame
    assert response is not None
    assert result(response, initial)["code"] == "broker_restart_unknown"


async def test_rejection_and_malformed_receipt_stop_later_effects(tmp_path: Path) -> None:
    rejected_journal = configured(tmp_path / "rejected.sqlite3")
    rejected_trace: list[tuple[str, str]] = []
    rejected_contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    rejected_effect = Effect(
        "verify",
        rejected_trace,
        rejected_contexts,
        apply_result=StageRejected(
            "verification_rejected",
            "reject_receipt_000000000000000",
        ),
    )
    rejected, _ = coordinator(
        rejected_journal,
        rejected_trace,
        rejected_contexts,
        overrides={"verify": rejected_effect},
    )
    initial, initial_frame = signed_request()
    response = await rejected.start(initial, initial_frame, AUTHORITY)
    assert result(response, initial)["state"] == "rejected"
    assert rejected_trace == [("apply", "ingest"), ("apply", "verify")]

    malformed_journal = configured(tmp_path / "malformed.sqlite3")
    malformed_trace: list[tuple[str, str]] = []
    malformed_contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    wrong = Effect(
        "publish",
        malformed_trace,
        malformed_contexts,
        apply_result=StageReconciliation(
            outcome="completed",
            receipt=receipt_for("ingest"),
        ),
    )
    malformed, _ = coordinator(
        malformed_journal,
        malformed_trace,
        malformed_contexts,
        overrides={"publish": wrong},
    )
    second, second_frame = signed_request()
    unknown = await malformed.start(second, second_frame, AUTHORITY)
    assert result(unknown, second)["code"] == "publication_unknown"
    assert malformed_trace[-1] == ("apply", "publish")


async def test_timeout_consumes_late_result_without_persisting_it(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []

    async def late(_context: ReplicaLifecycleContextV2) -> StageReconciliation[object]:
        await asyncio.sleep(0.08)
        return StageReconciliation(outcome="completed", receipt=receipt_for("ingest"))

    ingest = Effect("ingest", trace, contexts, apply_result=late)
    lifecycle, _ = coordinator(
        journal,
        trace,
        contexts,
        overrides={"ingest": ingest},
        effect_timeout_seconds=0.01,
    )
    initial, initial_frame = signed_request()
    response = await lifecycle.start(initial, initial_frame, AUTHORITY)
    assert result(response, initial)["code"] == "timeout_unknown"
    snapshot = event_rows(journal.path)
    await asyncio.sleep(0.1)
    assert event_rows(journal.path) == snapshot


async def test_reconcile_deadline_records_timeout_and_blocks_later_stages(
    tmp_path: Path,
) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    initial, initial_frame = signed_request()
    journal.accept_initial(initial, initial_frame, AUTHORITY)
    journal.claim_authority(
        initial,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )
    journal.begin_stage(initial, owner_token=OWNER_TOKEN, stage="ingest")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []

    async def late(_context: ReplicaLifecycleContextV2) -> StageReconciliation[object]:
        await asyncio.sleep(0.08)
        return StageReconciliation(outcome="completed", receipt=receipt_for("ingest"))

    ingest = Effect("ingest", trace, contexts, reconcile_result=late)
    restarted, _ = coordinator(
        journal,
        trace,
        contexts,
        overrides={"ingest": ingest},
        effect_timeout_seconds=0.01,
    )
    await restarted.recover_incomplete()
    response = journal.initial_status(initial).response_frame
    assert response is not None
    assert result(response, initial)["code"] == "timeout_unknown"
    assert trace == [("reconcile", "ingest")]
    snapshot = event_rows(journal.path)
    await asyncio.sleep(0.1)
    assert event_rows(journal.path) == snapshot


async def test_cancellation_records_unknown_then_rethrows_after_task_bookkeeping(
    tmp_path: Path,
) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    release = asyncio.Event()

    async def late(_context: ReplicaLifecycleContextV2) -> StageReconciliation[object]:
        await release.wait()
        return StageReconciliation(outcome="completed", receipt=receipt_for("ingest"))

    ingest = Effect("ingest", trace, contexts, apply_result=late)
    lifecycle, _ = coordinator(journal, trace, contexts, overrides={"ingest": ingest})
    initial, initial_frame = signed_request()
    task = asyncio.create_task(lifecycle.start(initial, initial_frame, AUTHORITY))
    await asyncio.wait_for(ingest.started.wait(), timeout=0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    response = journal.initial_status(initial).response_frame
    assert response is not None
    assert result(response, initial)["code"] == "cancellation_unknown"
    snapshot = event_rows(journal.path)
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert event_rows(journal.path) == snapshot
    assert not lifecycle._abandoned_effects


async def test_cancel_while_waiting_for_process_gate_changes_no_durable_state(
    tmp_path: Path,
) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    release = asyncio.Event()

    async def blocked(_context: ReplicaLifecycleContextV2) -> StageReconciliation[object]:
        await release.wait()
        return StageReconciliation(outcome="completed", receipt=receipt_for("ingest"))

    ingest = Effect("ingest", trace, contexts, apply_result=blocked)
    lifecycle, _ = coordinator(journal, trace, contexts, overrides={"ingest": ingest})
    initial, initial_frame = signed_request()
    first = asyncio.create_task(lifecycle.start(initial, initial_frame, AUTHORITY))
    await asyncio.wait_for(ingest.started.wait(), timeout=0.2)
    snapshot = event_rows(journal.path)
    waiter = asyncio.create_task(lifecycle.start(initial, initial_frame, AUTHORITY))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert event_rows(journal.path) == snapshot
    release.set()
    assert result(await first, initial)["state"] == "active"


async def test_post_effect_commit_absence_becomes_sqlite_unknown(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    lifecycle, _ = coordinator(journal, trace, contexts)
    initial, initial_frame = signed_request()
    original_complete = journal.complete_stage
    calls = 0

    def fail_first_completion(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ReplicaJournalV2CommitAbsent("forced absent completion")
        return original_complete(*args, **kwargs)  # type: ignore[arg-type]

    journal.complete_stage = fail_first_completion  # type: ignore[method-assign]
    response = await lifecycle.start(initial, initial_frame, AUTHORITY)
    assert result(response, initial)["code"] == "sqlite_commit_unknown"
    assert trace == [("apply", "ingest")]


async def test_retry_waits_for_uncertain_effect_when_unknown_commit_is_absent(
    tmp_path: Path,
) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    release = asyncio.Event()

    async def late(_context: ReplicaLifecycleContextV2) -> StageReconciliation[object]:
        await release.wait()
        return StageReconciliation(outcome="completed", receipt=receipt_for("ingest"))

    ingest = Effect("ingest", trace, contexts, apply_result=late)
    lifecycle, _ = coordinator(
        journal,
        trace,
        contexts,
        overrides={"ingest": ingest},
        effect_timeout_seconds=0.01,
    )
    initial, initial_frame = signed_request()
    original_unresolved = journal.mark_stage_unresolved
    unresolved_calls = 0

    def fail_first_unresolved(*args: object, **kwargs: object) -> object:
        nonlocal unresolved_calls
        unresolved_calls += 1
        if unresolved_calls == 1:
            raise ReplicaJournalV2CommitAbsent("forced absent unresolved")
        return original_unresolved(*args, **kwargs)  # type: ignore[arg-type]

    journal.mark_stage_unresolved = fail_first_unresolved  # type: ignore[method-assign]
    with pytest.raises(ReplicaJournalV2CommitAbsent):
        await lifecycle.start(initial, initial_frame, AUTHORITY)
    retry = asyncio.create_task(lifecycle.start(initial, initial_frame, AUTHORITY))
    await asyncio.sleep(0.02)
    assert trace == [("apply", "ingest")]
    release.set()
    response = await asyncio.wait_for(retry, timeout=0.5)
    assert result(response, initial)["state"] == "active"
    assert trace[:2] == [("apply", "ingest"), ("reconcile", "ingest")]
    assert trace.count(("apply", "ingest")) == 1


async def test_persistence_failure_escapes_without_converting_failure_to_success(
    tmp_path: Path,
) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    unknown = Effect(
        "ingest",
        trace,
        contexts,
        apply_result=StageReconciliation(outcome="unknown", reason="transfer_unknown"),
    )
    lifecycle, _ = coordinator(journal, trace, contexts, overrides={"ingest": unknown})
    initial, initial_frame = signed_request()

    def fail_persistence(*_args: object, **_kwargs: object) -> object:
        raise ReplicaJournalV2CommitAbsent("forced terminal commit absence")

    journal.mark_stage_unresolved = fail_persistence  # type: ignore[method-assign]
    with pytest.raises(ReplicaJournalV2CommitAbsent, match="forced terminal"):
        await lifecycle.start(initial, initial_frame, AUTHORITY)
    assert journal.initial_status(initial).state == "in_flight"


async def test_journal_has_no_open_connection_while_effect_awaits(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    awaiting_effect = False
    original_connect = journal._connect

    def guarded_connect() -> sqlite3.Connection:
        assert not awaiting_effect
        return original_connect()

    journal._connect = guarded_connect  # type: ignore[method-assign]

    async def checked(_context: ReplicaLifecycleContextV2) -> StageReconciliation[object]:
        nonlocal awaiting_effect
        awaiting_effect = True
        await asyncio.sleep(0)
        awaiting_effect = False
        return StageReconciliation(outcome="completed", receipt=receipt_for("ingest"))

    ingest = Effect("ingest", trace, contexts, apply_result=checked)
    lifecycle, _ = coordinator(journal, trace, contexts, overrides={"ingest": ingest})
    initial, initial_frame = signed_request()
    response = await lifecycle.start(initial, initial_frame, AUTHORITY)
    assert result(response, initial)["state"] == "active"


async def test_concurrent_identical_start_is_single_flight(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    release = asyncio.Event()

    async def blocked(_context: ReplicaLifecycleContextV2) -> StageReconciliation[object]:
        await release.wait()
        return StageReconciliation(outcome="completed", receipt=receipt_for("ingest"))

    ingest = Effect("ingest", trace, contexts, apply_result=blocked)
    lifecycle, _ = coordinator(journal, trace, contexts, overrides={"ingest": ingest})
    initial, initial_frame = signed_request()
    first = asyncio.create_task(lifecycle.start(initial, initial_frame, AUTHORITY))
    await asyncio.wait_for(ingest.started.wait(), timeout=0.2)
    second = asyncio.create_task(lifecycle.start(initial, initial_frame, AUTHORITY))
    await asyncio.sleep(0.02)
    assert trace == [("apply", "ingest")]
    release.set()
    first_response, second_response = await asyncio.gather(first, second)
    assert first_response == second_response
    assert trace == [("apply", stage) for stage in STAGES[:4]]


async def test_foreign_active_owner_wait_is_bounded_and_invokes_no_effects(
    tmp_path: Path,
) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    initial, initial_frame = signed_request()
    journal.accept_initial(initial, initial_frame, AUTHORITY)
    journal.claim_authority(
        initial,
        owner_token=OWNER_TOKEN,
        broker_incarnation=BROKER_INCARNATION,
    )
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    lifecycle, _ = coordinator(journal, trace, contexts, owner_wait_seconds=0.02)
    with pytest.raises(BrokerReplicaOwnerActive):
        await lifecycle.start(initial, initial_frame, AUTHORITY)
    assert trace == []


def test_signing_key_must_match_journal_pinned_public_key(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    configured_effects, _ = effects(trace, contexts)
    with pytest.raises(ReplicaJournalV2SyncError, match="signer"):
        BrokerReplicaLifecycleCoordinatorV2(
            journal,
            broker_incarnation=BROKER_INCARNATION,
            broker_private_key=Ed25519PrivateKey.generate(),
            effects=configured_effects,
        )


async def test_request_and_response_bind_to_correct_phase(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    lifecycle, initial, initial_frame, active, _trace, _contexts = await start_to_pause(journal)
    verify_broker_response(
        active,
        public_key=RESPONSE_KEY.public_key(),
        expected_request=initial,
    )
    drain, drain_frame, continuation = drain_request(initial, initial_frame)
    terminal = await lifecycle.continue_drain(drain, drain_frame, continuation)
    verify_broker_response(
        terminal,
        public_key=RESPONSE_KEY.public_key(),
        expected_request=drain,
    )
    with pytest.raises(BrokerProtocolError):
        verify_broker_response(
            terminal,
            public_key=RESPONSE_KEY.public_key(),
            expected_request=initial,
        )


async def test_retry_resumes_durably_started_continuation_by_reconcile(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    old, initial, initial_frame, _active, _trace, _contexts = await start_to_pause(journal)
    drain, drain_frame, continuation = drain_request(initial, initial_frame)
    journal.accept_drain_continuation(drain, drain_frame, continuation)
    active = journal.active_lifecycle(drain)
    assert active is not None
    journal.begin_stage(drain, owner_token=active.owner_token, stage="drain")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    restarted, _ = coordinator(journal, trace, contexts)
    terminal = await restarted.continue_drain(drain, drain_frame, continuation)
    assert result(terminal, drain)["state"] == "completed"
    assert trace[0] == ("reconcile", "drain")
    assert ("apply", "drain") not in trace
    assert old is not restarted


async def test_continuation_argument_must_match_authenticated_request(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    lifecycle, initial, initial_frame, _active, trace, _contexts = await start_to_pause(journal)
    drain, drain_frame, _continuation = drain_request(initial, initial_frame)
    parsed = replica_drain_continuation_from_request(drain)
    assert isinstance(parsed, ReplicaDrainContinuation)
    other = replace(parsed, initial_request_sha256="8" * 64)
    before = list(trace)
    with pytest.raises(ReplicaJournalV2ConflictError):
        await lifecycle.continue_drain(drain, drain_frame, other)
    assert trace == before


async def test_different_incarnation_request_fails_before_effects(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    lifecycle, _ = coordinator(journal, trace, contexts)
    foreign, foreign_frame = signed_request(broker_incarnation=FOREIGN_INCARNATION)
    with pytest.raises(BrokerReplicaLifecycleError, match="different broker"):
        await lifecycle.start(foreign, foreign_frame, cast(ReplicaAuthority, AUTHORITY))
    assert trace == []
    assert journal.initial_status(foreign).state == "absent"


async def test_semantically_invalid_bound_receipt_becomes_unknown(tmp_path: Path) -> None:
    journal = configured(tmp_path / "replica-v2.sqlite3")
    trace: list[tuple[str, str]] = []
    contexts: list[tuple[str, ReplicaLifecycleContextV2]] = []
    wrong_ingest = replace(
        cast(IngestReceipt, receipt_for("ingest")),
        bundle=cast(ExportReceipt, receipt_for("export")).bundle,
    )
    invalid = Effect(
        "ingest",
        trace,
        contexts,
        apply_result=StageReconciliation(outcome="completed", receipt=wrong_ingest),
    )
    lifecycle, _ = coordinator(journal, trace, contexts, overrides={"ingest": invalid})
    initial, initial_frame = signed_request()
    response = await lifecycle.start(initial, initial_frame, AUTHORITY)
    assert result(response, initial)["code"] == "transfer_unknown"
    assert trace == [("apply", "ingest")]
