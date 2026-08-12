"""Deletion tests for managed backup coordination."""

from __future__ import annotations

from datetime import datetime, timezone


def test_manager_enqueues_delete_with_server_runtime_authority() -> None:
    """Delete requests should resolve exact tenant version and generation internally."""
    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    captured: dict[str, object] = {}
    runtime = ManagedRuntimeStatus(
        user_id="user-1",
        runner_id="runner-1",
        provider_name="fly_sprites",
        sprite_name="sprite-1",
        lifecycle_status="ready",
        generation=7,
        artifact_version="runner-v1",
        created_at="2026-08-12T12:00:00Z",
        updated_at="2026-08-12T12:00:00Z",
        last_error=None,
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=5,
        status="ready",
        object_key="private/object.enc",
        object_version="version-1",
        size_bytes=17,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at="2026-08-11T12:00:00Z",
        completed_at="2026-08-11T12:01:00Z",
        last_error=None,
    )

    def start_deletion(user_id: str, **values):
        captured["user_id"] = user_id
        captured.update(values)
        return values["job_id"]

    manager = ManagedBackupManager(
        get_runtime=lambda _user_id: runtime,
        get_archive=lambda _user_id, _archive_id: archive,
        start_deletion=start_deletion,
        now=lambda: datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
        new_id=lambda: "job-1",
    )

    job = manager.enqueue_delete("user-1", "archive-1")

    assert job.job_id == "job-1"
    assert job.archive_id == "archive-1"
    assert captured["runtime_generation"] == 7
    assert captured["archive_id"] == "archive-1"


def test_manager_deletes_exact_version_before_erasing_key() -> None:
    """Remote exact-version deletion must precede catalog key erasure."""
    import asyncio
    from datetime import timedelta

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation

    events: list[str] = []
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="job-1",
        archive_id="archive-1",
        operation="delete",
        status="running",
        runtime_generation=7,
        started_at="2026-08-12T12:00:00Z",
        updated_at="2026-08-12T12:00:00Z",
        last_error=None,
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=5,
        status="deleting",
        object_key="private/object.enc",
        object_version="version-1",
        size_bytes=17,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at="2026-08-11T12:00:00Z",
        completed_at="2026-08-11T12:01:00Z",
        last_error=None,
    )

    class Store:
        async def delete_file(self, *, object_key: str, object_version: str) -> None:
            events.append(f"remote:{object_key}:{object_version}")

    def complete(*_args, **_values) -> bool:
        events.append("catalog")
        return True

    manager = ManagedBackupManager(
        provider=object(),
        store=Store(),
        relay=object(),
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        complete_deletion=complete,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
    )

    assert asyncio.run(manager.run_once())
    assert events == ["remote:private/object.enc:version-1", "catalog"]


def test_manager_deletion_completion_uses_exact_current_lease() -> None:
    """A completed remote delete may erase key material only under its current lease."""
    import asyncio
    from datetime import timedelta

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation

    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="job-1",
        archive_id="archive-1",
        operation="delete",
        status="running",
        runtime_generation=7,
        started_at="2026-08-12T12:00:00Z",
        updated_at="2026-08-12T12:00:00Z",
        last_error=None,
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=5,
        status="deleting",
        object_key="private/object.enc",
        object_version="version-1",
        size_bytes=17,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at="2026-08-11T12:00:00Z",
        completed_at="2026-08-11T12:01:00Z",
        last_error=None,
    )

    class Store:
        async def delete_file(self, **_values) -> None:
            return None

    def complete(*_args, **values) -> bool:
        assert values["lease_token"] == "lease-1"
        return True

    manager = ManagedBackupManager(
        provider=object(),
        store=Store(),
        relay=object(),
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        complete_deletion=complete,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
    )

    assert asyncio.run(manager.run_once())


def test_manager_enqueues_bounded_retention_deletions() -> None:
    """Retention should route bounded candidates through normal deletion claims."""
    from datetime import timedelta

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive

    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    candidates = [
        ManagedBackupArchive(
            id=f"archive-{index}",
            user_id=f"user-{index}",
            runtime_generation=1,
            status="ready",
            object_key=f"private/{index}.enc",
            object_version=f"version-{index}",
            size_bytes=17,
            sha256="d" * 64,
            wrapped_key=b"wrapped-key",
            key_id="backup-v1",
            owner_digest="c" * 64,
            created_at="2026-06-01T00:00:00Z",
            completed_at="2026-06-01T00:01:00Z",
            last_error=None,
        )
        for index in range(3)
    ]
    queued: list[tuple[str, str]] = []
    manager = ManagedBackupManager(
        retention_days=30,
        retention_batch_size=2,
        list_retention=lambda **values: (
            candidates
            if values == {"cutoff": (now - timedelta(days=30)).isoformat(), "limit": 2}
            else []
        ),
        enqueue_retention=lambda user_id, archive_id: queued.append((user_id, archive_id)),
        now=lambda: now,
    )

    assert manager.schedule_retention() == 2
    assert queued == [("user-0", "archive-0"), ("user-1", "archive-1")]
