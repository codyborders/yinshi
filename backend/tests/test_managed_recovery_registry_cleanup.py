"""Recovery cleanup removes confirmed-absent drill Sprite identities."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.mark.asyncio
async def test_cleanup_removes_confirmed_absent_registry_identity(monkeypatch, tmp_path) -> None:
    """Cleanup must not pass with a retained drill ownership record."""
    from yinshi.db import get_control_db, init_control_db
    from yinshi.managed_recovery_staging import StagingManagedRecoveryBoundary
    from yinshi.services.managed_sprite_registry import (
        list_managed_sprite_identities,
        register_managed_sprite_identity,
    )

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
    register_managed_sprite_identity(
        sprite_name="drill-source",
        identity_kind="runtime",
        user_id="drill-user",
        job_id=None,
        lifecycle_status="retired",
        now=datetime.now(timezone.utc),
    )

    class Tenant:
        user_id = "drill-user"
        data_dir = str(tmp_path / "drill-user")

    class Provider:
        async def get_sprite(self, _name: str):
            return None

    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.list_managed_backup_archives", lambda _user_id: ()
    )
    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.get_managed_runtime_status", lambda _user_id: None
    )
    boundary = StagingManagedRecoveryBoundary(
        runtime_manager=object(),
        backup_manager=object(),
        provider=Provider(),
        store=object(),
    )
    boundary._tenant = Tenant()

    assert await boundary.cleanup() is True
    assert list_managed_sprite_identities() == ()
