"""Managed restore coordinates a replacement after confirmed source loss."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.asyncio
async def test_source_loss_restore_does_not_quiesce_deleted_source(tmp_path) -> None:
    """A source-loss operation restores and activates without contacting old services."""
    from yinshi.services.managed_backup_manager import ManagedBackupManager
    from yinshi.services.managed_backups import ManagedBackupArchive, ManagedBackupOperation
    from yinshi.services.managed_runtime_manager import OnlineManagedRunner

    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
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
        source_sprite_id="deleted-source",
        source_lost=True,
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
    contacted_sources: list[str] = []

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

        async def start_service(self, name: str, **_values) -> None:
            if name == "deleted-source":
                contacted_sources.append(name)

        async def delete_file(self, *_args, **_values) -> None:
            return None

        async def read_file(self, *_args, **_values) -> bytes:
            return b'{"cleanup_pending":false,"job_id":"job-restore","status":"restored"}'

        async def delete_sprite(self, name: str) -> None:
            if name == "deleted-source":
                contacted_sources.append(name)

    class Relay:
        async def quiesce_runner(self, runner_id: str, **_values) -> None:
            if runner_id == "runner-1":
                contacted_sources.append(runner_id)

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
        complete_restore=lambda **_values: True,
        now=lambda: now,
        new_lease_token=lambda: "lease-1",
        staging_root=tmp_path,
    )

    assert await manager.run_once()
    assert contacted_sources == []
