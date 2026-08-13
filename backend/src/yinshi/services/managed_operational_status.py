"""Sanitized operational findings for managed runtime monitoring."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any


class ManagedAlertClass(str, Enum):
    """Stable alert identifiers consumed by external monitoring."""

    BACKUP_STALE = "managed_backup_stale"
    OPERATION_STUCK = "managed_operation_stuck"
    OPERATION_LEASE_EXPIRED = "managed_operation_lease_expired"
    RESTORE_FAILED = "managed_restore_failed"
    SPRITE_RECONCILIATION_FAILED = "managed_sprite_reconciliation_failed"
    STORAGE_PREFLIGHT_FAILED = "managed_storage_preflight_failed"
    DELETION_FAILED = "managed_deletion_failed"


@dataclass(frozen=True, slots=True)
class ManagedOperationalAlert:
    """One aggregated finding with no resource identifiers."""

    alert_class: ManagedAlertClass
    count: int
    oldest_age_seconds: int

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("alert count must be positive")
        if self.oldest_age_seconds < 0:
            raise ValueError("alert age must not be negative")

    def to_dict(self) -> dict[str, int | str]:
        """Return bounded public monitoring fields."""
        return {
            "alert_class": self.alert_class.value,
            "count": self.count,
            "oldest_age_seconds": self.oldest_age_seconds,
        }


@dataclass(frozen=True, slots=True)
class ManagedOperationalStatus:
    """Aggregated status returned by monitoring commands."""

    generated_at: str
    alerts: tuple[ManagedOperationalAlert, ...]

    @property
    def critical(self) -> bool:
        """Return whether monitoring must alert."""
        return bool(self.alerts)

    def to_dict(self) -> dict[str, Any]:
        """Serialize only sanitized aggregate data."""
        return {
            "schema_version": 1,
            "generated_at": self.generated_at,
            "status": "critical" if self.critical else "ok",
            "alerts": [alert.to_dict() for alert in self.alerts],
        }


def _parse_timestamp(value: object, *, now: datetime) -> datetime:
    """Parse one database timestamp into UTC or mark it maximally stale."""
    if not isinstance(value, str) or not value.strip():
        return datetime.min.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return min(parsed.astimezone(UTC), now)


def _age_seconds(timestamp: datetime, *, now: datetime) -> int:
    """Return a nonnegative bounded age."""
    seconds = int((now - timestamp).total_seconds())
    return min(max(seconds, 0), 2_147_483_647)


def _append_alert(
    alerts: list[ManagedOperationalAlert],
    alert_class: ManagedAlertClass,
    timestamps: list[object],
    *,
    now: datetime,
) -> None:
    """Append one finding when matching records exist."""
    if not timestamps:
        return
    oldest = min(_parse_timestamp(value, now=now) for value in timestamps)
    alerts.append(
        ManagedOperationalAlert(
            alert_class=alert_class,
            count=len(timestamps),
            oldest_age_seconds=_age_seconds(oldest, now=now),
        )
    )


def collect_managed_operational_status(
    database: sqlite3.Connection,
    *,
    now: datetime,
    backup_stale_seconds: int,
    operation_stuck_seconds: int,
) -> ManagedOperationalStatus:
    """Read control state and return sanitized critical findings."""
    if now.tzinfo is None:
        raise ValueError("now must include timezone information")
    if backup_stale_seconds < 60 or operation_stuck_seconds < 60:
        raise ValueError("operational thresholds must be at least 60 seconds")
    now = now.astimezone(UTC)
    backup_cutoff = (now - timedelta(seconds=backup_stale_seconds)).isoformat()
    operation_cutoff = (now - timedelta(seconds=operation_stuck_seconds)).isoformat()
    alerts: list[ManagedOperationalAlert] = []

    stale_backup_rows = database.execute(
        """SELECT MAX(archive.completed_at) AS finding_at
           FROM managed_runtimes AS runtime
           LEFT JOIN managed_backup_archives AS archive
             ON archive.user_id = runtime.user_id AND archive.status = ?
           WHERE runtime.lifecycle_status = ?
           GROUP BY runtime.user_id
           HAVING finding_at IS NULL OR finding_at < ?""",
        ("ready", "ready", backup_cutoff),
    ).fetchall()
    _append_alert(
        alerts,
        ManagedAlertClass.BACKUP_STALE,
        [row["finding_at"] for row in stale_backup_rows],
        now=now,
    )

    stuck_rows = database.execute(
        """SELECT updated_at FROM managed_backup_operations
           WHERE status = ? AND updated_at < ?""",
        ("running", operation_cutoff),
    ).fetchall()
    _append_alert(
        alerts,
        ManagedAlertClass.OPERATION_STUCK,
        [row["updated_at"] for row in stuck_rows],
        now=now,
    )

    expired_lease_rows = database.execute(
        """SELECT lease_expires_at FROM managed_backup_operations
           WHERE status = ? AND lease_token IS NOT NULL AND lease_expires_at <= ?""",
        ("running", now.isoformat()),
    ).fetchall()
    _append_alert(
        alerts,
        ManagedAlertClass.OPERATION_LEASE_EXPIRED,
        [row["lease_expires_at"] for row in expired_lease_rows],
        now=now,
    )

    restore_rows = database.execute(
        """SELECT updated_at FROM managed_backup_operations
           WHERE failure_class = ? AND status = ?""",
        ("restore_failed", "failed"),
    ).fetchall()
    _append_alert(
        alerts,
        ManagedAlertClass.RESTORE_FAILED,
        [row["updated_at"] for row in restore_rows],
        now=now,
    )

    deletion_rows = database.execute(
        """SELECT updated_at AS finding_at FROM managed_backup_operations
           WHERE failure_class = ? AND status = ?""",
        ("deletion_failed", "failed"),
    ).fetchall()
    _append_alert(
        alerts,
        ManagedAlertClass.DELETION_FAILED,
        [row["finding_at"] for row in deletion_rows],
        now=now,
    )

    alerts.sort(key=lambda alert: alert.alert_class.value)
    return ManagedOperationalStatus(
        generated_at=now.isoformat().replace("+00:00", "Z"),
        alerts=tuple(alerts),
    )
