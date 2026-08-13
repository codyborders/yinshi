"""Regression tests for managed backup lifecycle review findings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.asyncio
async def test_restore_post_activation_delete_failure_preserves_candidate(tmp_path) -> None:
    """A source cleanup failure must never delete the activated replacement."""
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
    deleted: list[str] = []
    revoked: list[str] = []

    class RuntimeService:
        artifact_version = "runner-v7"

        async def provision_restore_candidate(self, *_args, **_values):
            return OnlineManagedRunner(
                "candidate-runner",
                "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
            )

        async def verify_restore_candidate(self, *_args, **_values) -> None:
            return None

    class Store:
        async def get_file(self, target_path, **_values) -> None:
            target_path.write_bytes(b"encrypted-archive")

    class Provider:
        async def upload_file(self, *_args, **_values) -> None:
            return None

        async def write_file(self, *_args, **_values) -> None:
            return None

        async def configure_service(self, *_args, **_values) -> None:
            return None

        async def start_service(self, *_args, **_values) -> None:
            return None

        async def delete_file(self, *_args, **_values) -> None:
            return None

        async def read_file(self, *_args, **_values) -> bytes:
            return b'{"cleanup_pending":false,"job_id":"job-restore","status":"restored"}'

        async def delete_sprite(self, name: str) -> None:
            deleted.append(name)
            if name == "sprite-1":
                raise RuntimeError("source deletion failed")

    class Relay:
        async def quiesce_runner(self, *_args, **_values) -> None:
            return None

        async def release_maintenance(self, *_args, **_values) -> None:
            return None

    manager = ManagedBackupManager(
        provider=Provider(),
        store=Store(),
        relay=Relay(),
        runtime_service=RuntimeService(),
        wrapping_key=b"w" * 32,
        restore_name_key="restore-secret",
        claim_operation=lambda **_values: operation,
        get_archive=lambda _user_id, _archive_id: archive,
        unwrap_key=lambda **_values: b"k" * 32,
        record_candidate=lambda **_values: True,
        activate_candidate=lambda *_args, **_values: True,
        revoke_restore_runner=lambda user_id, job_id: (
            revoked.append(f"{user_id}:{job_id}") or True
        ),
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="source deletion failed"):
        await manager.run_once()

    assert all("managed-restore-" not in name for name in deleted)
    assert revoked == []


@pytest.mark.asyncio
async def test_restore_provisioning_failure_cleans_candidate_identity(tmp_path) -> None:
    """Partial candidate provisioning must revoke authority and delete its Sprite."""
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
        created_at=now.isoformat(),
        completed_at=now.isoformat(),
        last_error=None,
    )
    deleted: list[str] = []
    revoked: list[str] = []

    class RuntimeService:
        async def provision_restore_candidate(self, *_args, **_values):
            raise RuntimeError("candidate install failed")

    class Provider:
        async def delete_sprite(self, name: str) -> None:
            deleted.append(name)

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
        revoke_restore_runner=lambda user_id, job_id: (
            revoked.append(f"{user_id}:{job_id}") or True
        ),
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="candidate install failed"):
        await manager.run_once()
    assert len(deleted) == 1
    assert revoked == ["user-1:job-restore"]


@pytest.mark.asyncio
async def test_unrecorded_restore_candidate_is_cleaned_up(tmp_path) -> None:
    """A candidate without a durable job link must lose Sprite and runner authority."""
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
    deleted: list[str] = []
    revoked: list[str] = []

    class RuntimeService:
        async def provision_restore_candidate(self, *_args, **_values):
            return OnlineManagedRunner(
                "candidate-runner",
                "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
            )

    class Provider:
        async def delete_sprite(self, name: str) -> None:
            deleted.append(name)

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
        record_candidate=lambda **_values: False,
        revoke_restore_runner=lambda user_id, job_id: (
            revoked.append(f"{user_id}:{job_id}") or True
        ),
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    assert await manager.run_once()
    assert len(deleted) == 1
    assert "managed-restore-" in deleted[0]
    assert revoked == ["user-1:job-restore"]


@pytest.mark.asyncio
async def test_create_upload_retry_recovers_unrecorded_version(tmp_path) -> None:
    """An uncertain first upload must recover its exact version without replacement."""
    import hashlib
    import json

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backup_store import StoredManagedBackup
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    payload = b"encrypted-archive"
    digest = hashlib.sha256(payload).hexdigest()
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
        object_key="managed-v1/archive-1.enc",
        object_version=None,
        size_bytes=len(payload),
        sha256=digest,
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

    class Provider:
        async def stop_service(self, *_args, **_values) -> None:
            return None

        async def write_file(self, *_args, **_values) -> None:
            return None

        async def configure_service(self, *_args, **_values) -> None:
            return None

        async def start_service(self, *_args, **_values) -> None:
            return None

        async def read_file(self, *_args, **_values) -> bytes:
            return json.dumps(
                {
                    "job_id": operation.job_id,
                    "sha256": digest,
                    "size_bytes": len(payload),
                    "status": "ready",
                }
            ).encode()

        async def download_file(self, _name, **values) -> None:
            values["target_path"].write_bytes(payload)

        async def delete_file(self, *_args, **_values) -> None:
            return None

    class Store:
        async def put_file(self, *_args, **_values) -> StoredManagedBackup:
            raise AssertionError("uncertain upload must not be repeated")

        async def reconcile_upload(self, **values) -> StoredManagedBackup:
            assert values == {
                "archive_id": "archive-1",
                "expected_sha256": digest,
                "expected_size": len(payload),
                "object_key": "managed-v1/archive-1.enc",
            }
            return StoredManagedBackup("version-1", len(payload), digest)

    class Relay:
        async def quiesce_runner(self, *_args, **_values) -> None:
            return None

        async def release_maintenance(self, *_args, **_values) -> None:
            return None

    recorded: list[str] = []
    manager = ManagedBackupManager(
        provider=Provider(),
        store=Store(),
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
        record_upload=lambda *_args, **values: (recorded.append(values["object_version"]) or True),
        complete_creation=lambda *_args, **_values: True,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    assert await manager.run_once()
    assert recorded == ["version-1"]


@pytest.mark.asyncio
async def test_create_upload_reconciliation_refuses_abort_after_lease_loss(tmp_path) -> None:
    """A stale operation owner must not abort unfinished storage work."""
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
        object_key="managed-v1/archive-1.enc",
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
    aborted: list[str] = []

    class Store:
        async def reconcile_upload(self, **_values):
            return PendingManagedBackupUploads(("replacement-upload",))

        async def abort_uploads(self, **values) -> None:
            aborted.extend(values["upload_ids"])

    manager = ManagedBackupManager(
        provider=object(),
        store=Store(),
        relay=object(),
        wrapping_key=b"w" * 32,
        claim_operation=lambda **_values: operation,
        renew_lease=lambda **_values: False,
        get_archive=lambda _user_id, _archive_id: archive,
        get_runtime=lambda _user_id: runtime,
        get_runner=lambda _user_id: {"id": "runner-1"},
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="lease was lost"):
        await manager.run_once()

    assert aborted == []


@pytest.mark.asyncio
async def test_create_upload_reconciliation_aborts_after_lease_renewal(tmp_path) -> None:
    """Current owner should renew before exact unfinished upload cleanup."""
    import hashlib

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backup_store import (
        PendingManagedBackupUploads,
        StoredManagedBackup,
    )
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    payload = b"encrypted-archive"
    digest = hashlib.sha256(payload).hexdigest()
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
        object_key="managed-v1/archive-1.enc",
        object_version=None,
        size_bytes=len(payload),
        sha256=digest,
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
    reconciliation_calls = 0

    class Provider:
        async def download_file(self, _name, **values) -> None:
            events.append("download")
            values["target_path"].write_bytes(payload)

        async def write_file(self, *_args, **_values) -> None:
            events.append("release-file")

        async def start_service(self, *_args, **_values) -> None:
            events.append("start")

        async def delete_file(self, *_args, **_values) -> None:
            events.append("delete")

    class Store:
        async def reconcile_upload(self, **_values):
            nonlocal reconciliation_calls
            reconciliation_calls += 1
            if reconciliation_calls == 1:
                events.append("pending")
                return PendingManagedBackupUploads(("old-upload",))
            events.append("absent")
            return None

        async def abort_uploads(self, **values) -> None:
            assert values["upload_ids"] == ("old-upload",)
            events.append("abort")

        async def put_file(self, local_path, **_values):
            assert local_path.read_bytes() == payload
            events.append("upload")
            return StoredManagedBackup("version-1", len(payload), digest)

    class Relay:
        async def release_maintenance(self, *_args, **_values) -> None:
            events.append("relay-release")

    manager = ManagedBackupManager(
        provider=Provider(),
        store=Store(),
        relay=Relay(),
        wrapping_key=b"w" * 32,
        claim_operation=lambda **_values: operation,
        renew_lease=lambda **_values: events.append("renew") or True,
        get_archive=lambda _user_id, _archive_id: archive,
        get_runtime=lambda _user_id: runtime,
        get_runner=lambda _user_id: {"id": "runner-1"},
        record_upload=lambda *_args, **_values: True,
        complete_creation=lambda *_args, **_values: True,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    assert await manager.run_once()
    assert events[:6] == ["pending", "renew", "abort", "absent", "download", "upload"]


@pytest.mark.asyncio
async def test_create_upload_abort_failure_restores_source_runtime(tmp_path) -> None:
    """A failed exact upload abort must restore source services and relay access."""
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
        object_key="managed-v1/archive-1.enc",
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
        async def start_service(self, *_args, **_values) -> None:
            events.append("start")

    class Store:
        async def reconcile_upload(self, **_values):
            return PendingManagedBackupUploads(("old-upload",))

        async def abort_uploads(self, **_values) -> None:
            raise RuntimeError("abort unavailable")

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

    with pytest.raises(RuntimeError, match="abort unavailable"):
        await manager.run_once()

    assert events == ["start", "start", "relay-release"]


@pytest.mark.asyncio
async def test_create_upload_retry_reuploads_preserved_output_after_confirmed_absence(
    tmp_path,
) -> None:
    """Confirmed absence should immediately re-upload the preserved guest output."""
    import hashlib

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backup_store import StoredManagedBackup
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    payload = b"encrypted-archive"
    digest = hashlib.sha256(payload).hexdigest()
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
        object_key="managed-v1/archive-1.enc",
        object_version=None,
        size_bytes=len(payload),
        sha256=digest,
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
        async def download_file(self, _name, **values) -> None:
            events.append("guest-download")
            values["target_path"].write_bytes(payload)

        async def write_file(self, *_args, **_values) -> None:
            events.append("guest-release")

        async def start_service(self, *_args, **_values) -> None:
            events.append("start")

        async def delete_file(self, *_args, **_values) -> None:
            events.append("guest-delete")

    class Store:
        async def put_file(self, local_path, **values) -> StoredManagedBackup:
            assert local_path.read_bytes() == payload
            assert values == {
                "archive_id": "archive-1",
                "expected_sha256": digest,
                "expected_size": len(payload),
                "object_key": "managed-v1/archive-1.enc",
            }
            events.append("storage-upload")
            return StoredManagedBackup("version-1", len(payload), digest)

        async def reconcile_upload(self, **_values) -> None:
            events.append("storage-absent")
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
        record_upload=lambda *_args, **_values: True,
        complete_creation=lambda *_args, **_values: True,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    assert await manager.run_once()
    assert events == [
        "storage-absent",
        "guest-download",
        "storage-upload",
        "guest-release",
        "start",
        "start",
        "relay-release",
        "guest-delete",
        "guest-delete",
        "guest-delete",
        "guest-delete",
    ]


@pytest.mark.asyncio
async def test_create_upload_publication_failure_keeps_version_for_retry(tmp_path) -> None:
    """A failed catalog publication must retain exact object metadata for retry."""
    import hashlib
    import json

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backup_store import StoredManagedBackup
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    payload = b"encrypted-archive"
    digest = hashlib.sha256(payload).hexdigest()
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
    cleanup_versions: list[str] = []

    class Provider:
        async def stop_service(self, *_args, **_values) -> None:
            return None

        async def write_file(self, *_args, **_values) -> None:
            return None

        async def configure_service(self, *_args, **_values) -> None:
            return None

        async def start_service(self, *_args, **_values) -> None:
            return None

        async def read_file(self, *_args, **_values) -> bytes:
            return json.dumps(
                {
                    "job_id": operation.job_id,
                    "sha256": digest,
                    "size_bytes": len(payload),
                    "status": "ready",
                }
            ).encode()

        async def download_file(self, _name, **values) -> None:
            values["target_path"].write_bytes(payload)

        async def delete_file(self, *_args, **_values) -> None:
            return None

    class Store:
        async def put_file(self, *_args, **_values) -> StoredManagedBackup:
            return StoredManagedBackup("version-1", len(payload), digest)

        async def delete_file(self, **values) -> None:
            cleanup_versions.append(values["object_version"])
            raise RuntimeError("storage cleanup unavailable")

    class Relay:
        async def quiesce_runner(self, *_args, **_values) -> None:
            return None

        async def release_maintenance(self, *_args, **_values) -> None:
            return None

    recorded: list[str] = []
    manager = ManagedBackupManager(
        provider=Provider(),
        store=Store(),
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
        record_upload_intent=lambda **_values: True,
        record_upload=lambda *_args, **values: (recorded.append(values["object_version"]) or False),
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    assert await manager.run_once()
    assert recorded == ["version-1"]
    assert cleanup_versions == []


@pytest.mark.asyncio
async def test_create_upload_failure_retains_guest_output_after_runtime_recovery(
    tmp_path,
) -> None:
    """A transfer failure after quiescence must restore source availability."""
    import hashlib
    import json

    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    payload = b"encrypted-archive"
    digest = hashlib.sha256(payload).hexdigest()
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
        object_key="managed/archive.enc",
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
    events: list[str] = []

    class Provider:
        async def stop_service(self, *_args, **_values) -> None:
            return None

        async def write_file(self, *_args, **_values) -> None:
            return None

        async def configure_service(self, *_args, **_values) -> None:
            return None

        async def delete_file(self, *_args, **_values) -> None:
            events.append("cleanup")

        async def start_service(self, _name, **values) -> None:
            events.append(f"start:{values['service_name']}")

        async def read_file(self, *_args, **_values) -> bytes:
            return json.dumps(
                {
                    "job_id": operation.job_id,
                    "sha256": digest,
                    "size_bytes": len(payload),
                    "status": "ready",
                }
            ).encode()

        async def download_file(self, _name, **values) -> None:
            values["target_path"].write_bytes(payload)

    class Store:
        async def put_file(self, *_args, **_values):
            raise RuntimeError("object storage unavailable")

    class Relay:
        async def quiesce_runner(self, *_args, **_values) -> None:
            return None

        async def release_maintenance(self, *_args, **_values) -> None:
            events.append("release")

    manager = ManagedBackupManager(
        provider=Provider(),
        store=Store(),
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
        record_upload_intent=lambda **_values: True,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="object storage unavailable"):
        await manager.run_once()

    assert "start:yinshi-sidecar" in events
    assert "start:yinshi-runner" in events
    assert "release" in events
    assert "cleanup" not in events
