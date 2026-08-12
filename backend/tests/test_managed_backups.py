"""Tests for durable managed backup catalog transitions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _ready_runtime(auth_client, *, generation: int):
    from yinshi.db import get_control_db
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=datetime(2026, 8, 12, 11, 0, tzinfo=timezone.utc),
    )
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'ready', generation = ? WHERE user_id = ?",
            (generation, tenant.user_id),
        )
        database.commit()
    return tenant


def test_start_backup_creation_claims_one_active_operation(auth_client) -> None:
    """Concurrent creation attempts should share one durable user-level exclusion."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import (
        ManagedBackupConflictError,
        start_managed_backup_creation,
    )
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=datetime(2026, 8, 12, 11, 0, tzinfo=timezone.utc),
    )
    with get_control_db() as database:
        database.execute(
            """UPDATE managed_runtimes
               SET lifecycle_status = 'ready', generation = 3
               WHERE user_id = ?""",
            (tenant.user_id,),
        )
        database.commit()
    started_at = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    first = start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=3,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e6f",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e70",
        object_key="managed/v1/018f47a2.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="a" * 64,
        now=started_at,
    )

    assert first.archive.status == "creating"
    assert first.operation.status == "running"
    try:
        start_managed_backup_creation(
            tenant.user_id,
            runtime_generation=3,
            archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e71",
            job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e72",
            object_key="managed/v1/018f47a3.enc",
            wrapped_key=b"other-key",
            key_id="backup-v1",
            owner_digest="a" * 64,
            now=started_at,
        )
    except ManagedBackupConflictError:
        pass
    else:
        raise AssertionError("concurrent managed backup operation was accepted")


def test_failed_operation_does_not_block_a_retry(auth_client) -> None:
    """A failed catalog operation should permit a new backup claim."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import start_managed_backup_creation

    tenant = _ready_runtime(auth_client, generation=2)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=2,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e6f",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e70",
        object_key="managed/v1/failed.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="a" * 64,
        now=now,
    )
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_backup_operations SET status = 'failed' WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.commit()

    retry = start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=2,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e71",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e72",
        object_key="managed/v1/retry.enc",
        wrapped_key=b"retry-key",
        key_id="backup-v1",
        owner_digest="a" * 64,
        now=now,
    )

    assert retry.operation.status == "running"


def test_claim_due_operation_uses_expiring_owner_token(auth_client) -> None:
    """Only one worker should own resumable external work before lease expiry."""
    from datetime import timedelta

    from yinshi.services.managed_backups import (
        claim_due_managed_backup_operation,
        start_managed_backup_creation,
    )

    tenant = _ready_runtime(auth_client, generation=2)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    creation = start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=2,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e87",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e88",
        object_key="managed/v1/leased.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="a" * 64,
        now=now,
    )

    claimed = claim_due_managed_backup_operation(
        worker_id="worker-a",
        lease_token="lease-a",
        now=now,
        lease_expires_at=now + timedelta(minutes=2),
    )
    competing = claim_due_managed_backup_operation(
        worker_id="worker-b",
        lease_token="lease-b",
        now=now,
        lease_expires_at=now + timedelta(minutes=2),
    )

    assert claimed is not None
    assert claimed.job_id == creation.operation.job_id
    assert claimed.lease_owner == "worker-a"
    assert claimed.lease_token == "lease-a"
    assert competing is None


def test_advance_operation_requires_current_lease_phase_and_generation(
    auth_client,
) -> None:
    """Stale workers should not advance durable external-effect boundaries."""
    from datetime import timedelta

    from yinshi.services.managed_backups import (
        advance_managed_backup_operation,
        claim_due_managed_backup_operation,
        start_managed_backup_creation,
    )

    tenant = _ready_runtime(auth_client, generation=2)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    creation = start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=2,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e89",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e8a",
        object_key="managed/v1/advance.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="a" * 64,
        now=now,
    )
    claim_due_managed_backup_operation(
        worker_id="worker-a",
        lease_token="lease-a",
        now=now,
        lease_expires_at=now + timedelta(minutes=2),
    )

    assert not advance_managed_backup_operation(
        job_id=creation.operation.job_id,
        lease_token="stale-token",
        runtime_generation=2,
        expected_phase="claimed",
        next_phase="quiesced",
        now=now,
    )
    assert advance_managed_backup_operation(
        job_id=creation.operation.job_id,
        lease_token="lease-a",
        runtime_generation=2,
        expected_phase="claimed",
        next_phase="quiesced",
        now=now,
    )


def test_expired_operation_lease_can_be_reclaimed(auth_client) -> None:
    """A crashed worker should lose job ownership after the exact lease deadline."""
    from datetime import timedelta

    from yinshi.services.managed_backups import (
        claim_due_managed_backup_operation,
        start_managed_backup_creation,
    )

    tenant = _ready_runtime(auth_client, generation=2)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=2,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e8b",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e8c",
        object_key="managed/v1/reclaim.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="a" * 64,
        now=now,
    )
    first = claim_due_managed_backup_operation(
        worker_id="worker-a",
        lease_token="lease-a",
        now=now,
        lease_expires_at=now + timedelta(minutes=2),
    )
    reclaimed = claim_due_managed_backup_operation(
        worker_id="worker-b",
        lease_token="lease-b",
        now=now + timedelta(minutes=3),
        lease_expires_at=now + timedelta(minutes=5),
    )

    assert first is not None
    assert reclaimed is not None
    assert reclaimed.job_id == first.job_id
    assert reclaimed.lease_token == "lease-b"
    assert reclaimed.attempt_count == 2


def test_get_operation_returns_only_tenant_owned_job(auth_client) -> None:
    """Job status lookup should hide another tenant's maintenance work."""
    from yinshi.services.managed_backups import (
        get_managed_backup_operation,
        start_managed_backup_creation,
    )

    tenant = _ready_runtime(auth_client, generation=2)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    creation = start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=2,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e91",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e92",
        object_key="managed/v1/status.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="a" * 64,
        now=now,
    )

    operation = get_managed_backup_operation(
        tenant.user_id,
        creation.operation.job_id,
    )

    assert operation is not None
    assert operation.job_id == creation.operation.job_id
    assert get_managed_backup_operation("other-user", operation.job_id) is None


def test_running_operation_exposes_runtime_maintenance_fence(auth_client) -> None:
    """Capability issuance should observe the durable per-user maintenance fence."""
    from datetime import datetime, timezone

    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import (
        managed_backup_operation_is_running,
        start_managed_backup_creation,
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
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'ready' WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.commit()
    start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=1,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e75",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e76",
        object_key="managed/v1/fence.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        now=now,
    )

    assert managed_backup_operation_is_running(tenant.user_id)


def test_list_archives_returns_only_tenant_owned_safe_catalog_rows(auth_client) -> None:
    """Catalog listing should stay tenant-scoped and omit private storage metadata."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import list_managed_backup_archives

    tenant = getattr(auth_client, "yinshi_tenant")
    with get_control_db() as database:
        database.execute(
            """INSERT INTO managed_backup_archives (
                   id, user_id, runtime_generation, status, object_key,
                   object_version, size_bytes, sha256, wrapped_key, key_id,
                   owner_digest, created_at, completed_at
               ) VALUES (?, ?, 1, 'ready', ?, ?, 1024, ?, ?, ?, ?, ?, ?)""",
            (
                "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e79",
                tenant.user_id,
                "private/object.enc",
                "private-version",
                "d" * 64,
                b"private-key",
                "backup-v1",
                "c" * 64,
                "2026-08-12T12:00:00Z",
                "2026-08-12T12:01:00Z",
            ),
        )
        database.commit()

    archives = list_managed_backup_archives(tenant.user_id)

    assert len(archives) == 1
    assert archives[0].id == "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e79"
    assert archives[0].status == "ready"


def test_creation_failure_releases_fence_and_preserves_safe_error(auth_client) -> None:
    """Failed create work should release runtime access and leave retryable catalog state."""
    from yinshi.services.managed_backups import (
        fail_managed_backup_creation,
        get_managed_backup_archive,
        managed_backup_operation_is_running,
        start_managed_backup_creation,
    )

    tenant = _ready_runtime(auth_client, generation=2)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    creation = start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=2,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e7c",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e7d",
        object_key="managed/v1/failed-clean.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="a" * 64,
        now=now,
    )

    assert fail_managed_backup_creation(
        tenant.user_id,
        job_id=creation.operation.job_id,
        runtime_generation=2,
        error_code="provider_unavailable",
        now=now,
    )
    archive = get_managed_backup_archive(tenant.user_id, creation.archive.id)
    assert archive is not None
    assert archive.status == "failed"
    assert archive.last_error == "provider_unavailable"
    assert not managed_backup_operation_is_running(tenant.user_id)


def test_start_restore_requires_ready_archive_and_claims_same_user_fence(
    auth_client,
) -> None:
    """Restore claims should bind one ready archive to an exact runtime generation."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import start_managed_backup_restore

    tenant = _ready_runtime(auth_client, generation=3)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    archive_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e7e"
    with get_control_db() as database:
        database.execute(
            """INSERT INTO managed_backup_archives (
                   id, user_id, runtime_generation, status, object_key,
                   object_version, size_bytes, sha256, wrapped_key, key_id,
                   owner_digest, created_at, completed_at
               ) VALUES (?, ?, 2, 'ready', ?, 'version-1', 1024, ?, ?, ?, ?, ?, ?)""",
            (
                archive_id,
                tenant.user_id,
                "managed/v1/restore.enc",
                "d" * 64,
                b"wrapped-key",
                "backup-v1",
                "c" * 64,
                "2026-08-11T12:00:00Z",
                "2026-08-11T12:01:00Z",
            ),
        )
        database.commit()

    claim = start_managed_backup_restore(
        tenant.user_id,
        archive_id=archive_id,
        runtime_generation=3,
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e7f",
        now=now,
    )

    assert claim.archive.id == archive_id
    assert claim.operation.operation == "restore"
    assert claim.operation.runtime_generation == 3


def test_restore_candidate_metadata_is_idempotent_for_exact_owned_identity(
    auth_client,
) -> None:
    """Crash recovery should reuse the same persisted candidate without rotation."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import (
        claim_due_managed_backup_operation,
        record_managed_backup_candidate,
        start_managed_backup_restore,
    )

    tenant = _ready_runtime(auth_client, generation=3)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    archive_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5ea2"
    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5ea3"
    with get_control_db() as database:
        database.execute(
            """INSERT INTO managed_backup_archives (
                   id, user_id, runtime_generation, status, object_key,
                   object_version, size_bytes, sha256, wrapped_key, key_id,
                   owner_digest, created_at, completed_at
               ) VALUES (?, ?, 2, 'ready', ?, 'version-1', 1024, ?, ?, ?, ?, ?, ?)""",
            (
                archive_id,
                tenant.user_id,
                "managed/v1/candidate-reuse.enc",
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
    values = {
        "job_id": job_id,
        "lease_token": "lease-a",
        "runtime_generation": 3,
        "candidate_runner_id": "candidate-runner",
        "candidate_sprite_id": "candidate-sprite",
        "now": now,
    }

    assert record_managed_backup_candidate(**values)
    assert record_managed_backup_candidate(**values)


def test_restore_candidate_metadata_requires_exact_owned_lease(auth_client) -> None:
    """Only the current restore worker may persist candidate provider identity."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import (
        claim_due_managed_backup_operation,
        get_managed_backup_operation,
        record_managed_backup_candidate,
        start_managed_backup_restore,
    )

    tenant = _ready_runtime(auth_client, generation=3)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    archive_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5ea0"
    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5ea1"
    with get_control_db() as database:
        database.execute(
            """INSERT INTO managed_backup_archives (
                   id, user_id, runtime_generation, status, object_key,
                   object_version, size_bytes, sha256, wrapped_key, key_id,
                   owner_digest, created_at, completed_at
               ) VALUES (?, ?, 2, 'ready', ?, 'version-1', 1024, ?, ?, ?, ?, ?, ?)""",
            (
                archive_id,
                tenant.user_id,
                "managed/v1/candidate.enc",
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

    assert not record_managed_backup_candidate(
        job_id=job_id,
        lease_token="stale-lease",
        runtime_generation=3,
        candidate_runner_id="candidate-runner",
        candidate_sprite_id="candidate-sprite",
        now=now,
    )
    assert record_managed_backup_candidate(
        job_id=job_id,
        lease_token="lease-a",
        runtime_generation=3,
        candidate_runner_id="candidate-runner",
        candidate_sprite_id="candidate-sprite",
        now=now,
    )
    operation = get_managed_backup_operation(tenant.user_id, job_id)
    assert operation is not None
    assert operation.phase == "candidate_provisioning"
    assert operation.candidate_runner_id == "candidate-runner"
    assert operation.candidate_sprite_id == "candidate-sprite"
    assert operation.source_runner_id is not None
    assert operation.source_sprite_id is not None


def test_start_archive_deletion_requires_ready_exact_version(auth_client) -> None:
    """Delete claims should retain exact storage metadata until object deletion succeeds."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import start_managed_backup_deletion

    tenant = _ready_runtime(auth_client, generation=4)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    archive_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e80"
    with get_control_db() as database:
        database.execute(
            """INSERT INTO managed_backup_archives (
                   id, user_id, runtime_generation, status, object_key,
                   object_version, size_bytes, sha256, wrapped_key, key_id,
                   owner_digest, created_at, completed_at
               ) VALUES (?, ?, 2, 'ready', ?, 'version-2', 1024, ?, ?, ?, ?, ?, ?)""",
            (
                archive_id,
                tenant.user_id,
                "managed/v1/delete.enc",
                "d" * 64,
                b"wrapped-key",
                "backup-v1",
                "c" * 64,
                "2026-08-11T12:00:00Z",
                "2026-08-11T12:01:00Z",
            ),
        )
        database.commit()

    claim = start_managed_backup_deletion(
        tenant.user_id,
        archive_id=archive_id,
        runtime_generation=4,
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e81",
        now=now,
    )

    assert claim.archive.status == "deleting"
    assert claim.archive.object_version == "version-2"
    assert claim.operation.operation == "delete"


def test_archive_deletion_rejects_stale_operation_lease(auth_client) -> None:
    """A stale worker cannot erase wrapped key material after remote deletion."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import (
        claim_due_managed_backup_operation,
        complete_managed_backup_deletion,
        get_managed_backup_archive,
        start_managed_backup_deletion,
    )

    tenant = _ready_runtime(auth_client, generation=5)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    archive_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e88"
    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e89"
    with get_control_db() as database:
        database.execute(
            """INSERT INTO managed_backup_archives (
                   id, user_id, runtime_generation, status, object_key,
                   object_version, size_bytes, sha256, wrapped_key, key_id,
                   owner_digest, created_at, completed_at
               ) VALUES (?, ?, 2, 'ready', ?, 'version-3', 1024, ?, ?, ?, ?, ?, ?)""",
            (
                archive_id,
                tenant.user_id,
                "managed/v1/stale-delete.enc",
                "d" * 64,
                b"wrapped-key",
                "backup-v1",
                "c" * 64,
                "2026-08-11T12:00:00Z",
                "2026-08-11T12:01:00Z",
            ),
        )
        database.commit()
    start_managed_backup_deletion(
        tenant.user_id,
        archive_id=archive_id,
        runtime_generation=5,
        job_id=job_id,
        now=now,
    )
    claimed = claim_due_managed_backup_operation(
        worker_id="worker-a",
        lease_token="current-lease",
        now=now,
        lease_expires_at=now + timedelta(minutes=2),
    )
    assert claimed is not None

    assert not complete_managed_backup_deletion(
        tenant.user_id,
        job_id=job_id,
        lease_token="stale-lease",
        runtime_generation=5,
        now=now,
    )
    archive = get_managed_backup_archive(tenant.user_id, archive_id)
    assert archive is not None
    assert archive.status == "deleting"
    assert archive.wrapped_key == b"wrapped-key"


def test_complete_archive_deletion_erases_wrapped_key_after_remote_delete(
    auth_client,
) -> None:
    """Catalog completion should cryptographically retire a deleted archive."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import (
        claim_due_managed_backup_operation,
        complete_managed_backup_deletion,
        get_managed_backup_archive,
        start_managed_backup_deletion,
    )

    tenant = _ready_runtime(auth_client, generation=5)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    archive_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e82"
    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e83"
    with get_control_db() as database:
        database.execute(
            """INSERT INTO managed_backup_archives (
                   id, user_id, runtime_generation, status, object_key,
                   object_version, size_bytes, sha256, wrapped_key, key_id,
                   owner_digest, created_at, completed_at
               ) VALUES (?, ?, 2, 'ready', ?, 'version-3', 1024, ?, ?, ?, ?, ?, ?)""",
            (
                archive_id,
                tenant.user_id,
                "managed/v1/deleted.enc",
                "d" * 64,
                b"wrapped-key",
                "backup-v1",
                "c" * 64,
                "2026-08-11T12:00:00Z",
                "2026-08-11T12:01:00Z",
            ),
        )
        database.commit()
    start_managed_backup_deletion(
        tenant.user_id,
        archive_id=archive_id,
        runtime_generation=5,
        job_id=job_id,
        now=now,
    )

    claimed = claim_due_managed_backup_operation(
        worker_id="worker-a",
        lease_token="current-lease",
        now=now,
        lease_expires_at=now + timedelta(minutes=2),
    )
    assert claimed is not None
    assert complete_managed_backup_deletion(
        tenant.user_id,
        job_id=job_id,
        lease_token="current-lease",
        runtime_generation=5,
        now=now,
    )
    archive = get_managed_backup_archive(tenant.user_id, archive_id)
    assert archive is not None
    assert archive.status == "deleted"
    assert archive.wrapped_key == b""
    assert archive.object_version is None


def test_retention_selects_only_old_ready_archives_without_active_restore(
    auth_client,
) -> None:
    """Retention should return bounded exact-version archives outside active work."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import list_managed_backup_retention_candidates

    tenant = getattr(auth_client, "yinshi_tenant")
    with get_control_db() as database:
        for archive_id, created_at, status in (
            ("018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e84", "2026-07-01T00:00:00Z", "ready"),
            ("018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e85", "2026-08-11T00:00:00Z", "ready"),
            ("018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e86", "2026-06-01T00:00:00Z", "failed"),
        ):
            database.execute(
                """INSERT INTO managed_backup_archives (
                       id, user_id, runtime_generation, status, object_key,
                       object_version, size_bytes, sha256, wrapped_key, key_id,
                       owner_digest, created_at, completed_at
                   ) VALUES (?, ?, 1, ?, ?, 'version-retain', 1024, ?, ?, ?, ?, ?, ?)""",
                (
                    archive_id,
                    tenant.user_id,
                    status,
                    f"managed/v1/{archive_id}.enc",
                    "d" * 64,
                    b"wrapped-key",
                    "backup-v1",
                    "c" * 64,
                    created_at,
                    created_at,
                ),
            )
        database.commit()

    candidates = list_managed_backup_retention_candidates(
        cutoff="2026-08-01T00:00:00Z",
        limit=10,
    )

    assert [archive.id for archive in candidates] == ["018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e84"]
