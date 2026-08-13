"""Tests for durable managed backup background ownership."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_reconciliation_failure_restores_source_runtime(tmp_path) -> None:
    """A failed upload recovery pass must restore source services and relay access."""
    from datetime import datetime, timedelta, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="job-create",
        archive_id="archive-1",
        operation="create",
        status="running",
        runtime_generation=7,
        started_at=now.isoformat(),
        updated_at=now.isoformat(),
        last_error=None,
        phase="object_uploading",
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=7,
        status="creating",
        object_key="managed/v1/archive.enc",
        object_version=None,
        size_bytes=17,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at=now.isoformat(),
        completed_at=None,
        last_error=None,
    )
    runtime = ManagedRuntimeStatus(
        user_id="user-1",
        runner_id="runner-1",
        provider_name="fly_sprites",
        sprite_name="sprite-1",
        lifecycle_status="ready",
        generation=7,
        artifact_version="runner-v1",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        last_error=None,
    )
    events: list[str] = []

    class Provider:
        async def start_service(self, _name, **values) -> None:
            events.append(f"start:{values['service_name']}")

    class Store:
        async def reconcile_upload(self, **_values):
            raise RuntimeError("storage unavailable")

    class Relay:
        async def release_maintenance(self, *_args, **_values) -> None:
            events.append("relay-release")

    manager = ManagedBackupManager(
        provider=Provider(),
        store=Store(),
        relay=Relay(),
        wrapping_key=b"w" * 32,
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        get_runtime=lambda _user_id: runtime,
        get_runner=lambda _user_id: {"id": "runner-1"},
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="storage unavailable"):
        await manager.run_once()

    assert events == ["start:yinshi-sidecar", "start:yinshi-runner", "relay-release"]


@pytest.mark.asyncio
async def test_preserved_upload_download_failure_restores_source_runtime(tmp_path) -> None:
    """A failed guest download must restore source services and relay access."""
    from datetime import datetime, timedelta, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="job-create",
        archive_id="archive-1",
        operation="create",
        status="running",
        runtime_generation=7,
        started_at=now.isoformat(),
        updated_at=now.isoformat(),
        last_error=None,
        phase="object_uploading",
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=7,
        status="creating",
        object_key="managed/v1/archive.enc",
        object_version=None,
        size_bytes=17,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at=now.isoformat(),
        completed_at=None,
        last_error=None,
    )
    runtime = ManagedRuntimeStatus(
        user_id="user-1",
        runner_id="runner-1",
        provider_name="fly_sprites",
        sprite_name="sprite-1",
        lifecycle_status="ready",
        generation=7,
        artifact_version="runner-v1",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        last_error=None,
    )
    events: list[str] = []

    class Provider:
        async def download_file(self, *_args, **_values) -> None:
            raise RuntimeError("guest download unavailable")

        async def start_service(self, _name, **values) -> None:
            events.append(f"start:{values['service_name']}")

    class Store:
        async def reconcile_upload(self, **_values):
            return None

    class Relay:
        async def release_maintenance(self, *_args, **_values) -> None:
            events.append("relay-release")

    manager = ManagedBackupManager(
        provider=Provider(),
        store=Store(),
        relay=Relay(),
        wrapping_key=b"w" * 32,
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        get_runtime=lambda _user_id: runtime,
        get_runner=lambda _user_id: {"id": "runner-1"},
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="guest download unavailable"):
        await manager.run_once()

    assert events == ["start:yinshi-sidecar", "start:yinshi-runner", "relay-release"]


@pytest.mark.parametrize("cancel_stage", ["reconcile", "abort", "download"])
@pytest.mark.asyncio
async def test_cancelled_reconciliation_restores_runtime_only_with_current_lease(
    tmp_path,
    cancel_stage: str,
) -> None:
    """Cancellation should restore source access only while exact ownership remains."""
    from datetime import datetime, timedelta, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backup_store import PendingManagedBackupUploads
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="job-create",
        archive_id="archive-1",
        operation="create",
        status="running",
        runtime_generation=7,
        started_at=now.isoformat(),
        updated_at=now.isoformat(),
        last_error=None,
        phase="object_uploading",
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=7,
        status="creating",
        object_key="managed/v1/archive.enc",
        object_version=None,
        size_bytes=17,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at=now.isoformat(),
        completed_at=None,
        last_error=None,
    )
    runtime = ManagedRuntimeStatus(
        user_id="user-1",
        runner_id="runner-1",
        provider_name="fly_sprites",
        sprite_name="sprite-1",
        lifecycle_status="ready",
        generation=7,
        artifact_version="runner-v1",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        last_error=None,
    )
    recovery_started = asyncio.Event()
    events: list[str] = []

    class Provider:
        async def download_file(self, *_args, **_values) -> None:
            if cancel_stage == "download":
                recovery_started.set()
                await asyncio.Event().wait()

        async def start_service(self, *_args, **_values) -> None:
            events.append("start")

    class Store:
        async def reconcile_upload(self, **_values):
            if cancel_stage == "reconcile":
                recovery_started.set()
                await asyncio.Event().wait()
            if cancel_stage == "abort":
                return PendingManagedBackupUploads(("old-upload",))
            return None

        async def abort_uploads(self, **_values) -> None:
            recovery_started.set()
            await asyncio.Event().wait()

    class Relay:
        async def release_maintenance(self, *_args, **_values) -> None:
            events.append("relay-release")

    manager = ManagedBackupManager(
        provider=Provider(),
        store=Store(),
        relay=Relay(),
        wrapping_key=b"w" * 32,
        claim_operation=lambda **_values: operation,
        renew_lease=lambda **_values: True,
        get_archive=lambda _user_id, _archive_id: archive,
        get_runtime=lambda _user_id: runtime,
        get_runner=lambda _user_id: {"id": "runner-1"},
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    task = asyncio.create_task(manager.run_once())
    await asyncio.wait_for(recovery_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == ["start", "start", "relay-release"]


@pytest.mark.asyncio
async def test_manager_periodically_schedules_retention_when_idle() -> None:
    """Idle background cadence should enqueue bounded retention work."""
    from yinshi.services.managed_backup_manager import ManagedBackupManager

    scheduled = asyncio.Event()

    async def reconcile_once() -> bool:
        return False

    manager = ManagedBackupManager(
        reconcile_once=reconcile_once,
        interval_seconds=0.01,
        wrapping_key=b"w" * 32,
        enqueue_retention=lambda *_args: None,
    )
    manager.schedule_retention = lambda: scheduled.set() or 0
    await manager.start()
    await asyncio.wait_for(scheduled.wait(), timeout=1)
    await manager.aclose()

    assert scheduled.is_set()


@pytest.mark.asyncio
async def test_manager_claim_lease_outlives_bounded_guest_work() -> None:
    """Claim ownership should outlast the maximum configured maintenance hold."""
    from datetime import datetime, timedelta, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager

    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    captured: dict[str, object] = {}

    def claim_operation(**values):
        captured.update(values)
        return None

    manager = ManagedBackupManager(
        provider=object(),
        store=object(),
        relay=object(),
        claim_operation=claim_operation,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
    )

    assert not await manager.run_once()
    assert captured["lease_expires_at"] >= now + timedelta(minutes=10)


@pytest.mark.asyncio
async def test_manager_shutdown_waits_for_external_work_cancellation() -> None:
    """Manager close must cancel and drain one claimed external operation."""
    from datetime import datetime, timedelta, timezone

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
        started_at=now.isoformat(),
        updated_at=now.isoformat(),
        last_error=None,
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=15)).isoformat(),
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=7,
        status="deleting",
        object_key="private/archive.enc",
        object_version="version-1",
        size_bytes=17,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at=now.isoformat(),
        completed_at=None,
        last_error=None,
    )
    work_started = asyncio.Event()
    work_stopped = asyncio.Event()
    claimed = False

    class Store:
        async def delete_file(self, **_values) -> None:
            work_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                work_stopped.set()

    def claim(**_values):
        nonlocal claimed
        if claimed:
            return None
        claimed = True
        return operation

    manager = ManagedBackupManager(
        provider=object(),
        store=Store(),
        relay=object(),
        claim_operation=claim,
        get_archive=lambda _user_id, _archive_id: archive,
        list_retention=lambda **_values: (),
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
    )
    await manager.start()
    await asyncio.wait_for(work_started.wait(), timeout=1)

    await manager.aclose()

    assert work_stopped.is_set()


@pytest.mark.asyncio
async def test_manager_continues_after_one_reconciliation_failure() -> None:
    """One transient job failure must not stop the shared background manager."""
    from yinshi.services.managed_backup_manager import ManagedBackupManager

    calls = 0
    recovered = asyncio.Event()

    async def reconcile_once() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient provider failure")
        recovered.set()
        return False

    manager = ManagedBackupManager(
        reconcile_once=reconcile_once,
        interval_seconds=0.01,
    )
    await manager.start()
    await asyncio.wait_for(recovered.wait(), timeout=1)
    await manager.aclose()

    assert calls >= 2


@pytest.mark.asyncio
async def test_manager_reconciles_after_start_and_wake() -> None:
    """Startup and accepted API work should each prompt bounded reconciliation."""
    from yinshi.services.managed_backup_manager import ManagedBackupManager

    calls = 0
    reconciled = asyncio.Event()

    async def reconcile_once() -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            reconciled.set()
        return False

    manager = ManagedBackupManager(
        reconcile_once=reconcile_once,
        interval_seconds=60,
        list_retention=lambda **_values: (),
    )
    await manager.start()
    while calls == 0:
        await asyncio.sleep(0)
    manager.wake()
    await asyncio.wait_for(reconciled.wait(), timeout=1)
    await manager.aclose()

    assert calls == 2


@pytest.mark.asyncio
async def test_manager_preserves_wake_received_during_reconciliation() -> None:
    """Work accepted during an active pass should prompt another pass immediately."""
    from yinshi.services.managed_backup_manager import ManagedBackupManager

    calls = 0
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def reconcile_once() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
        if calls == 2:
            second_started.set()
        return False

    manager = ManagedBackupManager(
        reconcile_once=reconcile_once,
        interval_seconds=60,
        list_retention=lambda **_values: (),
    )
    await manager.start()
    await asyncio.wait_for(first_started.wait(), timeout=1)
    manager.wake()
    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)
    await manager.aclose()

    assert calls == 2


@pytest.mark.asyncio
async def test_manager_schedules_retention_during_background_reconciliation() -> None:
    """The background owner should periodically queue bounded retention work."""
    from yinshi.services.managed_backup_manager import ManagedBackupManager

    reconciled = asyncio.Event()
    retention_calls = 0
    retention_scans = 0

    async def reconcile_once() -> bool:
        reconciled.set()
        return False

    def list_retention(**_values):
        nonlocal retention_scans
        retention_scans += 1
        return ()

    def enqueue_retention(_user_id: str, _archive_id: str) -> None:
        nonlocal retention_calls
        retention_calls += 1

    manager = ManagedBackupManager(
        reconcile_once=reconcile_once,
        interval_seconds=60,
        list_retention=list_retention,
        enqueue_retention=enqueue_retention,
    )
    await manager.start()
    await asyncio.wait_for(reconciled.wait(), timeout=1)
    await manager.aclose()

    assert retention_calls == 0
    assert retention_scans == 1


def test_manager_reports_missing_archive_before_restore_state_conflict() -> None:
    """Missing tenant archive should map to the API not-found contract."""
    from yinshi.services.managed_backup_manager import ManagedBackupManager

    manager = ManagedBackupManager(
        get_runtime=lambda _user_id: None,
        get_archive=lambda _user_id, _archive_id: None,
    )

    with pytest.raises(LookupError, match="not found"):
        manager.enqueue_restore("user-1", "missing-archive")


@pytest.mark.asyncio
async def test_manager_enqueues_restore_with_server_runtime_authority() -> None:
    """Restore requests should resolve tenant archive and generation internally."""
    from datetime import datetime, timezone

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

    def start_restore(user_id: str, **values):
        captured["user_id"] = user_id
        captured.update(values)
        return values["job_id"]

    manager = ManagedBackupManager(
        get_runtime=lambda _user_id: runtime,
        get_archive=lambda _user_id, _archive_id: archive,
        start_restore=start_restore,
        now=lambda: datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
        new_id=lambda: "job-1",
    )

    job = manager.enqueue_restore("user-1", "archive-1")

    assert job.job_id == "job-1"
    assert job.archive_id == "archive-1"
    assert captured["runtime_generation"] == 7
    assert captured["archive_id"] == "archive-1"


@pytest.mark.asyncio
async def test_manager_enqueues_create_with_server_generated_authority() -> None:
    """The manager should derive all archive authority outside the browser request."""
    from datetime import datetime, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager
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

    def start_creation(user_id: str, **values):
        captured["user_id"] = user_id
        captured.update(values)
        return values["job_id"]

    manager = ManagedBackupManager(
        start_creation=start_creation,
        get_runtime=lambda _user_id: runtime,
        get_runner=lambda _user_id: {
            "id": "runner-1",
            "noise_key_confirmed": True,
            "noise_public_key": "runner-public-key",
        },
        wrapping_key=b"w" * 32,
        key_id="backup-v1",
        object_prefix="managed-v1",
        now=lambda: datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
        new_id=iter(("archive-1", "job-1")).__next__,
    )

    job = manager.enqueue_create("user-1")

    assert job.job_id == "job-1"
    assert job.archive_id == "archive-1"
    assert captured["runtime_generation"] == 7
    assert captured["object_key"] == "managed-v1/archive-1.enc"
    assert captured["owner_digest"] == (
        "c6c289e49e9c05b2145860387b73bcb18df43fb09a1e4a4a9713c76c88bb541b"
    )
    assert captured["wrapped_key"] != b""


@pytest.mark.asyncio
async def test_manager_renews_operation_lease_during_external_work() -> None:
    """Claimed work should retain exact ownership until coordination finishes."""
    from datetime import datetime, timedelta, timezone

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
        lease_expires_at=(now + timedelta(minutes=15)).isoformat(),
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=7,
        status="deleting",
        object_key="private/archive.enc",
        object_version="version-1",
        size_bytes=17,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at="2026-08-11T12:00:00Z",
        completed_at=None,
        last_error=None,
    )
    renewed = asyncio.Event()
    release_delete = asyncio.Event()
    current_now = now

    class Store:
        async def delete_file(self, **_values) -> None:
            await asyncio.wait_for(release_delete.wait(), timeout=1)

    def renew_lease(**values) -> bool:
        nonlocal current_now
        assert values["job_id"] == "job-1"
        assert values["lease_token"] == "lease-1"
        current_now = now + timedelta(minutes=1)
        renewed.set()
        release_delete.set()
        return True

    manager = ManagedBackupManager(
        provider=object(),
        store=Store(),
        relay=object(),
        worker_id="worker-1",
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        renew_lease=renew_lease,
        lease_renew_interval_seconds=0.01,
        complete_deletion=lambda *_args, **_values: True,
        now=lambda: current_now,
        new_lease_token=lambda: "lease-1",
    )

    assert await manager.run_once()
    assert renewed.is_set()


@pytest.mark.asyncio
async def test_run_once_cancellation_stops_external_work() -> None:
    """Manager cancellation must stop a claimed external operation before returning."""
    from datetime import datetime, timedelta, timezone

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
        started_at=now.isoformat(),
        updated_at=now.isoformat(),
        last_error=None,
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=15)).isoformat(),
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=7,
        status="deleting",
        object_key="private/archive.enc",
        object_version="version-1",
        size_bytes=17,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at=now.isoformat(),
        completed_at=None,
        last_error=None,
    )
    work_started = asyncio.Event()
    work_stopped = asyncio.Event()

    class Store:
        async def delete_file(self, **_values) -> None:
            work_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                work_stopped.set()

    manager = ManagedBackupManager(
        provider=object(),
        store=Store(),
        relay=object(),
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
    )
    task = asyncio.create_task(manager.run_once())
    await asyncio.wait_for(work_started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert work_stopped.is_set()


@pytest.mark.asyncio
async def test_manager_aborts_work_when_lease_renewal_fails() -> None:
    """Lost lease ownership should cancel external work before it can finish."""
    from datetime import datetime, timedelta, timezone

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
        started_at=now.isoformat(),
        updated_at=now.isoformat(),
        last_error=None,
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=15)).isoformat(),
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=7,
        status="deleting",
        object_key="private/archive.enc",
        object_version="version-1",
        size_bytes=17,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at=now.isoformat(),
        completed_at=None,
        last_error=None,
    )
    cancelled = asyncio.Event()

    class Store:
        async def delete_file(self, **_values) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    manager = ManagedBackupManager(
        provider=object(),
        store=Store(),
        relay=object(),
        worker_id="worker-1",
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        renew_lease=lambda **_values: False,
        lease_renew_interval_seconds=0.01,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
    )

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    with pytest.raises(RuntimeError, match="lease was lost"):
        await asyncio.wait_for(manager.run_once(), timeout=1)
    assert loop.time() - started_at < 0.2
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_manager_create_uploads_before_release_and_publishes_after_recovery(
    tmp_path,
) -> None:
    """Create coordination should fence maintenance and publish only after recovery."""
    import hashlib
    import json
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backup_store import StoredManagedBackup
    from yinshi.services.managed_backups import (
        ManagedBackupArchive,
        ManagedBackupOperation,
    )
    from yinshi.services.managed_runners import ManagedRuntimeStatus
    from yinshi.services.sprites import SpriteFileTransfer

    events: list[str] = []
    upload_intent_recorded = False
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e91",
        archive_id="archive-1",
        operation="create",
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
        runtime_generation=7,
        status="creating",
        object_key="managed-v1/archive-1.enc",
        object_version=None,
        size_bytes=None,
        sha256=None,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at="2026-08-12T12:00:00Z",
        completed_at=None,
        last_error=None,
    )
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
    ciphertext = b"encrypted-archive"
    digest = hashlib.sha256(ciphertext).hexdigest()

    class Provider:
        async def write_file(self, _name: str, **values) -> None:
            events.append(f"write:{Path(values['path']).suffix}")
            assert b"archive_key" not in values["content"]

        async def configure_service(self, _name: str, **values) -> None:
            events.append("configure")
            assert values["args"] == (
                "-m",
                "yinshi.managed_backup_guest",
                "create",
                "--job-id",
                operation.job_id,
                "--hold-seconds",
                "300",
            )

        async def stop_service(self, _name: str, **_values) -> None:
            events.append("stop-sidecar")

        async def start_service(self, _name: str, **values) -> None:
            events.append(f"start:{values['service_name']}")

        async def read_file(self, _name: str, **_values) -> bytes:
            events.append("result")
            return json.dumps(
                {
                    "job_id": operation.job_id,
                    "sha256": digest,
                    "size_bytes": len(ciphertext),
                    "status": "ready",
                }
            ).encode()

        async def download_file(self, _name: str, **values) -> SpriteFileTransfer:
            events.append("download")
            values["target_path"].write_bytes(ciphertext)
            return SpriteFileTransfer(size_bytes=len(ciphertext), sha256=digest)

        async def delete_file(self, _name: str, **_values) -> None:
            events.append("cleanup")

    class Store:
        async def put_file(self, source_path: Path, **values) -> StoredManagedBackup:
            events.append("upload")
            assert upload_intent_recorded
            assert source_path.read_bytes() == ciphertext
            assert values["object_key"] == archive.object_key
            return StoredManagedBackup(
                version="version-1",
                size_bytes=len(ciphertext),
                sha256=digest,
            )

    class Relay:
        async def quiesce_runner(self, runner_id: str, **values) -> None:
            events.append("quiesce")
            assert runner_id == "runner-1"
            assert values["job_id"] == operation.job_id

        async def release_maintenance(self, runner_id: str, **values) -> None:
            events.append("relay-release")
            assert runner_id == "runner-1"
            assert values["job_id"] == operation.job_id

        def is_runner_connected(self, runner_id: str) -> bool:
            return runner_id == "runner-1"

    def record_upload_intent(**values) -> bool:
        nonlocal upload_intent_recorded
        assert values["size_bytes"] == len(ciphertext)
        assert values["sha256"] == digest
        upload_intent_recorded = True
        return True

    def record_upload(*_args, **values) -> bool:
        events.append("record-upload")
        assert values["lease_token"] == "lease-1"
        return True

    def complete(*_args, **values) -> bool:
        events.append("publish")
        assert values["lease_token"] == "lease-1"
        return True

    manager = ManagedBackupManager(
        provider=Provider(),
        store=Store(),
        relay=Relay(),
        wrapping_key=b"w" * 32,
        key_id="backup-v1",
        worker_id="worker-1",
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        get_runtime=lambda _user_id: runtime,
        get_runner=lambda _user_id: {
            "id": "runner-1",
            "noise_key_confirmed": True,
            "noise_public_key": "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
        },
        unwrap_key=lambda **_values: b"k" * 32,
        record_upload_intent=record_upload_intent,
        record_upload=record_upload,
        complete_creation=complete,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    assert await manager.run_once()
    assert upload_intent_recorded
    assert events.index("upload") < events.index("record-upload")
    assert events.index("record-upload") < events.index("write:.release")
    assert events.index("start:yinshi-sidecar") < events.index("publish")
    assert events.index("relay-release") < events.index("publish")
    assert not tuple(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_manager_restore_executes_download_guest_restore_and_activation(tmp_path) -> None:
    """Restore should verify exact ciphertext before candidate activation and source deletion."""
    import hashlib
    from datetime import datetime, timedelta, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation
    from yinshi.services.managed_runners import ManagedRuntimeStatus, managed_sprite_name
    from yinshi.services.managed_runtime_manager import OnlineManagedRunner

    payload = b"encrypted-archive"
    digest = hashlib.sha256(payload).hexdigest()
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    candidate_name = managed_sprite_name(
        "user-1:job-restore",
        prefix="managed-restore",
        secret_key="restore-secret",
    )
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="job-restore",
        archive_id="archive-1",
        operation="restore",
        status="running",
        runtime_generation=7,
        started_at="2026-08-12T12:00:00Z",
        updated_at="2026-08-12T12:00:00Z",
        last_error=None,
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
        source_runner_id="runner-1",
        source_sprite_id="sprite-1",
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=5,
        status="ready",
        object_key="private/archive.enc",
        object_version="version-1",
        size_bytes=len(payload),
        sha256=digest,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at="2026-08-11T12:00:00Z",
        completed_at="2026-08-11T12:01:00Z",
        last_error=None,
    )
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
    events: list[str] = []

    class RuntimeService:
        artifact_version = "runner-v7"

        async def provision_restore_candidate(self, user_id: str, **values):
            events.append(f"provision:{values['candidate_sprite_name']}")
            return OnlineManagedRunner(
                "candidate-runner",
                "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
            )

        async def verify_restore_candidate(self, user_id: str, **values) -> None:
            assert values["job_id"] == "job-restore"
            events.append(f"verify:{values['candidate_runner_id']}")

        def get_status(self, _user_id: str):
            return runtime

    class Store:
        async def get_file(self, target_path, **values) -> None:
            events.append(f"download:{values['object_version']}")
            target_path.write_bytes(payload)

    class Provider:
        async def upload_file(self, name: str, **values) -> None:
            events.append(f"upload:{name}:{values['path']}")

        async def write_file(self, name: str, **values) -> None:
            events.append(f"write:{name}:{values['path']}")

        async def configure_service(self, name: str, **values) -> None:
            events.append(f"configure:{name}:{values['service_name']}")

        async def start_service(self, name: str, **values) -> None:
            events.append(f"start:{name}:{values['service_name']}")

        async def read_file(self, name: str, **_values) -> bytes:
            events.append(f"result:{name}")
            return b'{"cleanup_pending":false,"job_id":"job-restore","status":"restored"}'

        async def delete_sprite(self, name: str) -> None:
            events.append(f"delete:{name}")

        async def delete_file(self, *_args, **_values) -> None:
            return None

    class Relay:
        async def quiesce_runner(self, runner_id: str, **_values) -> None:
            events.append(f"quiesce:{runner_id}")

    manager = ManagedBackupManager(
        provider=Provider(),
        store=Store(),
        relay=Relay(),
        runtime_service=RuntimeService(),
        wrapping_key=b"w" * 32,
        restore_name_prefix="managed-restore",
        restore_name_key="restore-secret",
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        get_runtime=lambda _user_id: runtime,
        unwrap_key=lambda **_values: b"k" * 32,
        record_candidate=lambda **_values: True,
        record_upload_intent=lambda **_values: True,
        activate_candidate=lambda *_args, **values: (
            events.append(f"activate:{values['artifact_version']}") or True
        ),
        complete_restore=lambda **_values: True,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    assert await manager.run_once()
    assert events == [
        f"provision:{candidate_name}",
        "download:version-1",
        f"upload:{candidate_name}:/var/lib/yinshi/maintenance/job-restore.archive.enc",
        f"write:{candidate_name}:/var/lib/yinshi/maintenance/job-restore.job",
        f"configure:{candidate_name}:yinshi-maintenance",
        f"start:{candidate_name}:yinshi-maintenance",
        f"result:{candidate_name}",
        f"start:{candidate_name}:yinshi-sidecar",
        f"start:{candidate_name}:yinshi-runner",
        "verify:candidate-runner",
        "quiesce:runner-1",
        "activate:runner-v7",
        "delete:sprite-1",
    ]


@pytest.mark.asyncio
async def test_restore_recovery_after_activation_only_deletes_old_sprite(tmp_path) -> None:
    """A restart after atomic activation must never provision or cut authority back."""
    from datetime import datetime, timedelta, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation

    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="job-restore",
        archive_id="archive-1",
        operation="restore",
        status="running",
        runtime_generation=7,
        started_at="2026-08-12T12:00:00Z",
        updated_at="2026-08-12T12:00:00Z",
        last_error=None,
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
        phase="activated",
        source_runner_id="runner-1",
        source_sprite_id="sprite-1",
        candidate_runner_id="candidate-runner",
        candidate_sprite_id="candidate-sprite",
        activation_generation=8,
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=5,
        status="ready",
        object_key="private/archive.enc",
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
    events: list[str] = []

    class Provider:
        async def delete_sprite(self, name: str) -> None:
            events.append(f"delete:{name}")

        async def delete_file(self, name: str, **values) -> None:
            events.append(f"cleanup:{name}:{values['path']}")

    class RuntimeService:
        async def provision_restore_candidate(self, *_args, **_values):
            raise AssertionError("activated restore must not provision again")

    manager = ManagedBackupManager(
        provider=Provider(),
        store=object(),
        relay=object(),
        runtime_service=RuntimeService(),
        wrapping_key=b"w" * 32,
        restore_name_key="restore-secret",
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        complete_restore=lambda **_values: events.append("complete") or True,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    assert await manager.run_once()
    assert events[0] == "delete:sprite-1"
    assert events[-1] == "complete"
    assert events[1:-1] == [
        "cleanup:candidate-sprite:/var/lib/yinshi/maintenance/job-restore.job",
        "cleanup:candidate-sprite:/var/lib/yinshi/maintenance/job-restore.result",
        "cleanup:candidate-sprite:/var/lib/yinshi/maintenance/job-restore.archive.enc",
        "cleanup:candidate-sprite:/var/lib/yinshi/maintenance/job-restore.release",
    ]


@pytest.mark.asyncio
async def test_restore_failure_before_activation_revokes_candidate_authority(
    tmp_path,
) -> None:
    """Failed replacement cleanup should revoke candidate credentials before retry."""
    from datetime import datetime, timedelta, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupOperation

    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="job-restore",
        archive_id="archive-1",
        operation="restore",
        status="running",
        runtime_generation=7,
        started_at="2026-08-12T12:00:00Z",
        updated_at="2026-08-12T12:00:00Z",
        last_error=None,
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
        source_runner_id="runner-source",
        source_sprite_id="sprite-source",
        candidate_runner_id="runner-candidate",
        candidate_sprite_id="sprite-candidate",
    )
    revoked: list[str] = []
    cleared: list[str] = []

    class Provider:
        async def delete_sprite(self, _name: str) -> None:
            return None

        async def start_service(self, *_args, **_values) -> None:
            return None

    class Relay:
        async def release_maintenance(self, *_args, **_values) -> None:
            return None

    manager = ManagedBackupManager(
        provider=Provider(),
        store=object(),
        relay=Relay(),
        revoke_restore_runner=lambda user_id, job_id: (
            revoked.append(f"{user_id}:{job_id}") or True
        ),
        clear_candidate=lambda **values: (cleared.append(values["candidate_runner_id"]) or True),
        now=lambda: now,
        staging_root=tmp_path,
    )

    await manager._recover_failed_restore(operation, "sprite-candidate")

    assert revoked == ["user-1:job-restore"]
    assert cleared == ["runner-candidate"]


@pytest.mark.asyncio
async def test_failed_candidate_deletion_retains_durable_identity(tmp_path) -> None:
    """A provider deletion failure must preserve candidate IDs for retry."""
    from datetime import datetime, timedelta, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation

    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="job-restore",
        archive_id="archive-1",
        operation="restore",
        status="running",
        runtime_generation=7,
        started_at=now.isoformat(),
        updated_at=now.isoformat(),
        last_error=None,
        phase="candidate_provisioning",
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
        source_runner_id="runner-source",
        source_sprite_id="sprite-source",
        candidate_runner_id="runner-candidate",
        candidate_sprite_id="sprite-candidate",
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=5,
        status="ready",
        object_key="private/archive.enc",
        object_version="version-1",
        size_bytes=17,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at=now.isoformat(),
        completed_at=now.isoformat(),
        last_error=None,
    )
    cleared: list[str] = []

    class RuntimeService:
        async def provision_restore_candidate(self, *_args, **_values):
            raise RuntimeError("candidate unavailable")

    class Provider:
        async def delete_sprite(self, _name: str) -> None:
            raise RuntimeError("candidate deletion failed")

        async def start_service(self, *_args, **_values) -> None:
            return None

    class Relay:
        async def release_maintenance(self, *_args, **_values) -> None:
            return None

    manager = ManagedBackupManager(
        provider=Provider(),
        store=object(),
        relay=Relay(),
        runtime_service=RuntimeService(),
        wrapping_key=b"w" * 32,
        restore_name_key="restore-secret",
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        clear_candidate=lambda **values: (cleared.append(values["candidate_runner_id"]) or True),
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="candidate unavailable"):
        await manager.run_once()

    assert cleared == []


@pytest.mark.asyncio
async def test_restore_failure_before_activation_deletes_candidate_and_resumes_source(
    tmp_path,
) -> None:
    """Pre-activation restore failure should leave source authoritative and remove candidate."""
    from datetime import datetime, timedelta, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation
    from yinshi.services.managed_runtime_manager import OnlineManagedRunner

    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="job-restore",
        archive_id="archive-1",
        operation="restore",
        status="running",
        runtime_generation=7,
        started_at="2026-08-12T12:00:00Z",
        updated_at="2026-08-12T12:00:00Z",
        last_error=None,
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
        source_runner_id="runner-1",
        source_sprite_id="sprite-1",
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=5,
        status="ready",
        object_key="private/archive.enc",
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
    events: list[str] = []

    class RuntimeService:
        async def provision_restore_candidate(self, *_args, **_values):
            return OnlineManagedRunner(
                "candidate-runner",
                "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
            )

    class Store:
        async def get_file(self, *_args, **_values) -> None:
            raise RuntimeError("storage unavailable")

    class Provider:
        async def delete_sprite(self, name: str) -> None:
            events.append(f"delete:{name}")

        async def start_service(self, name: str, **values) -> None:
            events.append(f"start:{name}:{values['service_name']}")

    class Relay:
        async def release_maintenance(self, runner_id: str, **_values) -> None:
            events.append(f"release:{runner_id}")

    manager = ManagedBackupManager(
        provider=Provider(),
        store=Store(),
        relay=Relay(),
        runtime_service=RuntimeService(),
        wrapping_key=b"w" * 32,
        restore_name_key="restore-secret",
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        record_candidate=lambda **_values: True,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="storage unavailable"):
        await manager.run_once()
    assert "delete:managed-restore-" in events[0]
    assert "start:sprite-1:yinshi-sidecar" in events
    assert "start:sprite-1:yinshi-runner" in events
    assert "release:runner-1" in events


@pytest.mark.asyncio
async def test_manager_restore_starts_replacement_execution(tmp_path) -> None:
    """Claimed restore work should execute against a non-active replacement."""
    from datetime import datetime, timedelta, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation

    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="job-restore",
        archive_id="archive-1",
        operation="restore",
        status="running",
        runtime_generation=7,
        started_at="2026-08-12T12:00:00Z",
        updated_at="2026-08-12T12:00:00Z",
        last_error=None,
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
        source_runner_id="runner-1",
        source_sprite_id="sprite-1",
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=5,
        status="ready",
        object_key="private/archive.enc",
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
    calls: list[tuple[ManagedBackupOperation, ManagedBackupArchive]] = []

    async def coordinate_restore(
        claimed_operation: ManagedBackupOperation,
        claimed_archive: ManagedBackupArchive,
    ) -> None:
        calls.append((claimed_operation, claimed_archive))

    manager = ManagedBackupManager(
        provider=object(),
        store=object(),
        relay=object(),
        wrapping_key=b"w" * 32,
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        coordinate_restore=coordinate_restore,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    assert await manager.run_once()
    assert calls == [(operation, archive)]


@pytest.mark.asyncio
async def test_manager_classifies_restore_coordination_failure(tmp_path) -> None:
    """A failed restore coordinator must persist the restore failure class."""
    from datetime import datetime, timedelta, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation

    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="job-restore-failure",
        archive_id="archive-1",
        operation="restore",
        status="running",
        runtime_generation=7,
        started_at=now.isoformat(),
        updated_at=now.isoformat(),
        last_error=None,
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
    )
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=7,
        status="ready",
        object_key="private/archive.enc",
        object_version="version-1",
        size_bytes=17,
        sha256="d" * 64,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at=now.isoformat(),
        completed_at=now.isoformat(),
        last_error=None,
    )
    failures: list[str] = []

    async def coordinate_restore(*_values: object) -> None:
        raise RuntimeError("provider details")

    manager = ManagedBackupManager(
        provider=object(),
        store=object(),
        relay=object(),
        wrapping_key=b"w" * 32,
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        coordinate_restore=coordinate_restore,
        fail_operation=lambda **values: failures.append(values["failure_class"]) or True,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="provider details"):
        await manager.run_once()
    assert failures == ["restore_failed"]


@pytest.mark.asyncio
async def test_manager_recovers_uploaded_archive_without_second_upload(tmp_path) -> None:
    """Restart after durable upload should recover services and publish the same version."""
    import hashlib
    from datetime import datetime, timedelta, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import (
        ManagedBackupArchive,
        ManagedBackupOperation,
    )
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    events: list[str] = []
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e98",
        archive_id="archive-1",
        operation="create",
        status="running",
        runtime_generation=7,
        started_at="2026-08-12T12:00:00Z",
        updated_at="2026-08-12T12:01:00Z",
        last_error=None,
        lease_owner="worker-1",
        lease_token="lease-1",
        lease_expires_at=(now + timedelta(minutes=2)).isoformat(),
    )
    digest = hashlib.sha256(b"encrypted-archive").hexdigest()
    archive = ManagedBackupArchive(
        id="archive-1",
        user_id="user-1",
        runtime_generation=7,
        status="uploaded",
        object_key="managed-v1/archive-1.enc",
        object_version="version-1",
        size_bytes=17,
        sha256=digest,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at="2026-08-12T12:00:00Z",
        completed_at="2026-08-12T12:01:00Z",
        last_error=None,
    )
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

    class Provider:
        async def write_file(self, _name: str, **values) -> None:
            events.append(f"write:{values['path'].rsplit('.', 1)[-1]}")

        async def start_service(self, _name: str, **values) -> None:
            events.append(f"start:{values['service_name']}")

        async def delete_file(self, _name: str, **_values) -> None:
            events.append("cleanup")

    class Store:
        async def put_file(self, *_args, **_values):
            raise AssertionError("uploaded archive must not be uploaded twice")

    class Relay:
        async def release_maintenance(self, _runner_id: str, **_values) -> None:
            events.append("relay-release")

    def complete(*_args, **values) -> bool:
        events.append("publish")
        assert values["object_version"] == "version-1"
        return True

    manager = ManagedBackupManager(
        provider=Provider(),
        store=Store(),
        relay=Relay(),
        wrapping_key=b"w" * 32,
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        get_runtime=lambda _user_id: runtime,
        get_runner=lambda _user_id: {"id": "runner-1"},
        complete_creation=complete,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    assert await manager.run_once()
    assert events.index("write:release") < events.index("publish")
    assert events.index("relay-release") < events.index("publish")


@pytest.mark.asyncio
async def test_manager_recovers_source_when_create_fails_before_upload(tmp_path) -> None:
    """A pre-upload failure should restart services and release exact maintenance."""
    from datetime import datetime, timedelta, timezone

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    events: list[str] = []
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    operation = ManagedBackupOperation(
        user_id="user-1",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e99",
        archive_id="archive-1",
        operation="create",
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
        runtime_generation=7,
        status="creating",
        object_key="managed-v1/archive-1.enc",
        object_version=None,
        size_bytes=None,
        sha256=None,
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        created_at="2026-08-12T12:00:00Z",
        completed_at=None,
        last_error=None,
    )
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

    class Provider:
        async def stop_service(self, *_args, **_values) -> None:
            events.append("stop")

        async def write_file(self, *_args, **_values) -> None:
            events.append("write")

        async def configure_service(self, *_args, **_values) -> None:
            events.append("configure")

        async def start_service(self, _name: str, **values) -> None:
            events.append(f"start:{values['service_name']}")

        async def read_file(self, *_args, **_values) -> bytes:
            raise RuntimeError("guest result unavailable")

        async def delete_file(self, *_args, **_values) -> None:
            events.append("cleanup")

    class Relay:
        async def quiesce_runner(self, *_args, **_values) -> None:
            events.append("quiesce")

        async def release_maintenance(self, *_args, **_values) -> None:
            events.append("relay-release")

    manager = ManagedBackupManager(
        provider=Provider(),
        store=object(),
        relay=Relay(),
        wrapping_key=b"w" * 32,
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        get_runtime=lambda _user_id: runtime,
        get_runner=lambda _user_id: {
            "id": "runner-1",
            "noise_public_key": "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
        },
        unwrap_key=lambda **_values: b"k" * 32,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="guest result unavailable"):
        await manager.run_once()

    assert "start:yinshi-sidecar" in events
    assert "start:yinshi-runner" in events
    assert "relay-release" in events
    assert "cleanup" in events
