"""Source-loss restore claims are durable and generation-fenced."""

from __future__ import annotations

from datetime import datetime, timezone


def test_start_source_loss_restore_records_deleted_source(auth_client) -> None:
    """One ready archive should produce an explicitly source-lost restore operation."""
    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import start_managed_source_loss_restore
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = auth_client.yinshi_tenant
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    runtime = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=now,
    ).runtime
    archive_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5eb1"
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'ready' WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.execute(
            """INSERT INTO managed_backup_archives (
                   id, user_id, runtime_generation, status, object_key,
                   object_version, size_bytes, sha256, wrapped_key, key_id,
                   owner_digest, created_at, completed_at
               ) VALUES (?, ?, ?, 'ready', ?, 'version-1', 1024, ?, ?, ?, ?, ?, ?)""",
            (
                archive_id,
                tenant.user_id,
                runtime.generation,
                "managed/drill.enc",
                "d" * 64,
                b"wrapped-key",
                "backup-v1",
                "c" * 64,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        database.execute("""CREATE TRIGGER require_source_loss_on_insert
               BEFORE INSERT ON managed_backup_operations
               WHEN NEW.job_id = '018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5eb2'
                    AND NEW.source_lost != 1
               BEGIN
                   SELECT RAISE(ABORT, 'source loss must be atomic');
               END""")
        database.commit()

    claim = start_managed_source_loss_restore(
        tenant.user_id,
        archive_id=archive_id,
        runtime_generation=runtime.generation,
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5eb2",
        now=now,
    )

    assert claim.operation.source_lost is True
    assert claim.operation.source_runner_id == runtime.runner_id
    assert claim.operation.source_sprite_id == runtime.sprite_name
    with get_control_db() as database:
        row = database.execute(
            "SELECT source_lost FROM managed_backup_operations WHERE job_id = ?",
            (claim.operation.job_id,),
        ).fetchone()
    assert row is not None
    assert row["source_lost"] == 1
