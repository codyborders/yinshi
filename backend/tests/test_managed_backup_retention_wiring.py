"""Managed backup retention wiring checks."""

from __future__ import annotations

from datetime import datetime, timezone


def test_retention_uses_manager_durable_delete_without_extra_wiring() -> None:
    """Production manager should route old archives through its deletion catalog."""
    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive

    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=3,
        status="ready",
        object_key="managed/archive.enc",
        object_version="version-1",
        size_bytes=1,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at="2026-06-01T00:00:00Z",
        completed_at="2026-06-01T00:01:00Z",
        last_error=None,
    )
    queued: list[dict[str, object]] = []

    def start_deletion(_user_id: str, **values):
        queued.append(values)
        return values["job_id"]

    manager = ManagedBackupManager(
        start_deletion=start_deletion,
        get_runtime=lambda _user_id: None,
        get_archive=lambda _user_id, _archive_id: archive,
        list_retention=lambda **_values: (archive,),
        now=lambda: now,
        new_id=lambda: "job-1",
    )

    assert manager.schedule_retention() == 1
    assert queued == [
        {
            "archive_id": "archive-1",
            "runtime_generation": 3,
            "job_id": "job-1",
            "now": now,
        }
    ]
