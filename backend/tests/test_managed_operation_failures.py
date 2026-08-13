"""Typed operation failure writes remain fenced by lease ownership."""

from __future__ import annotations

from datetime import datetime, timezone


def test_stale_lease_cannot_fail_reclaimed_operation(auth_client) -> None:
    """A previous worker must not mutate an operation after lease transfer."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import start_managed_backup_creation
    from yinshi.services.managed_operation_failures import fail_managed_backup_operation
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'ready' WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.commit()
    claim = start_managed_backup_creation(
        tenant.user_id,
        archive_id="archive-stale-worker",
        runtime_generation=1,
        job_id="job-stale-worker",
        wrapped_key=b"wrapped",
        key_id="backup-v1",
        owner_digest="a" * 64,
        object_key="objects/stale-worker",
        now=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    with get_control_db() as database:
        database.execute(
            """UPDATE managed_backup_operations
               SET lease_owner = ?, lease_token = ? WHERE job_id = ?""",
            ("current-worker", "current-token", claim.operation.job_id),
        )
        database.commit()

    changed = fail_managed_backup_operation(
        job_id=claim.operation.job_id,
        runtime_generation=1,
        lease_owner="stale-worker",
        lease_token="stale-token",
        failure_class="restore_failed",
        error_code="restore_coordination_failed",
        now=datetime(2026, 8, 13, 1, tzinfo=timezone.utc),
    )

    assert changed is False
    with get_control_db() as database:
        status = database.execute(
            "SELECT status FROM managed_backup_operations WHERE job_id = ?",
            (claim.operation.job_id,),
        ).fetchone()["status"]
    assert status == "running"
