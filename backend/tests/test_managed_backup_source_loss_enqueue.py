"""Backup manager owns explicit source-loss restore requests."""

from __future__ import annotations

from datetime import datetime, timezone


def test_enqueue_source_loss_restore_uses_dedicated_claim() -> None:
    """The manager should persist source loss through its configured claim boundary."""
    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    runtime = ManagedRuntimeStatus(
        user_id="user-1",
        provider_name="fly_sprites",
        sprite_name="source",
        runner_id="runner",
        lifecycle_status="ready",
        generation=4,
        artifact_version="runner-v1",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        last_error=None,
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=4,
        status="ready",
        object_key="managed/archive.enc",
        object_version="version-1",
        size_bytes=10,
        sha256="d" * 64,
        wrapped_key=b"key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at=now.isoformat(),
        completed_at=now.isoformat(),
        last_error=None,
    )
    calls: list[dict[str, object]] = []

    def reject_normal_restore(*_args, **_values):
        raise AssertionError("normal restore claim must not be used")

    def start_source_loss(user_id: str, **values):
        calls.append({"user_id": user_id, **values})
        return ManagedBackupOperation(
            user_id=user_id,
            job_id=str(values["job_id"]),
            archive_id=str(values["archive_id"]),
            operation="restore",
            status="running",
            runtime_generation=int(values["runtime_generation"]),
            started_at=now.isoformat(),
            updated_at=now.isoformat(),
            last_error=None,
            source_lost=True,
        )

    manager = ManagedBackupManager(
        get_runtime=lambda _user_id: runtime,
        get_archive=lambda _user_id, _archive_id: archive,
        start_restore=reject_normal_restore,
        start_source_loss_restore=start_source_loss,
        new_id=lambda: "job-1",
        now=lambda: now,
    )

    operation = manager.enqueue_source_loss_restore("user-1", "archive-1")

    assert operation.source_lost is True
    assert calls[0]["runtime_generation"] == 4
