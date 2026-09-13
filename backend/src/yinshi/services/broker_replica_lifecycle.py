"""Coordinate broker-owned replica lifecycles through durable stage records."""

from __future__ import annotations

import asyncio
import inspect
import re
import secrets
import time
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from yinshi.services.broker_protocol import BrokerRequest, create_signed_response
from yinshi.services.broker_replica_journal import (
    AdmissionReceipt,
    BrokerReplicaJournal,
    DrainReceipt,
    ExportReceipt,
    IncompleteReplicaLifecycle,
    IngestReceipt,
    PublishReceipt,
    ReclaimReceipt,
    RejectedReceipt,
    ReplicaAuthority,
    ReplicaJournalConflictError,
    ReplicaJournalDecision,
    ReplicaJournalEffectResultError,
    VerifyReceipt,
)

REPLICA_LIFECYCLE_STAGES: tuple[str, ...] = (
    "ingest",
    "verify",
    "publish",
    "admission",
    "drain",
    "export",
    "reclaim",
)
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

_UNEXPECTED_ERROR_REASONS: dict[str, str] = {
    "ingest": "transfer_unknown",
    "verify": "verification_unknown",
    "publish": "publication_unknown",
    "admission": "admission_unknown",
    "drain": "acknowledgment_unknown",
    "export": "transfer_unknown",
    "reclaim": "effect_unknown",
}


class BrokerReplicaLifecycleError(RuntimeError):
    """Base class for replica lifecycle coordination failures."""


class BrokerReplicaOwnerActive(BrokerReplicaLifecycleError):
    """Report another owner that remains active past the bounded wait."""


class StageRejected(BrokerReplicaLifecycleError):
    """Report an explicit external rejection for one stage."""

    def __init__(self, status: str, receipt_id: str) -> None:
        super().__init__(f"replica stage rejected: {status}")
        self.status = status
        self.receipt_id = receipt_id


class StageOutcomeUnknown(BrokerReplicaLifecycleError):
    """Report an external stage whose outcome cannot be confirmed."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"replica stage outcome unknown: {reason}")
        self.reason = reason


class _StageTerminal(Exception):
    """Carry a recorded terminal response through internal stage control."""

    def __init__(self, frame: bytes) -> None:
        super().__init__("replica stage reached a terminal state")
        self.frame = frame


@dataclass(frozen=True, slots=True)
class StageReconciliation(Generic[ReceiptT_co]):
    """Represent a completed, confirmed absent, or unknown stage effect."""

    outcome: str
    receipt: ReceiptT_co | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"completed", "not_applied", "unknown"}:
            raise ValueError("replica stage outcome is invalid")
        if self.outcome == "completed":
            if self.receipt is None:
                raise ValueError("replica completed outcome requires a receipt")
            if self.reason is not None:
                raise ValueError("replica completed outcome must not carry a reason")
            return
        if self.receipt is not None:
            raise ValueError("only a completed outcome may carry a receipt")
        if self.outcome == "unknown":
            if not isinstance(self.reason, str) or not self.reason:
                raise ValueError("replica unknown outcome requires a reason")
            return
        if self.reason is not None:
            raise ValueError("replica not-applied outcome must not carry a reason")


@dataclass(frozen=True, slots=True)
class ReplicaLifecycleContext:
    """Hold immutable inputs for one replica stage effect."""

    request: BrokerRequest
    authority: ReplicaAuthority
    owner_token: str


class ReplicaStageEffect(Protocol[ReceiptT_co]):
    """Apply or inspect one named replica stage effect."""

    async def apply(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[ReceiptT_co]:
        """Apply the stage effect."""
        ...

    async def reconcile(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[ReceiptT_co]:
        """Inspect a stage that started before broker recovery."""
        ...


@dataclass(frozen=True, slots=True)
class ReplicaLifecycleEffects:
    """Bind each lifecycle stage to its matching receipt contract."""

    ingest: ReplicaStageEffect[IngestReceipt]
    verify: ReplicaStageEffect[VerifyReceipt]
    publish: ReplicaStageEffect[PublishReceipt]
    admission: ReplicaStageEffect[AdmissionReceipt]
    drain: ReplicaStageEffect[DrainReceipt]
    export: ReplicaStageEffect[ExportReceipt]
    reclaim: ReplicaStageEffect[ReclaimReceipt]


@dataclass(slots=True)
class _OperationGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    user_count: int = 0


class BrokerReplicaLifecycleCoordinator:
    """Drive one replica through the journal-owned fixed stage order."""

    def __init__(
        self,
        journal: BrokerReplicaJournal,
        *,
        broker_incarnation: str,
        broker_private_keys: Mapping[str, Ed25519PrivateKey],
        effects: ReplicaLifecycleEffects,
        effect_timeout_seconds: float = _EFFECT_TIMEOUT_SECONDS_DEFAULT,
        owner_wait_seconds: float = _OWNER_WAIT_SECONDS_DEFAULT,
        owner_poll_seconds: float = _OWNER_POLL_SECONDS_DEFAULT,
    ) -> None:
        if not isinstance(journal, BrokerReplicaJournal):
            raise TypeError("replica journal is invalid")
        if not isinstance(broker_private_keys, Mapping) or not broker_private_keys:
            raise TypeError("replica broker private keyring is invalid")
        if (
            not isinstance(broker_incarnation, str)
            or _TOKEN_PATTERN.fullmatch(broker_incarnation) is None
        ):
            raise ValueError("replica broker incarnation is invalid")
        signer_keys = dict(broker_private_keys)
        if broker_incarnation not in signer_keys:
            raise ValueError("replica current broker signer is missing")
        for incarnation, private_key in signer_keys.items():
            journal.require_response_signer(incarnation, private_key)
        if not isinstance(effects, ReplicaLifecycleEffects):
            raise TypeError("replica stage effects are invalid")
        configured_effects: dict[str, ReplicaStageEffect[object]] = {
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
                raise TypeError(f"replica {stage} apply effect is invalid")
            if not callable(getattr(effect, "reconcile", None)):
                raise TypeError(f"replica {stage} reconcile effect is invalid")
        self._journal = journal
        self._broker_incarnation = broker_incarnation
        self._broker_private_keys = signer_keys
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
        self._active_uncertain_effects: dict[str, asyncio.Future[StageReconciliation[object]]] = {}
        self._operation_gates: dict[str, _OperationGate] = {}

    @staticmethod
    def _bounded_seconds(value: float, *, maximum: float, description: str) -> float:
        if type(value) not in {int, float} or not 0 < value <= maximum:
            raise ValueError(f"replica {description} must be a positive bounded number")
        return float(value)

    async def run(
        self,
        request: BrokerRequest,
        request_frame: bytes,
        authority: ReplicaAuthority,
    ) -> bytes:
        """Accept, claim, and execute one lifecycle to a terminal response."""
        if not isinstance(request, BrokerRequest):
            raise TypeError("replica request is invalid")
        if not isinstance(request_frame, bytes) or not request_frame:
            raise TypeError("replica request frame is invalid")
        if not isinstance(authority, ReplicaAuthority):
            raise TypeError("replica authority is invalid")
        if request.broker_incarnation != self._broker_incarnation:
            raise BrokerReplicaLifecycleError(
                "replica request targets a different broker incarnation"
            )
        gate = await self._acquire_operation_gate(request.operation_id)
        try:
            decision = self._journal.accept(request, request_frame, authority)
            if decision.response_frame is not None:
                return decision.response_frame
            decision = await self._claim_within_deadline(request)
            if decision.response_frame is not None:
                return decision.response_frame
            if decision.stage not in REPLICA_LIFECYCLE_STAGES:
                raise BrokerReplicaLifecycleError("replica lifecycle has no active stage")
            context = ReplicaLifecycleContext(
                request=request,
                authority=authority,
                owner_token=self._owner_token,
            )
            if decision.state == "in_flight":
                response = await self._resume_started(context, decision.stage)
            else:
                response = await self._drive_from(context, decision.stage)
            if response is None:
                raise BrokerReplicaLifecycleError("replica lifecycle has no terminal response")
            return response
        finally:
            self._release_operation_gate(request.operation_id, gate)

    async def recover_incomplete(self) -> None:
        """Recover incomplete lifecycles serially before serving requests."""
        for lifecycle in self._journal.incomplete_lifecycles():
            await self._recover_one(lifecycle)

    async def _recover_one(self, lifecycle: IncompleteReplicaLifecycle) -> None:
        request = self._journal.authenticated_request(lifecycle)
        if lifecycle.owner_broker_incarnation != self._broker_incarnation:
            if not lifecycle.stage_started:
                self._journal.begin_stage(
                    request,
                    owner_token=lifecycle.owner_token,
                    stage=lifecycle.stage,
                )
            self._record_unresolved(
                request,
                lifecycle.owner_token,
                lifecycle.stage,
                "broker_restart_unknown",
            )
            return
        context = ReplicaLifecycleContext(
            request=request,
            authority=lifecycle.authority,
            owner_token=lifecycle.owner_token,
        )
        if not lifecycle.stage_started:
            await self._drive_from(context, lifecycle.stage)
            return
        await self._resume_started(context, lifecycle.stage)

    async def _resume_started(
        self,
        context: ReplicaLifecycleContext,
        stage: str,
    ) -> bytes | None:
        try:
            outcome = await self._attempt(context, stage, reconcile=True)
        except _StageTerminal as terminal:
            return terminal.frame
        if outcome.outcome == "completed":
            response = self._persist_completed(context, stage, outcome.receipt)
            if response is not None:
                return response
            return await self._drive_from(context, self._next_stage(stage))
        if outcome.outcome == "not_applied":
            return await self._drive_from(context, stage)
        return self._record_unresolved(
            context.request,
            context.owner_token,
            stage,
            cast(str, outcome.reason),
        )

    async def _acquire_operation_gate(self, operation_id: str) -> _OperationGate:
        gate = self._operation_gates.get(operation_id)
        if gate is None:
            if len(self._operation_gates) >= _MAX_OPERATION_GATES:
                raise BrokerReplicaLifecycleError("replica operation capacity is exhausted")
            gate = _OperationGate()
            self._operation_gates[operation_id] = gate
        gate.user_count += 1
        acquired = False
        try:
            await gate.lock.acquire()
            acquired = True
            uncertain = self._active_uncertain_effects.get(operation_id)
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
                self._operation_gates.pop(operation_id, None)
            raise
        return gate

    def _release_operation_gate(self, operation_id: str, gate: _OperationGate) -> None:
        current = self._operation_gates.get(operation_id)
        if current is not gate or not gate.lock.locked() or gate.user_count < 1:
            raise BrokerReplicaLifecycleError("replica operation gate state is invalid")
        gate.lock.release()
        gate.user_count -= 1
        if gate.user_count == 0:
            self._operation_gates.pop(operation_id, None)

    async def _claim_within_deadline(
        self,
        request: BrokerRequest,
    ) -> ReplicaJournalDecision:
        deadline = time.monotonic() + self._owner_wait_seconds
        while True:
            try:
                return self._journal.claim_authority(
                    request,
                    owner_token=self._owner_token,
                    broker_incarnation=self._broker_incarnation,
                )
            except ReplicaJournalConflictError:
                current = self._journal.status(request)
                if current.response_frame is not None:
                    return current
                if time.monotonic() >= deadline:
                    raise BrokerReplicaOwnerActive(
                        "replica lifecycle remains owned by another broker"
                    ) from None
                await asyncio.sleep(self._owner_poll_seconds)

    async def _drive_from(
        self,
        context: ReplicaLifecycleContext,
        start_stage: str,
    ) -> bytes | None:
        if start_stage not in REPLICA_LIFECYCLE_STAGES:
            raise ValueError("replica start stage is invalid")
        first = REPLICA_LIFECYCLE_STAGES.index(start_stage)
        for stage in REPLICA_LIFECYCLE_STAGES[first:]:
            response = await self._apply_stage(context, stage)
            if response is not None:
                return response
        return None

    async def _apply_stage(
        self,
        context: ReplicaLifecycleContext,
        stage: str,
    ) -> bytes | None:
        self._journal.begin_stage(
            context.request,
            owner_token=context.owner_token,
            stage=stage,
        )
        try:
            outcome = await self._attempt(context, stage, reconcile=False)
        except _StageTerminal as terminal:
            return terminal.frame
        if outcome.outcome == "completed":
            return self._persist_completed(context, stage, outcome.receipt)
        reason = outcome.reason or _UNEXPECTED_ERROR_REASONS[stage]
        return self._record_unresolved(
            context.request,
            context.owner_token,
            stage,
            reason,
        )

    async def _attempt(
        self,
        context: ReplicaLifecycleContext,
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
            return StageReconciliation(
                outcome="unknown",
                reason="cancellation_unknown",
            )
        except StageRejected as rejected:
            raise _StageTerminal(
                self._record_rejected(
                    context.request,
                    context.owner_token,
                    stage,
                    rejected,
                )
            ) from None
        except StageOutcomeUnknown as unknown:
            return StageReconciliation(outcome="unknown", reason=unknown.reason)
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
            self._abandon(task, context.request.operation_id)
            self._record_unresolved(
                context.request,
                context.owner_token,
                stage,
                "cancellation_unknown",
            )
            raise
        if task not in done:
            self._abandon(task, context.request.operation_id)
            return StageReconciliation(outcome="unknown", reason="timeout_unknown")
        try:
            result = task.result()
        except asyncio.CancelledError:
            return StageReconciliation(outcome="unknown", reason="cancellation_unknown")
        except StageRejected as rejected:
            raise _StageTerminal(
                self._record_rejected(
                    context.request,
                    context.owner_token,
                    stage,
                    rejected,
                )
            ) from None
        except StageOutcomeUnknown as unknown:
            return StageReconciliation(outcome="unknown", reason=unknown.reason)
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
        context: ReplicaLifecycleContext,
        stage: str,
        receipt: object | None,
    ) -> bytes | None:
        expected_types: dict[str, type[object]] = {
            "ingest": IngestReceipt,
            "verify": VerifyReceipt,
            "publish": PublishReceipt,
            "admission": AdmissionReceipt,
            "drain": DrainReceipt,
            "export": ExportReceipt,
            "reclaim": ReclaimReceipt,
        }
        if not isinstance(receipt, expected_types[stage]):
            return self._record_unresolved(
                context.request,
                context.owner_token,
                stage,
                _UNEXPECTED_ERROR_REASONS[stage],
            )
        try:
            self._journal.validate_stage_receipt(
                context.request,
                context.authority,
                stage,
                receipt,
            )
        except ReplicaJournalEffectResultError:
            return self._record_unresolved(
                context.request,
                context.owner_token,
                stage,
                _UNEXPECTED_ERROR_REASONS[stage],
            )
        if stage == "reclaim":
            reclaim_receipt = cast(ReclaimReceipt, receipt)
            response = create_signed_response(
                context.request,
                private_key=self._signing_key(context.request),
                status="ok",
                error=None,
                result={
                    "stage": "reclaim",
                    "state": "completed",
                    "receipt_id": reclaim_receipt.reclaim_receipt_id,
                },
            )
            self._journal.complete_reclaim(
                context.request,
                owner_token=context.owner_token,
                receipt=reclaim_receipt,
                response_frame=response,
            )
            return response
        if stage == "ingest":
            self._journal.complete_ingest(
                context.request,
                owner_token=context.owner_token,
                receipt=cast(IngestReceipt, receipt),
            )
        elif stage == "verify":
            self._journal.complete_verify(
                context.request,
                owner_token=context.owner_token,
                receipt=cast(VerifyReceipt, receipt),
            )
        elif stage == "publish":
            self._journal.complete_publish(
                context.request,
                owner_token=context.owner_token,
                receipt=cast(PublishReceipt, receipt),
            )
        elif stage == "admission":
            self._journal.complete_admission(
                context.request,
                owner_token=context.owner_token,
                receipt=cast(AdmissionReceipt, receipt),
            )
        elif stage == "drain":
            self._journal.complete_drain(
                context.request,
                owner_token=context.owner_token,
                receipt=cast(DrainReceipt, receipt),
            )
        elif stage == "export":
            self._journal.complete_export(
                context.request,
                owner_token=context.owner_token,
                receipt=cast(ExportReceipt, receipt),
            )
        else:
            raise BrokerReplicaLifecycleError("replica completion stage is invalid")
        return None

    def _signing_key(self, request: BrokerRequest) -> Ed25519PrivateKey:
        private_key = self._broker_private_keys.get(request.broker_incarnation)
        if private_key is None:
            raise BrokerReplicaLifecycleError("replica historical broker signer is unavailable")
        return private_key

    def _record_rejected(
        self,
        request: BrokerRequest,
        owner_token: str,
        stage: str,
        rejected: StageRejected,
    ) -> bytes:
        try:
            receipt = RejectedReceipt(receipt_id=rejected.receipt_id)
            self._journal.validate_rejection(stage, rejected.status, receipt)
        except (ReplicaJournalEffectResultError, TypeError, ValueError):
            return self._record_unresolved(
                request,
                owner_token,
                stage,
                _UNEXPECTED_ERROR_REASONS[stage],
            )
        response = create_signed_response(
            request,
            private_key=self._signing_key(request),
            status="error",
            error=rejected.status,
            result={
                "stage": stage,
                "state": "rejected",
                "code": rejected.status,
                "receipt_id": rejected.receipt_id,
            },
        )
        self._journal.record_rejected(
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
        try:
            self._journal.validate_unresolved_reason(stage, reason)
        except ReplicaJournalEffectResultError:
            reason = _UNEXPECTED_ERROR_REASONS[stage]
            self._journal.validate_unresolved_reason(stage, reason)
        response = create_signed_response(
            request,
            private_key=self._signing_key(request),
            status="error",
            error=reason,
            result={"stage": stage, "state": "unresolved", "code": reason},
        )
        self._journal.record_unresolved(
            request,
            owner_token=owner_token,
            stage=stage,
            reason=reason,
            response_frame=response,
        )
        self._active_uncertain_effects.pop(request.operation_id, None)
        return response

    def _abandon(
        self,
        task: asyncio.Future[StageReconciliation[object]],
        operation_id: str,
    ) -> None:
        self._abandoned_effects.add(task)
        self._active_uncertain_effects[operation_id] = task
        task.add_done_callback(self._consume_abandoned)

    def _consume_abandoned(self, task: asyncio.Future[StageReconciliation[object]]) -> None:
        self._abandoned_effects.discard(task)
        for operation_id, uncertain in tuple(self._active_uncertain_effects.items()):
            if uncertain is task:
                self._active_uncertain_effects.pop(operation_id, None)
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _next_stage(stage: str) -> str:
        index = REPLICA_LIFECYCLE_STAGES.index(stage)
        if index + 1 >= len(REPLICA_LIFECYCLE_STAGES):
            raise BrokerReplicaLifecycleError("reclaim has no following stage")
        return REPLICA_LIFECYCLE_STAGES[index + 1]
