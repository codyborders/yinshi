"""Coordinate broker-owned replica lifecycles through durable stage records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from yinshi.services.broker_protocol import BrokerRequest
from yinshi.services.broker_replica_journal import ReplicaAuthority

REPLICA_LIFECYCLE_STAGES: tuple[str, ...] = (
    "ingest",
    "verify",
    "publish",
    "admission",
    "drain",
    "export",
    "reclaim",
)
ReceiptT_co = TypeVar("ReceiptT_co", covariant=True)


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
