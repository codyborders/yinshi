"""Managed Sprite inventory reconciliation behavior tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from yinshi.services.sprites import SpriteInventoryRecord, SpriteRecord


class FakeProvider:
    """Record inventory and deletion operations at the provider boundary."""

    def __init__(self, records: tuple[SpriteRecord, ...]) -> None:
        self.records = records
        self.deleted: list[str] = []

    async def list_sprites(self, *, prefix: str) -> tuple[SpriteInventoryRecord, ...]:
        return tuple(
            SpriteInventoryRecord(record.name)
            for record in self.records
            if record.name.startswith(prefix)
        )

    async def get_sprite(self, name: str) -> SpriteRecord | None:
        return next((record for record in self.records if record.name == name), None)

    async def delete_sprite(self, name: str) -> None:
        self.deleted.append(name)


@pytest.mark.asyncio
async def test_reconcile_ignores_unregistered_matching_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider names never establish deployment ownership by themselves."""
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.managed_sprite_reconciliation import ManagedSpriteReconciler

    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "disabled")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    get_settings.cache_clear()
    init_control_db()

    old = datetime(2026, 8, 12, tzinfo=timezone.utc)
    provider = FakeProvider((SpriteRecord("foreign", "yinshi-foreign", "cold", old),))
    reconciler = ManagedSpriteReconciler(
        provider=provider,
        name_prefix="yinshi",
        restore_name_prefix="yinshi-restore",
        restore_name_key="sprite-name-key",
        grace=timedelta(hours=1),
        clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
    )

    result = await reconciler.reconcile_once()

    assert result.deleted == ()
    assert provider.deleted == []
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_reconcile_retains_durable_references_and_deletes_old_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime, operation, and unpublished candidate names must survive cleanup."""
    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db
    from yinshi.services.managed_runners import managed_sprite_name
    from yinshi.services.managed_sprite_reconciliation import ManagedSpriteReconciler
    from yinshi.services.managed_sprite_registry import register_managed_sprite_identity

    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "disabled")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    get_settings.cache_clear()
    init_control_db()

    restore_prefix = "yinshi-restore"
    key = "sprite-name-key"
    candidate_name = managed_sprite_name("user-1:job-1", prefix=restore_prefix, secret_key=key)
    with get_control_db() as database:
        database.execute(
            "INSERT INTO users (id, email, display_name) VALUES (?, ?, ?)",
            ("user-1", "user@example.com", "User"),
        )
        database.execute(
            """INSERT INTO user_runners
               (id, user_id, kind, name, cloud_provider, region, status)
               VALUES (?, ?, 'managed', ?, 'fly_sprites', 'ord', 'online')""",
            ("runner-1", "user-1", "Managed"),
        )
        database.execute(
            """INSERT INTO managed_runtimes
               (user_id, runner_id, provider_name, sprite_external_id,
                lifecycle_status, generation, artifact_version)
               VALUES (?, ?, 'fly_sprites', ?, 'ready', 1, 'version')""",
            ("user-1", "runner-1", "yinshi-runtime"),
        )
        database.execute(
            """INSERT INTO managed_backup_archives
               (id, user_id, runtime_generation, status, object_key, wrapped_key,
                key_id, owner_digest, created_at)
               VALUES (?, ?, 1, 'ready', ?, X'00', 'key', ?, ?)""",
            ("archive-1", "user-1", "object", "a" * 64, "2026-08-01T00:00:00Z"),
        )
        database.execute(
            """INSERT INTO managed_backup_operations
               (user_id, job_id, archive_id, operation, status, runtime_generation,
                phase, source_sprite_id, started_at, updated_at)
               VALUES (?, ?, ?, 'restore', 'running', 1, 'claimed', ?, ?, ?)""",
            (
                "user-1",
                "job-1",
                "archive-1",
                "yinshi-source",
                "2026-08-13T00:00:00Z",
                "2026-08-13T00:00:00Z",
            ),
        )
        database.commit()

    old = datetime(2026, 8, 12, tzinfo=timezone.utc)
    for sprite_name, identity_kind, job_id, lifecycle_status in (
        ("yinshi-runtime", "runtime", None, "active"),
        ("yinshi-source", "runtime", None, "retired"),
        (candidate_name, "restore_candidate", "job-1", "creating"),
        ("yinshi-orphan", "runtime", None, "retired"),
    ):
        register_managed_sprite_identity(
            sprite_name=sprite_name,
            identity_kind=identity_kind,
            user_id="user-1",
            job_id=job_id,
            lifecycle_status=lifecycle_status,
            now=old,
        )
    provider = FakeProvider(
        (
            SpriteRecord("runtime", "yinshi-runtime", "cold", old),
            SpriteRecord("source", "yinshi-source", "cold", old),
            SpriteRecord("candidate", candidate_name, "cold", old),
            SpriteRecord("orphan", "yinshi-orphan", "cold", old),
        )
    )
    reconciler = ManagedSpriteReconciler(
        provider=provider,
        name_prefix="yinshi",
        restore_name_prefix=restore_prefix,
        restore_name_key=key,
        grace=timedelta(hours=1),
        clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
    )

    result = await reconciler.reconcile_once()

    assert result.deleted == ("yinshi-orphan",)
    assert provider.deleted == ["yinshi-orphan"]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_reconcile_revokes_orphan_restore_authority_before_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting an orphan candidate must revoke its remaining runner authority."""
    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db
    from yinshi.services.managed_runners import managed_sprite_name
    from yinshi.services.managed_sprite_reconciliation import ManagedSpriteReconciler
    from yinshi.services.managed_sprite_registry import register_managed_sprite_identity

    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "disabled")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    get_settings.cache_clear()
    init_control_db()

    restore_prefix = "yinshi-restore"
    restore_key = "sprite-name-key"
    candidate_name = managed_sprite_name(
        "user-1:job-orphan",
        prefix=restore_prefix,
        secret_key=restore_key,
    )
    with get_control_db() as database:
        database.execute(
            "INSERT INTO users (id, email, display_name) VALUES (?, ?, ?)",
            ("user-1", "user@example.com", "User"),
        )
        database.execute(
            """INSERT INTO user_runners
               (id, user_id, kind, name, cloud_provider, region, status, restore_job_id)
               VALUES (?, ?, 'managed_restore', ?, 'fly_sprites', 'ord', 'online', ?)""",
            ("runner-orphan", "user-1", "Restore", "job-orphan"),
        )
        database.commit()

    old = datetime(2026, 8, 12, tzinfo=timezone.utc)
    register_managed_sprite_identity(
        sprite_name=candidate_name,
        identity_kind="restore_candidate",
        user_id="user-1",
        job_id="job-orphan",
        lifecycle_status="retired",
        now=old,
    )
    provider = FakeProvider((SpriteRecord("candidate", candidate_name, "cold", old),))
    reconciler = ManagedSpriteReconciler(
        provider=provider,
        name_prefix="yinshi",
        restore_name_prefix=restore_prefix,
        restore_name_key=restore_key,
        grace=timedelta(hours=1),
        clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
    )

    await reconciler.reconcile_once()

    with get_control_db() as database:
        runner = database.execute(
            "SELECT status, revoked_at FROM user_runners WHERE id = ?",
            ("runner-orphan",),
        ).fetchone()
    assert provider.deleted == [candidate_name]
    assert runner["status"] == "revoked"
    assert runner["revoked_at"] is not None
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_reconcile_defers_young_orphan_and_rechecks_before_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grace and immediate durable ownership checks must prevent unsafe deletion."""
    import yinshi.services.managed_sprite_reconciliation as reconciliation
    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db
    from yinshi.services.managed_sprite_registry import register_managed_sprite_identity

    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "disabled")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    get_settings.cache_clear()
    init_control_db()

    with get_control_db() as database:
        database.execute(
            "INSERT INTO users (id, email, display_name) VALUES (?, ?, ?)",
            ("user-1", "user@example.com", "User"),
        )
        database.commit()
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    for sprite_name, created_at in (
        ("yinshi-young", now - timedelta(minutes=5)),
        ("yinshi-concurrent", now - timedelta(days=1)),
    ):
        register_managed_sprite_identity(
            sprite_name=sprite_name,
            identity_kind="runtime",
            user_id="user-1",
            job_id=None,
            lifecycle_status="retired",
            now=created_at,
        )
    provider = FakeProvider(
        (
            SpriteRecord("young", "yinshi-young", "cold", now - timedelta(minutes=5)),
            SpriteRecord("old", "yinshi-concurrent", "cold", now - timedelta(days=1)),
        )
    )
    original = reconciliation._managed_sprite_references
    calls = 0

    def references(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        result = original(**kwargs)
        if calls > 1:
            return reconciliation._ManagedSpriteReferences(
                result.names | {"yinshi-concurrent"}, result.restore_jobs_by_name
            )
        return result

    monkeypatch.setattr(reconciliation, "_managed_sprite_references", references)
    reconciler = reconciliation.ManagedSpriteReconciler(
        provider=provider,
        name_prefix="yinshi",
        restore_name_prefix="yinshi-restore",
        restore_name_key="sprite-name-key",
        grace=timedelta(hours=1),
        clock=lambda: now,
    )

    result = await reconciler.reconcile_once()

    assert result.deferred == 1
    assert result.retained == 1
    assert provider.deleted == []
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_reconcile_retains_identity_after_inconsistent_provider_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One list/get inconsistency must not erase durable provider ownership."""
    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db
    from yinshi.services.managed_sprite_reconciliation import ManagedSpriteReconciler
    from yinshi.services.managed_sprite_registry import (
        list_managed_sprite_identities,
        register_managed_sprite_identity,
    )

    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "disabled")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    get_settings.cache_clear()
    init_control_db()
    with get_control_db() as database:
        database.execute(
            "INSERT INTO users (id, email, display_name) VALUES (?, ?, ?)",
            ("user-1", "user@example.com", "User"),
        )
        database.commit()
    old = datetime(2026, 8, 12, tzinfo=timezone.utc)
    register_managed_sprite_identity(
        sprite_name="yinshi-inconsistent",
        identity_kind="runtime",
        user_id="user-1",
        job_id=None,
        lifecycle_status="retired",
        now=old,
    )

    class InconsistentProvider(FakeProvider):
        async def list_sprites(self, *, prefix: str) -> tuple[SpriteInventoryRecord, ...]:
            if "yinshi-inconsistent".startswith(prefix):
                return (SpriteInventoryRecord("yinshi-inconsistent"),)
            return ()

        async def get_sprite(self, name: str) -> SpriteRecord | None:
            return None

    reconciler = ManagedSpriteReconciler(
        provider=InconsistentProvider(()),
        name_prefix="yinshi",
        restore_name_prefix="yinshi-restore",
        restore_name_key="sprite-name-key",
        grace=timedelta(hours=1),
        clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
    )

    await reconciler.reconcile_once()

    assert [identity.sprite_name for identity in list_managed_sprite_identities()] == [
        "yinshi-inconsistent"
    ]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_classified_reconcile_logs_and_reraises_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Startup and recurring callers must share one failure classification."""
    import yinshi.services.managed_sprite_reconciliation as reconciliation

    reconciler = reconciliation.ManagedSpriteReconciler(
        provider=FakeProvider(()),
        name_prefix="yinshi",
        restore_name_prefix="yinshi-restore",
        restore_name_key="sprite-name-key",
        grace=timedelta(hours=1),
    )
    monkeypatch.setattr(
        reconciler,
        "reconcile_once",
        AsyncMock(side_effect=RuntimeError("database details")),
    )

    with pytest.raises(RuntimeError, match="database details"):
        await reconciler.reconcile_classified(raise_on_failure=True)
    assert "managed_sprite_reconciliation_failed" in caplog.text


@pytest.mark.asyncio
async def test_recurring_reconcile_retries_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected pass failure must not stop later inventory passes."""
    import yinshi.services.managed_sprite_reconciliation as reconciliation

    reconciler = reconciliation.ManagedSpriteReconciler(
        provider=FakeProvider(()),
        name_prefix="yinshi",
        restore_name_prefix="yinshi-restore",
        restore_name_key="sprite-name-key",
        grace=timedelta(hours=1),
    )
    reconcile_once = AsyncMock(
        side_effect=[
            RuntimeError("unexpected database failure"),
            reconciliation.ManagedSpriteReconciliationResult(0, 0, (), 0),
        ]
    )
    monkeypatch.setattr(reconciler, "reconcile_once", reconcile_once)
    sleeps = 0

    async def sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(reconciler, "_sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await reconciler.run(interval_seconds=60)

    assert reconcile_once.await_count == 2
    assert "managed_sprite_reconciliation_failed" in caplog.text
