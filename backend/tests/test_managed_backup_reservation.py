"""Managed backup reservations exist before operations become runnable."""

from __future__ import annotations

from yinshi.services.managed_backup_manager import ManagedBackupManager


def test_reservation_contains_exact_unpublished_object_identity() -> None:
    """A caller can arm exact storage behavior before publishing a backup claim."""
    manager = ManagedBackupManager(
        new_id=iter(("archive-1", "job-1")).__next__,
        object_prefix="managed",
    )

    reservation = manager.reserve_create()

    assert reservation.archive_id == "archive-1"
    assert reservation.job_id == "job-1"
    assert reservation.object_key == "managed/archive-1.enc"
