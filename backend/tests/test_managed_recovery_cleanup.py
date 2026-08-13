"""Staging recovery cleanup removes every drill-owned durable resource."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_cleanup_rejects_remaining_delete_marker(monkeypatch, tmp_path) -> None:
    """Cleanup must not pass while any exact-object delete marker remains."""
    from yinshi.db import get_control_db, init_control_db
    from yinshi.managed_recovery_staging import StagingManagedRecoveryBoundary
    from yinshi.services.managed_backup_store import ManagedBackupObjectInventory

    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("HOST", "127.0.0.1")
    from yinshi.config import get_settings

    get_settings.cache_clear()
    init_control_db()
    with get_control_db() as database:
        database.execute(
            "INSERT INTO users (id, email) VALUES (?, ?)",
            ("drill-user", "drill@invalid.local"),
        )
        database.commit()

    class Tenant:
        user_id = "drill-user"
        data_dir = str(tmp_path / "drill-user")

    class Store:
        async def inspect_object(self, *, object_key: str):
            assert object_key == "managed/archive.enc"
            return ManagedBackupObjectInventory(
                version_count=0,
                delete_marker_count=1,
                multipart_upload_ids=(),
            )

    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.list_managed_backup_archives", lambda _user_id: ()
    )
    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.get_managed_runtime_status", lambda _user_id: None
    )
    boundary = StagingManagedRecoveryBoundary(
        runtime_manager=object(),
        backup_manager=object(),
        provider=object(),
        store=Store(),
    )
    boundary._tenant = Tenant()
    boundary._object_key = "managed/archive.enc"

    assert await boundary.cleanup() is False


@pytest.mark.asyncio
async def test_cleanup_preserves_key_until_ready_archive_is_purged(monkeypatch, tmp_path) -> None:
    """Ready archive cleanup must purge storage without normal catalog deletion."""
    from types import SimpleNamespace

    from yinshi.db import get_control_db, init_control_db
    from yinshi.managed_recovery_staging import StagingManagedRecoveryBoundary

    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("HOST", "127.0.0.1")
    from yinshi.config import get_settings

    get_settings.cache_clear()
    init_control_db()
    with get_control_db() as database:
        database.execute(
            "INSERT INTO users (id, email) VALUES (?, ?)",
            ("ready-user", "ready@invalid.local"),
        )
        database.commit()
    archive = SimpleNamespace(
        id="archive-1",
        object_key="managed/archive.enc",
        status="ready",
    )

    class Tenant:
        user_id = "ready-user"
        data_dir = str(tmp_path / "ready-user")

    class BackupManager:
        def enqueue_delete(self, *_args):
            raise AssertionError("normal deletion erased wrapped key")

    class Store:
        async def purge_object(self, **_values) -> None:
            return None

        async def inspect_object(self, **_values):
            return SimpleNamespace(
                version_count=0,
                delete_marker_count=0,
                multipart_upload_ids=(),
            )

    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.list_managed_backup_archives",
        lambda _user_id: (archive,),
    )
    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.get_managed_runtime_status", lambda _user_id: None
    )
    boundary = StagingManagedRecoveryBoundary(
        runtime_manager=object(),
        backup_manager=BackupManager(),
        provider=object(),
        store=Store(),
    )
    boundary._tenant = Tenant()
    boundary._archive_id = archive.id
    boundary._object_key = archive.object_key

    assert await boundary.cleanup() is True


@pytest.mark.asyncio
async def test_cleanup_stops_when_replacement_deletion_fails(monkeypatch, tmp_path) -> None:
    """A live replacement must retain its runtime and tenant ownership records."""
    from types import SimpleNamespace

    from yinshi.managed_recovery_staging import StagingManagedRecoveryBoundary

    runtime = SimpleNamespace(sprite_name="replacement", generation=8)
    finalized: list[bool] = []

    class Tenant:
        user_id = "drill-user"
        data_dir = str(tmp_path / "drill-user")

    class Provider:
        async def delete_sprite(self, _name: str) -> None:
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.list_managed_backup_archives", lambda _user_id: ()
    )
    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.get_managed_runtime_status",
        lambda _user_id: runtime,
    )
    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.claim_managed_runtime_deletion",
        lambda *_args: SimpleNamespace(runtime=runtime),
    )
    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.finalize_managed_runtime_deletion",
        lambda *_args: finalized.append(True) or True,
    )
    boundary = StagingManagedRecoveryBoundary(
        runtime_manager=object(),
        backup_manager=object(),
        provider=Provider(),
        store=object(),
    )
    boundary._tenant = Tenant()

    assert await boundary.cleanup() is False
    assert finalized == []
