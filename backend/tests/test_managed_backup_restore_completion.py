"""Tests for exact durable completion after replacement activation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_restore_completion_requires_activated_owned_lease(auth_client) -> None:
    """Old-Sprite cleanup completes only the exact activated restore lease."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import (
        claim_due_managed_backup_operation,
        complete_managed_backup_restore,
        get_managed_backup_operation,
        start_managed_backup_restore,
    )
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=now,
    )
    archive_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5ea2"
    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5ea3"
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'ready', generation = 3 "
            "WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.execute(
            """INSERT INTO managed_backup_archives (
                   id, user_id, runtime_generation, status, object_key,
                   object_version, size_bytes, sha256, wrapped_key, key_id,
                   owner_digest, created_at, completed_at
               ) VALUES (?, ?, 2, 'ready', ?, 'version-1', 1024, ?, ?, ?, ?, ?, ?)""",
            (
                archive_id,
                tenant.user_id,
                "managed/v1/restore-complete.enc",
                "d" * 64,
                b"wrapped-key",
                "backup-v1",
                "c" * 64,
                "2026-08-11T12:00:00Z",
                "2026-08-11T12:01:00Z",
            ),
        )
        database.commit()
    start_managed_backup_restore(
        tenant.user_id,
        archive_id=archive_id,
        runtime_generation=3,
        job_id=job_id,
        now=now,
    )
    claimed = claim_due_managed_backup_operation(
        worker_id="worker-a",
        lease_token="lease-a",
        now=now,
        lease_expires_at=now + timedelta(minutes=2),
    )
    assert claimed is not None
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_backup_operations SET phase = 'activated' WHERE job_id = ?",
            (job_id,),
        )
        database.commit()

    assert not complete_managed_backup_restore(
        job_id=job_id,
        lease_token="stale-lease",
        runtime_generation=3,
        now=now,
    )
    assert complete_managed_backup_restore(
        job_id=job_id,
        lease_token="lease-a",
        runtime_generation=3,
        now=now,
    )
    assert get_managed_backup_operation(tenant.user_id, job_id) is None
