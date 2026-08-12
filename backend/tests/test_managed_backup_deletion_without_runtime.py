"""Managed archive deletion tests without a live guest runtime."""

from __future__ import annotations

from datetime import datetime, timezone


def test_archive_deletion_does_not_require_live_runtime(auth_client) -> None:
    """Exact object deletion should remain available after runtime loss."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import (
        ManagedBackupArchive,
        start_managed_backup_deletion,
    )

    tenant = getattr(auth_client, "yinshi_tenant")
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    archive = ManagedBackupArchive(
        id="archive-orphan",
        user_id=tenant.user_id,
        runtime_generation=4,
        status="ready",
        object_key="managed/v1/orphan.enc",
        object_version="version-2",
        size_bytes=1024,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at=now.isoformat(),
        completed_at=now.isoformat(),
        last_error=None,
    )
    with get_control_db() as database:
        database.execute(
            """INSERT INTO managed_backup_archives (
                   id, user_id, runtime_generation, status, object_key,
                   object_version, size_bytes, sha256, wrapped_key, key_id,
                   owner_digest, created_at, completed_at
               ) VALUES (?, ?, 4, 'ready', ?, 'version-2', 1024, ?, ?, ?, ?, ?, ?)""",
            (
                archive.id,
                tenant.user_id,
                archive.object_key,
                archive.sha256,
                archive.wrapped_key,
                archive.key_id,
                archive.owner_digest,
                archive.created_at,
                archive.completed_at,
            ),
        )
        database.commit()
    manager = ManagedBackupManager(
        get_runtime=lambda _user_id: None,
        get_archive=lambda _user_id, _archive_id: archive,
        start_deletion=start_managed_backup_deletion,
        now=lambda: now,
        new_id=lambda: "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e98",
    )

    operation = manager.enqueue_delete(tenant.user_id, archive.id)

    assert operation.operation == "delete"
