"""Centralized thread delegation status and transition policy.

Pure domain policy for the thread orchestration layer. This module holds the
delegation status vocabulary and the legal transitions between statuses. It
deliberately performs no I/O so every caller shares one definition of the
Phase 3 state machine from the thread orchestration plan.
"""

from __future__ import annotations

import uuid

DELEGATION_STATUS_PROVISIONING = "provisioning"
DELEGATION_STATUS_QUEUED = "queued"
DELEGATION_STATUS_RUNNING = "running"
DELEGATION_STATUS_CANCELLING = "cancelling"
DELEGATION_STATUS_COMPLETED = "completed"
DELEGATION_STATUS_FAILED = "failed"
DELEGATION_STATUS_CANCELLED = "cancelled"
DELEGATION_STATUS_INTERRUPTED = "interrupted"

TERMINAL_DELEGATION_STATUSES = frozenset(
    {
        DELEGATION_STATUS_COMPLETED,
        DELEGATION_STATUS_FAILED,
        DELEGATION_STATUS_CANCELLED,
        DELEGATION_STATUS_INTERRUPTED,
    }
)


def is_terminal_delegation_status(status: str) -> bool:
    """Return whether one delegation status can never change again."""
    return status in TERMINAL_DELEGATION_STATUSES


def cancellation_target(status: str) -> str:
    """Return the stable resulting status when cancellation is requested."""
    targets = {
        DELEGATION_STATUS_PROVISIONING: DELEGATION_STATUS_CANCELLED,
        DELEGATION_STATUS_QUEUED: DELEGATION_STATUS_CANCELLED,
        DELEGATION_STATUS_RUNNING: DELEGATION_STATUS_CANCELLING,
        DELEGATION_STATUS_CANCELLING: DELEGATION_STATUS_CANCELLING,
        DELEGATION_STATUS_COMPLETED: DELEGATION_STATUS_COMPLETED,
        DELEGATION_STATUS_FAILED: DELEGATION_STATUS_FAILED,
        DELEGATION_STATUS_CANCELLED: DELEGATION_STATUS_CANCELLED,
        DELEGATION_STATUS_INTERRUPTED: DELEGATION_STATUS_INTERRUPTED,
    }
    return targets[status]


def initial_run_idempotency_key(delegation_id: str) -> str:
    """Derive the deterministic initial prompt-run idempotency key."""
    normalized = str(delegation_id).strip().lower()
    if len(normalized) != 32 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("delegation_id must be 32 lowercase hexadecimal characters")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"yinshi:thread-initial-run:{normalized}"))
