"""Coordinate paused replica lifecycle V2 work through durable journal authority."""

from __future__ import annotations

import asyncio
import inspect
import re
import secrets
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Protocol, TypeVar, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from yinshi.services.broker_protocol import BrokerRequest, create_signed_response
from yinshi.services.broker_replica_journal import (
    AdmissionReceipt,
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
    BrokerReplicaJournalV2,
    CompletedReplicaReceipts,
    IncompleteReplicaLifecycleV2,
    ReplicaDrainContinuation,
    ReplicaJournalV2CommitAbsent,
    ReplicaJournalV2ConflictError,
    ReplicaJournalV2Decision,
    replica_drain_continuation_from_request,
)
from yinshi.services.broker_replica_lifecycle import (
    BrokerReplicaLifecycleError,
    BrokerReplicaOwnerActive,
    StageOutcomeUnknown,
    StageReconciliation,
    StageRejected,
)

REPLICA_LIFECYCLE_V2_STAGES: tuple[str, ...] = (
    "ingest",
    "verify",
    "publish",
    "admission",
    "drain",
    "export",
    "reclaim",
)
_INITIAL_STAGES = REPLICA_LIFECYCLE_V2_STAGES[:4]
_CONTINUATION_STAGES = REPLICA_LIFECYCLE_V2_STAGES[4:]
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_OWNER_TOKEN_BYTES = 32
_MAX_ABANDONED_EFFECTS = 32
_MAX_OPERATION_GATES = 256
_EFFECT_TIMEOUT_SECONDS_DEFAULT = 30.0
_EFFECT_TIMEOUT_SECONDS_MAX = 3_600.0
_OWNER_WAIT_SECONDS_DEFAULT = 10.0
_OWNER_WAIT_SECONDS_MAX = 3_600.0
_OWNER_POLL_SECONDS_DEFAULT = 0.05
_OWNER_POLL_SECONDS_MAX = 60.0
ReceiptT_co = TypeVar("ReceiptT_co", covariant=True)
_Identity = tuple[str, str]

_UNEXPECTED_ERROR_REASONS: dict[str, str] = {
    "ingest": "transfer_unknown",
    "verify": "verification_unknown",
    "publish": "publication_unknown",
    "admission": "admission_unknown",
    "drain": "acknowledgment_unknown",
    "export": "transfer_unknown",
    "reclaim": "effect_unknown",
}
_BASE_UNRESOLVED_REASONS = frozenset(
    {
        "broker_restart_unknown",
        "cancellation_unknown",
        "sqlite_commit_unknown",
        "timeout_unknown",
        "transport_unknown",
    }
)
_STAGE_UNRESOLVED_REASONS: dict[str, frozenset[str]] = {
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
_REJECTED_STATUSES: dict[str, frozenset[str]] = {
    "ingest": frozenset({"artifact_rejected", "limit_rejected"}),
    "verify": frozenset({"verification_rejected"}),
    "publish": frozenset({"publication_rejected"}),
    "admission": frozenset({"admission_rejected"}),
    "drain": frozenset({"drain_rejected"}),
    "export": frozenset({"export_rejected", "limit_rejected"}),
    "reclaim": frozenset({"reclaim_rejected"}),
}
_EXPECTED_RECEIPT_TYPES: dict[str, type[object]] = {
    "ingest": IngestReceipt,
    "verify": VerifyReceipt,
    "publish": PublishReceipt,
    "admission": AdmissionReceipt,
    "drain": DrainReceipt,
    "export": ExportReceipt,
    "reclaim": ReclaimReceipt,
}


class _StageTerminal(Exception):
    """Carry one durably recorded terminal response through stage control."""

    def __init__(self, frame: bytes) -> None:
        super().__init__("replica V2 stage reached a terminal state")
        self.frame = frame


@dataclass(frozen=True, slots=True)
class ReplicaLifecycleContextV2:
    """Hold immutable initial authority, durable receipts, and optional continuation."""

    request: BrokerRequest
    authority: ReplicaAuthority
    owner_token: str
    completed_receipts: CompletedReplicaReceipts
    drain_request: BrokerRequest | None
    drain_continuation: ReplicaDrainContinuation | None


class ReplicaStageEffectV2(Protocol[ReceiptT_co]):
    """Apply or reconcile one replica V2 stage effect."""

    async def apply(
        self,
        context: ReplicaLifecycleContextV2,
    ) -> StageReconciliation[ReceiptT_co]:
        """Apply the stage effect after its durable start."""
        ...

    async def reconcile(
        self,
        context: ReplicaLifecycleContextV2,
    ) -> StageReconciliation[ReceiptT_co]:
        """Inspect an effect whose durable start survived interruption."""
        ...


@dataclass(frozen=True, slots=True)
class ReplicaLifecycleEffectsV2:
    """Bind all seven stages to typed effect contracts."""

    ingest: ReplicaStageEffectV2[IngestReceipt]
    verify: ReplicaStageEffectV2[VerifyReceipt]
    publish: ReplicaStageEffectV2[PublishReceipt]
    admission: ReplicaStageEffectV2[AdmissionReceipt]
    drain: ReplicaStageEffectV2[DrainReceipt]
    export: ReplicaStageEffectV2[ExportReceipt]
    reclaim: ReplicaStageEffectV2[ReclaimReceipt]


@dataclass(slots=True)
class _OperationGate:
    """Own process-local serialization for one durable lifecycle identity."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    user_count: int = 0


class BrokerReplicaLifecycleCoordinatorV2:
    """Drive initial work to pause, then independently authorized drain work."""

    def __init__(
        self,
        journal: BrokerReplicaJournalV2,
        *,
        broker_incarnation: str,
        broker_private_key: Ed25519PrivateKey,
        effects: ReplicaLifecycleEffectsV2,
        effect_timeout_seconds: float = _EFFECT_TIMEOUT_SECONDS_DEFAULT,
        owner_wait_seconds: float = _OWNER_WAIT_SECONDS_DEFAULT,
        owner_poll_seconds: float = _OWNER_POLL_SECONDS_DEFAULT,
    ) -> None:
        if not isinstance(journal, BrokerReplicaJournalV2):
            raise TypeError("replica journal V2 is invalid")
        if (
            not isinstance(broker_incarnation, str)
            or _TOKEN_PATTERN.fullmatch(broker_incarnation) is None
        ):
            raise ValueError("replica broker incarnation is invalid")
        journal.require_response_signer(broker_private_key)
        if not isinstance(effects, ReplicaLifecycleEffectsV2):
            raise TypeError("replica V2 stage effects are invalid")
        configured_effects: dict[str, ReplicaStageEffectV2[object]] = {
            "ingest": effects.ingest,
            "verify": effects.verify,
            "publish": effects.publish,
            "admission": effects.admission,
            "drain": effects.drain,
            "export": effects.export,
            "reclaim": effects.reclaim,
        }
        for stage, effect in configured_effects.items():
            if not callable(getattr(effect, "apply", None)):
                raise TypeError(f"replica V2 {stage} apply effect is invalid")
            if not callable(getattr(effect, "reconcile", None)):
                raise TypeError(f"replica V2 {stage} reconcile effect is invalid")
        self._journal = journal
        self._broker_incarnation = broker_incarnation
        self._broker_private_key = broker_private_key
        self._effects = configured_effects
        self._effect_timeout_seconds = self._bounded_seconds(
            effect_timeout_seconds,
            maximum=_EFFECT_TIMEOUT_SECONDS_MAX,
            description="effect timeout",
        )
        self._owner_wait_seconds = self._bounded_seconds(
            owner_wait_seconds,
            maximum=_OWNER_WAIT_SECONDS_MAX,
            description="owner wait",
        )
        self._owner_poll_seconds = self._bounded_seconds(
            owner_poll_seconds,
            maximum=_OWNER_POLL_SECONDS_MAX,
            description="owner poll",
        )
        self._owner_token = secrets.token_urlsafe(_OWNER_TOKEN_BYTES)
        self._abandoned_effects: set[asyncio.Future[StageReconciliation[object]]] = set()
        self._active_uncertain_effects: dict[
            _Identity, asyncio.Future[StageReconciliation[object]]
        ] = {}
        self._operation_gates: dict[_Identity, _OperationGate] = {}

    @staticmethod
    def _bounded_seconds(value: float, *, maximum: float, description: str) -> float:
        if type(value) not in {int, float} or not 0 < value <= maximum:
            raise ValueError(f"replica {description} must be a positive bounded number")
        return float(value)

    @staticmethod
    def _unknown_outcome(stage: str, reason: object) -> StageReconciliation[object]:
        allowed_reasons = _BASE_UNRESOLVED_REASONS | _STAGE_UNRESOLVED_REASONS[stage]
        normalized = (
            reason
            if isinstance(reason, str) and reason in allowed_reasons
            else _UNEXPECTED_ERROR_REASONS[stage]
        )
        return StageReconciliation(outcome="unknown", reason=normalized)

    async def start(
        self,
        request: BrokerRequest,
        request_frame: bytes,
        authority: ReplicaAuthority,
    ) -> bytes:
        """Accept an initial request and stop after atomically recording admission pause."""
        self._validate_request(
            request,
            request_frame,
            expected_type="replica.lifecycle",
        )
        if not isinstance(authority, ReplicaAuthority):
            raise TypeError("replica authority is invalid")
        identity = self._identity(request)
        gate = await self._acquire_operation_gate(identity)
        try:
            decision = self._journal.accept_initial(request, request_frame, authority)
            if decision.response_frame is not None:
                return decision.response_frame
            decision = await self._claim_within_deadline(request)
            if decision.response_frame is not None:
                return decision.response_frame
            if decision.stage not in _INITIAL_STAGES:
                raise BrokerReplicaLifecycleError(
                    "replica V2 initial lifecycle has no active stage"
                )
            context = self._context(
                initial_request=request,
                journal_request=request,
                authority=authority,
                owner_token=self._owner_token,
                drain_request=None,
                drain_continuation=None,
            )
            if decision.state == "in_flight":
                response = await self._resume_started(context, request, decision.stage)
            else:
                response = await self._drive_from(context, request, decision.stage)
            if response is None:
                raise BrokerReplicaLifecycleError("replica V2 initial lifecycle did not pause")
            return response
        finally:
            self._release_operation_gate(identity, gate)

    async def continue_drain(
        self,
        request: BrokerRequest,
        request_frame: bytes,
        continuation: ReplicaDrainContinuation,
    ) -> bytes:
        """Durably accept one authenticated continuation before any drain effect starts."""
        self._validate_request(
            request,
            request_frame,
            expected_type="replica.drain",
        )
        if not isinstance(continuation, ReplicaDrainContinuation):
            raise TypeError("replica drain continuation is invalid")
        identity = self._identity(request)
        gate = await self._acquire_operation_gate(identity)
        try:
            decision = self._journal.accept_drain_continuation(
                request,
                request_frame,
                continuation,
            )
            if decision.response_frame is not None:
                return decision.response_frame
            if decision.stage not in _CONTINUATION_STAGES:
                raise BrokerReplicaLifecycleError("replica V2 continuation has no active stage")
            active = self._journal.active_lifecycle(request)
            if active is None:
                raise BrokerReplicaLifecycleError("replica V2 active lifecycle is missing")
            if active.owner_broker_incarnation != self._broker_incarnation:
                raise BrokerReplicaLifecycleError(
                    "replica V2 lifecycle belongs to another broker incarnation"
                )
            initial_request = self._journal.authenticated_initial_request(active)
            context = self._context(
                initial_request=initial_request,
                journal_request=request,
                authority=active.authority,
                owner_token=active.owner_token,
                drain_request=request,
                drain_continuation=continuation,
            )
            if decision.state == "in_flight":
                response = await self._resume_started(context, request, decision.stage)
            else:
                response = await self._drive_from(context, request, decision.stage)
            if response is None:
                raise BrokerReplicaLifecycleError(
                    "replica V2 continuation has no terminal response"
                )
            return response
        finally:
            self._release_operation_gate(identity, gate)

    async def recover_incomplete(self) -> None:
        """Recover durable nonterminal work serially before listener startup."""
        for lifecycle in self._journal.incomplete_lifecycles():
            initial_request = self._journal.authenticated_initial_request(lifecycle)
            identity = self._identity(initial_request)
            gate = await self._acquire_operation_gate(identity)
            try:
                await self._recover_one(lifecycle, initial_request)
            finally:
                self._release_operation_gate(identity, gate)

    async def _recover_one(
        self,
        lifecycle: IncompleteReplicaLifecycleV2,
        initial_request: BrokerRequest,
    ) -> None:
        drain_request: BrokerRequest | None = None
        continuation: ReplicaDrainContinuation | None = None
        if lifecycle.drain_request_frame is not None:
            drain_request = self._journal.authenticated_drain_request(lifecycle)
            continuation = replica_drain_continuation_from_request(drain_request)
        if (
            lifecycle.paused
            and drain_request is None
            and lifecycle.owner_broker_incarnation == self._broker_incarnation
        ):
            return
        journal_request = (
            initial_request
            if lifecycle.paused and drain_request is None and lifecycle.stage == "drain"
            else self._stage_request(
                lifecycle.stage,
                initial_request=initial_request,
                drain_request=drain_request,
            )
        )
        if lifecycle.owner_broker_incarnation != self._broker_incarnation:
            if not lifecycle.stage_started and not (
                lifecycle.paused and lifecycle.stage == "drain"
            ):
                self._journal.begin_stage(
                    journal_request,
                    owner_token=lifecycle.owner_token,
                    stage=lifecycle.stage,
                )
            self._record_unresolved(
                journal_request,
                lifecycle.owner_token,
                lifecycle.stage,
                "broker_restart_unknown",
            )
            return
        context = self._context(
            initial_request=initial_request,
            journal_request=journal_request,
            authority=lifecycle.authority,
            owner_token=lifecycle.owner_token,
            drain_request=drain_request,
            drain_continuation=continuation,
        )
        if lifecycle.stage_started:
            await self._resume_started(context, journal_request, lifecycle.stage)
            return
        await self._drive_from(context, journal_request, lifecycle.stage)

    def _validate_request(
        self,
        request: BrokerRequest,
        request_frame: bytes,
        *,
        expected_type: str,
    ) -> None:
        if not isinstance(request, BrokerRequest):
            raise TypeError("replica request is invalid")
        if not isinstance(request_frame, bytes) or not request_frame:
            raise TypeError("replica request frame is invalid")
        if request.request_type != expected_type:
            raise ValueError("replica request type is invalid")
        if request.broker_incarnation != self._broker_incarnation:
            raise BrokerReplicaLifecycleError(
                "replica request targets a different broker incarnation"
            )

    @staticmethod
    def _identity(request: BrokerRequest) -> _Identity:
        return request.database_incarnation, request.operation_id

    @staticmethod
    def _stage_request(
        stage: str,
        *,
        initial_request: BrokerRequest,
        drain_request: BrokerRequest | None,
    ) -> BrokerRequest:
        if stage in _INITIAL_STAGES:
            return initial_request
        if stage in _CONTINUATION_STAGES and drain_request is not None:
            return drain_request
        raise BrokerReplicaLifecycleError("replica V2 continuation request is missing")

    def _context(
        self,
        *,
        initial_request: BrokerRequest,
        journal_request: BrokerRequest,
        authority: ReplicaAuthority,
        owner_token: str,
        drain_request: BrokerRequest | None,
        drain_continuation: ReplicaDrainContinuation | None,
    ) -> ReplicaLifecycleContextV2:
        receipts = self._journal.completed_receipts(journal_request)
        return ReplicaLifecycleContextV2(
            request=initial_request,
            authority=authority,
            owner_token=owner_token,
            completed_receipts=receipts,
            drain_request=drain_request,
            drain_continuation=drain_continuation,
        )

    async def _acquire_operation_gate(self, identity: _Identity) -> _OperationGate:
        gate = self._operation_gates.get(identity)
        if gate is None:
            if len(self._operation_gates) >= _MAX_OPERATION_GATES:
                raise BrokerReplicaLifecycleError("replica V2 operation capacity is exhausted")
            gate = _OperationGate()
            self._operation_gates[identity] = gate
        gate.user_count += 1
        acquired = False
        try:
            await gate.lock.acquire()
            acquired = True
            uncertain = self._active_uncertain_effects.get(identity)
            if uncertain is not None and not uncertain.done():
                try:
                    await asyncio.shield(uncertain)
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling():
                        raise
                except Exception:  # noqa: BLE001
                    uncertain.exception()
        except BaseException:
            if acquired:
                gate.lock.release()
            gate.user_count -= 1
            if gate.user_count == 0 and not gate.lock.locked():
                self._operation_gates.pop(identity, None)
            raise
        return gate

    def _release_operation_gate(self, identity: _Identity, gate: _OperationGate) -> None:
        current = self._operation_gates.get(identity)
        if current is not gate or not gate.lock.locked() or gate.user_count < 1:
            raise BrokerReplicaLifecycleError("replica V2 operation gate state is invalid")
        gate.lock.release()
        gate.user_count -= 1
        if gate.user_count == 0:
            self._operation_gates.pop(identity, None)

    async def _claim_within_deadline(
        self,
        request: BrokerRequest,
    ) -> ReplicaJournalV2Decision:
        deadline = time.monotonic() + self._owner_wait_seconds
        while True:
            try:
                return self._journal.claim_authority(
                    request,
                    owner_token=self._owner_token,
                    broker_incarnation=self._broker_incarnation,
                )
            except ReplicaJournalV2ConflictError:
                current = self._journal.initial_status(request)
                if current.response_frame is not None:
                    return current
                if time.monotonic() >= deadline:
                    raise BrokerReplicaOwnerActive(
                        "replica V2 lifecycle remains owned by another broker"
                    ) from None
                await asyncio.sleep(self._owner_poll_seconds)

    async def _drive_from(
        self,
        context: ReplicaLifecycleContextV2,
        journal_request: BrokerRequest,
        start_stage: str,
    ) -> bytes | None:
        phase = (
            _INITIAL_STAGES
            if journal_request.request_type == "replica.lifecycle"
            else _CONTINUATION_STAGES
        )
        if start_stage not in phase:
            raise ValueError("replica V2 start stage is invalid")
        first = phase.index(start_stage)
        for stage in phase[first:]:
            response = await self._apply_stage(context, journal_request, stage)
            if response is not None:
                return response
        return None

    async def _apply_stage(
        self,
        context: ReplicaLifecycleContextV2,
        journal_request: BrokerRequest,
        stage: str,
    ) -> bytes | None:
        self._journal.begin_stage(
            journal_request,
            owner_token=context.owner_token,
            stage=stage,
        )
        current_context = self._context(
            initial_request=context.request,
            journal_request=journal_request,
            authority=context.authority,
            owner_token=context.owner_token,
            drain_request=context.drain_request,
            drain_continuation=context.drain_continuation,
        )
        try:
            outcome = await self._attempt(
                current_context,
                journal_request,
                stage,
                reconcile=False,
            )
        except _StageTerminal as terminal:
            return terminal.frame
        if outcome.outcome == "completed":
            return self._persist_completed(
                current_context,
                journal_request,
                stage,
                outcome.receipt,
            )
        reason = outcome.reason or _UNEXPECTED_ERROR_REASONS[stage]
        return self._record_unresolved(
            journal_request,
            current_context.owner_token,
            stage,
            reason,
        )

    async def _resume_started(
        self,
        context: ReplicaLifecycleContextV2,
        journal_request: BrokerRequest,
        stage: str,
    ) -> bytes | None:
        current_context = self._context(
            initial_request=context.request,
            journal_request=journal_request,
            authority=context.authority,
            owner_token=context.owner_token,
            drain_request=context.drain_request,
            drain_continuation=context.drain_continuation,
        )
        try:
            outcome = await self._attempt(
                current_context,
                journal_request,
                stage,
                reconcile=True,
            )
        except _StageTerminal as terminal:
            return terminal.frame
        if outcome.outcome == "completed":
            response = self._persist_completed(
                current_context,
                journal_request,
                stage,
                outcome.receipt,
            )
            if response is not None:
                return response
            return await self._drive_from(
                current_context,
                journal_request,
                self._next_stage(stage),
            )
        if outcome.outcome == "not_applied":
            return await self._drive_from(current_context, journal_request, stage)
        return self._record_unresolved(
            journal_request,
            current_context.owner_token,
            stage,
            cast(str, outcome.reason),
        )

    async def _attempt(
        self,
        context: ReplicaLifecycleContextV2,
        journal_request: BrokerRequest,
        stage: str,
        *,
        reconcile: bool,
    ) -> StageReconciliation[object]:
        if len(self._abandoned_effects) >= _MAX_ABANDONED_EFFECTS:
            return StageReconciliation(outcome="unknown", reason="timeout_unknown")
        effect = self._effects[stage]
        try:
            invocation = effect.reconcile(context) if reconcile else effect.apply(context)
        except asyncio.CancelledError:
            self._record_unresolved(
                journal_request,
                context.owner_token,
                stage,
                "cancellation_unknown",
            )
            raise
        except StageRejected as rejected:
            raise _StageTerminal(
                self._record_rejected(
                    journal_request,
                    context.owner_token,
                    stage,
                    rejected,
                )
            ) from None
        except StageOutcomeUnknown as unknown:
            return self._unknown_outcome(stage, unknown.reason)
        except Exception:  # noqa: BLE001
            return StageReconciliation(
                outcome="unknown",
                reason=_UNEXPECTED_ERROR_REASONS[stage],
            )
        if not inspect.isawaitable(invocation):
            return StageReconciliation(
                outcome="unknown",
                reason=_UNEXPECTED_ERROR_REASONS[stage],
            )
        task = asyncio.ensure_future(cast(Awaitable[StageReconciliation[object]], invocation))
        deadline = time.monotonic() + self._effect_timeout_seconds
        try:
            done, _pending = await asyncio.wait(
                {task},
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except asyncio.CancelledError:
            self._abandon(task, self._identity(journal_request))
            self._record_unresolved(
                journal_request,
                context.owner_token,
                stage,
                "cancellation_unknown",
            )
            raise
        if task not in done:
            self._abandon(task, self._identity(journal_request))
            return StageReconciliation(outcome="unknown", reason="timeout_unknown")
        try:
            result = task.result()
        except asyncio.CancelledError:
            return StageReconciliation(outcome="unknown", reason="cancellation_unknown")
        except StageRejected as rejected:
            raise _StageTerminal(
                self._record_rejected(
                    journal_request,
                    context.owner_token,
                    stage,
                    rejected,
                )
            ) from None
        except StageOutcomeUnknown as unknown:
            return self._unknown_outcome(stage, unknown.reason)
        except Exception:  # noqa: BLE001
            return StageReconciliation(
                outcome="unknown",
                reason=_UNEXPECTED_ERROR_REASONS[stage],
            )
        if not isinstance(result, StageReconciliation):
            return StageReconciliation(
                outcome="unknown",
                reason=_UNEXPECTED_ERROR_REASONS[stage],
            )
        return result

    def _persist_completed(
        self,
        context: ReplicaLifecycleContextV2,
        journal_request: BrokerRequest,
        stage: str,
        receipt: object | None,
    ) -> bytes | None:
        if not isinstance(receipt, _EXPECTED_RECEIPT_TYPES[stage]):
            return self._record_unresolved(
                journal_request,
                context.owner_token,
                stage,
                _UNEXPECTED_ERROR_REASONS[stage],
            )
        response: bytes | None = None
        if stage == "admission":
            admission = cast(AdmissionReceipt, receipt)
            response = create_signed_response(
                journal_request,
                private_key=self._broker_private_key,
                status="ok",
                error=None,
                result={
                    "stage": "admission",
                    "state": "active",
                    "receipt_id": admission.receipt_id,
                },
            )
        elif stage == "reclaim":
            reclaim = cast(ReclaimReceipt, receipt)
            response = create_signed_response(
                journal_request,
                private_key=self._broker_private_key,
                status="ok",
                error=None,
                result={
                    "stage": "reclaim",
                    "state": "completed",
                    "receipt_id": reclaim.reclaim_receipt_id,
                },
            )
        try:
            if stage == "admission":
                self._journal.pause_after_admission(
                    journal_request,
                    owner_token=context.owner_token,
                    receipt=cast(AdmissionReceipt, receipt),
                    response_frame=cast(bytes, response),
                )
            else:
                self._journal.complete_stage(
                    journal_request,
                    owner_token=context.owner_token,
                    stage=stage,
                    receipt=receipt,
                    response_frame=response,
                )
        except ReplicaJournalV2CommitAbsent:
            return self._record_unresolved(
                journal_request,
                context.owner_token,
                stage,
                "sqlite_commit_unknown",
            )
        except (TypeError, ValueError):
            return self._record_unresolved(
                journal_request,
                context.owner_token,
                stage,
                _UNEXPECTED_ERROR_REASONS[stage],
            )
        return response

    def _record_rejected(
        self,
        request: BrokerRequest,
        owner_token: str,
        stage: str,
        rejected: StageRejected,
    ) -> bytes:
        if rejected.status not in _REJECTED_STATUSES[stage]:
            return self._record_unresolved(
                request,
                owner_token,
                stage,
                _UNEXPECTED_ERROR_REASONS[stage],
            )
        try:
            receipt = RejectedReceipt(receipt_id=rejected.receipt_id)
        except (TypeError, ValueError):
            return self._record_unresolved(
                request,
                owner_token,
                stage,
                _UNEXPECTED_ERROR_REASONS[stage],
            )
        response = create_signed_response(
            request,
            private_key=self._broker_private_key,
            status="error",
            error=rejected.status,
            result={
                "stage": stage,
                "state": "rejected",
                "code": rejected.status,
                "receipt_id": rejected.receipt_id,
            },
        )
        self._journal.reject_stage(
            request,
            owner_token=owner_token,
            stage=stage,
            status=rejected.status,
            receipt=receipt,
            response_frame=response,
        )
        return response

    def _record_unresolved(
        self,
        request: BrokerRequest,
        owner_token: str,
        stage: str,
        reason: str,
    ) -> bytes:
        if reason not in _BASE_UNRESOLVED_REASONS | _STAGE_UNRESOLVED_REASONS[stage]:
            reason = _UNEXPECTED_ERROR_REASONS[stage]
        response = create_signed_response(
            request,
            private_key=self._broker_private_key,
            status="error",
            error=reason,
            result={"stage": stage, "state": "unresolved", "code": reason},
        )
        self._journal.mark_stage_unresolved(
            request,
            owner_token=owner_token,
            stage=stage,
            reason=reason,
            response_frame=response,
        )
        self._active_uncertain_effects.pop(self._identity(request), None)
        return response

    def _abandon(
        self,
        task: asyncio.Future[StageReconciliation[object]],
        identity: _Identity,
    ) -> None:
        self._abandoned_effects.add(task)
        self._active_uncertain_effects[identity] = task
        task.add_done_callback(self._consume_abandoned)

    def _consume_abandoned(
        self,
        task: asyncio.Future[StageReconciliation[object]],
    ) -> None:
        self._abandoned_effects.discard(task)
        for identity, uncertain in tuple(self._active_uncertain_effects.items()):
            if uncertain is task:
                self._active_uncertain_effects.pop(identity, None)
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _next_stage(stage: str) -> str:
        index = REPLICA_LIFECYCLE_V2_STAGES.index(stage)
        if index + 1 >= len(REPLICA_LIFECYCLE_V2_STAGES):
            raise BrokerReplicaLifecycleError("replica V2 reclaim has no following stage")
        return REPLICA_LIFECYCLE_V2_STAGES[index + 1]
