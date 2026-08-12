"""Retention cadence tests for managed backup coordination."""

from __future__ import annotations

from datetime import datetime, timezone


def test_manager_throttles_retention_scans_between_idle_cycles() -> None:
    """Repeated scheduling at one time should perform one retention scan."""
    from yinshi.services.managed_backup_manager import ManagedBackupManager

    scans = 0

    def list_retention(**_values):
        nonlocal scans
        scans += 1
        return ()

    manager = ManagedBackupManager(
        list_retention=list_retention,
        now=lambda: datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
    )

    manager.schedule_retention()
    manager.schedule_retention()

    assert scans == 1
