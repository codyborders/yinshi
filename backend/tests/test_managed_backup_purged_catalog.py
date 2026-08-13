"""Tenant cleanup erases backup catalog rows after external purge."""

from __future__ import annotations

from datetime import datetime, timezone


def test_tenant_deletion_cascades_purged_archive_catalog(auth_client) -> None:
    """The retained catalog row should disappear with its internal drill tenant."""
    from yinshi.db import get_control_db

    user_id = auth_client.yinshi_tenant.user_id
    archive_id = "archive-purged"
    with get_control_db() as database:
        database.execute(
            """INSERT INTO managed_backup_archives (
                   id, user_id, runtime_generation, status, object_key,
                   object_version, wrapped_key, key_id, owner_digest, created_at
               ) VALUES (?, ?, 1, 'failed', ?, 'version-1', ?, ?, ?, ?)""",
            (
                archive_id,
                user_id,
                "managed/archive.enc",
                b"wrapped-key",
                "backup-v1",
                "d" * 64,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        database.execute("DELETE FROM users WHERE id = ?", (user_id,))
        database.commit()
        assert (
            database.execute(
                "SELECT 1 FROM managed_backup_archives WHERE id = ?", (archive_id,)
            ).fetchone()
            is None
        )
