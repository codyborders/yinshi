"""Managed Sprite inventory reconciliation behavior tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from yinshi.services.sprites import SpriteRecord


class FakeProvider:
    """Record inventory and deletion operations at the provider boundary."""

    def __init__(self, records: tuple[SpriteRecord, ...]) -> None:
        self.records = records
        self.deleted: list[str] = []

    async def list_sprites(self, *, prefix: str) -> tuple[SpriteRecord, ...]:
        return tuple(record for record in self.records if record.name.startswith(prefix))

    async def get_sprite(self, name: str) -> SpriteRecord | None:
        return next((record for record in self.records if record.name == name), None)

    async def delete_sprite(self, name: str) -> None:
        self.deleted.append(name)


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
async def test_reconcile_defers_young_orphan_and_rechecks_before_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grace and immediate durable ownership checks must prevent unsafe deletion."""
    import yinshi.services.managed_sprite_reconciliation as reconciliation
    from yinshi.config import get_settings
    from yinshi.db import init_control_db

    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "disabled")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    get_settings.cache_clear()
    init_control_db()

    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
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
async def test_recurring_reconcile_retries_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider failure must not stop later recurring inventory passes."""
    import yinshi.services.managed_sprite_reconciliation as reconciliation
    from yinshi.services.sprites import SpritesProviderError

    reconciler = reconciliation.ManagedSpriteReconciler(
        provider=FakeProvider(()),
        name_prefix="yinshi",
        restore_name_prefix="yinshi-restore",
        restore_name_key="sprite-name-key",
        grace=timedelta(hours=1),
    )
    reconcile_once = AsyncMock(
        side_effect=[
            SpritesProviderError("unavailable"),
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
