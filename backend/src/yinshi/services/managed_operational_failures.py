"""Durable active failures used by managed operational monitoring."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from yinshi.db import get_control_db


class ManagedPersistentAlertClass(str, Enum):
    """Service failures approved for durable operational state."""

    SPRITE_RECONCILIATION_FAILED = "managed_sprite_reconciliation_failed"
    STORAGE_PREFLIGHT_FAILED = "managed_storage_preflight_failed"


def _validate_alert_class(
    alert_class: ManagedPersistentAlertClass,
) -> ManagedPersistentAlertClass:
    if not isinstance(alert_class, ManagedPersistentAlertClass):
        raise ValueError("alert class is not approved for durable monitoring")
    return alert_class


def _format_utc(timestamp: datetime) -> str:
    if timestamp.tzinfo is None:
        raise ValueError("failure timestamp must include timezone information")
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def record_managed_operational_failure(
    alert_class: ManagedPersistentAlertClass,
    *,
    now: datetime | None = None,
) -> None:
    """Record one active service failure without storing provider details."""
    approved_class = _validate_alert_class(alert_class)
    timestamp = _format_utc(now or datetime.now(UTC))
    with get_control_db() as database:
        database.execute(
            """INSERT INTO managed_operational_failures (
                   alert_class, first_seen_at, last_seen_at
               ) VALUES (?, ?, ?)
               ON CONFLICT(alert_class) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
            (approved_class.value, timestamp, timestamp),
        )
        database.commit()


def clear_managed_operational_failure(
    alert_class: ManagedPersistentAlertClass,
) -> None:
    """Clear one active service failure after a complete successful operation."""
    approved_class = _validate_alert_class(alert_class)
    with get_control_db() as database:
        database.execute(
            "DELETE FROM managed_operational_failures WHERE alert_class = ?",
            (approved_class.value,),
        )
        database.commit()
