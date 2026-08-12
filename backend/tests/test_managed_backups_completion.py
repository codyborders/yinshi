"""Tests for managed backup completion fencing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_completion_requires_matching_job_and_runtime_generation(auth_client) -> None:
    """Only one matching active job may publish verified object metadata."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import (
        claim_due_managed_backup_operation,
        complete_managed_backup_creation,
        get_managed_backup_archive,
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
    creation = start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=1,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e6f",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e70",
        object_key="managed/v1/018f47a2.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        now=now,
    )
    claimed = claim_due_managed_backup_operation(
        worker_id="worker-a",
        lease_token="current-lease",
        now=now,
        lease_expires_at=now + timedelta(minutes=2),
    )
    assert claimed is not None

    assert not complete_managed_backup_creation(
        tenant.user_id,
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e71",
        lease_token="current-lease",
        runtime_generation=1,
        size_bytes=4096,
        sha256="d" * 64,
        object_version="version-1",
        now=now,
    )
    assert complete_managed_backup_creation(
        tenant.user_id,
        job_id=creation.operation.job_id,
        lease_token="current-lease",
        runtime_generation=1,
        size_bytes=4096,
        sha256="d" * 64,
        object_version="version-1",
        now=now,
    )
    archive = get_managed_backup_archive(tenant.user_id, creation.archive.id)
    assert archive is not None
    assert archive.status == "ready"
    assert archive.size_bytes == 4096
    assert archive.sha256 == "d" * 64
    assert archive.object_version == "version-1"


def test_upload_publication_rejects_stale_operation_lease(auth_client) -> None:
    """A worker that lost its lease cannot publish uploaded object metadata."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import (
        claim_due_managed_backup_operation,
        get_managed_backup_archive,
        record_managed_backup_upload,
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
    creation = start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=1,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e76",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e77",
        object_key="managed/v1/lease.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        now=now,
    )
    claimed = claim_due_managed_backup_operation(
        worker_id="worker-a",
        lease_token="current-lease",
        now=now,
        lease_expires_at=now + timedelta(minutes=2),
    )
    assert claimed is not None

    assert not record_managed_backup_upload(
        tenant.user_id,
        job_id=creation.operation.job_id,
        lease_token="stale-lease",
        runtime_generation=1,
        size_bytes=4096,
        sha256="d" * 64,
        object_version="version-1",
        now=now,
    )
    archive = get_managed_backup_archive(tenant.user_id, creation.archive.id)
    assert archive is not None
    assert archive.status == "creating"


def test_ready_publication_rejects_stale_operation_lease(auth_client) -> None:
    """A worker that lost ownership cannot publish a ready archive."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import (
        claim_due_managed_backup_operation,
        complete_managed_backup_creation,
        get_managed_backup_archive,
        record_managed_backup_upload,
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
    creation = start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=1,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e78",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e79",
        object_key="managed/v1/ready-lease.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        now=now,
    )
    claimed = claim_due_managed_backup_operation(
        worker_id="worker-a",
        lease_token="current-lease",
        now=now,
        lease_expires_at=now + timedelta(minutes=2),
    )
    assert claimed is not None
    assert record_managed_backup_upload(
        tenant.user_id,
        job_id=creation.operation.job_id,
        lease_token="current-lease",
        runtime_generation=1,
        size_bytes=4096,
        sha256="d" * 64,
        object_version="version-1",
        now=now,
    )

    assert not complete_managed_backup_creation(
        tenant.user_id,
        job_id=creation.operation.job_id,
        lease_token="stale-lease",
        runtime_generation=1,
        size_bytes=4096,
        sha256="d" * 64,
        object_version="version-1",
        now=now,
    )
    archive = get_managed_backup_archive(tenant.user_id, creation.archive.id)
    assert archive is not None
    assert archive.status == "uploaded"


def test_completion_rejects_missing_object_version(auth_client) -> None:
    """A ready archive must always retain one exact immutable object version."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import (
        complete_managed_backup_creation,
        get_managed_backup_archive,
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
    creation = start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=1,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e74",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e75",
        object_key="managed/v1/018f47a4.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        now=now,
    )

    assert not complete_managed_backup_creation(
        tenant.user_id,
        job_id=creation.operation.job_id,
        lease_token="missing-version-lease",
        runtime_generation=1,
        size_bytes=4096,
        sha256="d" * 64,
        object_version=None,
        now=now,
    )
    archive = get_managed_backup_archive(tenant.user_id, creation.archive.id)
    assert archive is not None
    assert archive.status == "creating"


def test_uploaded_object_is_durable_before_publication(auth_client) -> None:
    """Catalog should retain exact object metadata while runtime recovery finishes."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import (
        claim_due_managed_backup_operation,
        complete_managed_backup_creation,
        get_managed_backup_archive,
        record_managed_backup_upload,
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
    creation = start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=1,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e72",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e73",
        object_key="managed/v1/018f47a3.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="c" * 64,
        now=now,
    )

    claimed = claim_due_managed_backup_operation(
        worker_id="worker-a",
        lease_token="current-lease",
        now=now,
        lease_expires_at=now + timedelta(minutes=2),
    )
    assert claimed is not None
    assert record_managed_backup_upload(
        tenant.user_id,
        job_id=creation.operation.job_id,
        lease_token="current-lease",
        runtime_generation=1,
        size_bytes=4096,
        sha256="d" * 64,
        object_version="version-2",
        now=now,
    )
    archive = get_managed_backup_archive(tenant.user_id, creation.archive.id)
    assert archive is not None
    assert archive.status == "uploaded"
    assert archive.object_version == "version-2"
    assert complete_managed_backup_creation(
        tenant.user_id,
        job_id=creation.operation.job_id,
        lease_token="current-lease",
        runtime_generation=1,
        size_bytes=4096,
        sha256="d" * 64,
        object_version="version-2",
        now=now,
    )
