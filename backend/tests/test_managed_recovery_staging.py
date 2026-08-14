"""Staging recovery boundary provisions isolated runtime state and canaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_staging_boundary_writes_bounded_representative_state(monkeypatch) -> None:
    """Provisioning should create one internal tenant and exact guest fixtures."""
    from yinshi.managed_recovery_staging import StagingManagedRecoveryBoundary

    class Tenant:
        user_id = "drill-user"
        data_dir = "/tmp/drill-user"

    class Runtime:
        sprite_name = "source-sprite"
        lifecycle_status = "ready"

    class RuntimeManager:
        async def provision(self, user_id: str) -> Runtime:
            assert user_id == "drill-user"
            return Runtime()

    class Provider:
        def __init__(self) -> None:
            self.files: dict[str, bytes] = {}

        async def write_file(self, _name: str, *, path: str, content: bytes, **_values) -> None:
            self.files[path] = content

        async def read_file(self, _name: str, *, path: str, **_values) -> bytes:
            return self.files[path]

    provider = Provider()
    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.resolve_or_create_user",
        lambda *_args, **_values: Tenant(),
    )
    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.get_managed_runtime_status",
        lambda _user_id: Runtime(),
    )
    boundary = StagingManagedRecoveryBoundary(
        runtime_manager=RuntimeManager(),
        backup_manager=object(),
        provider=provider,
        store=object(),
    )

    await boundary.provision()
    await boundary.write_fixtures()

    assert set(provider.files) == {
        "/var/lib/yinshi/sqlite/drill.db",
        "/var/lib/yinshi/files/nested/canary.txt",
        "/var/lib/yinshi/files/canary.bin",
        "/var/lib/yinshi/files/empty",
    }
    assert provider.files["/var/lib/yinshi/files/empty"] == b""


@pytest.mark.asyncio
async def test_staging_boundary_verifies_sqlite_within_provider_read_limit(monkeypatch) -> None:
    """Verification should use the provider limit and validate the restored SQLite fixture."""
    from yinshi.managed_recovery_staging import StagingManagedRecoveryBoundary

    class Tenant:
        user_id = "drill-user"
        data_dir = "/tmp/drill-user"

    source_runtime = SimpleNamespace(sprite_name="source-sprite", lifecycle_status="ready")
    replacement_runtime = SimpleNamespace(
        sprite_name="replacement-sprite",
        lifecycle_status="ready",
    )
    current_runtime = source_runtime

    class RuntimeManager:
        async def provision(self, user_id: str):
            assert user_id == "drill-user"
            return source_runtime

    class Provider:
        def __init__(self) -> None:
            self.files: dict[str, bytes] = {}
            self.read_limits: dict[str, int] = {}

        async def write_file(self, _name: str, *, path: str, content: bytes, **_values) -> None:
            self.files[path] = content

        async def read_file(self, _name: str, *, path: str, max_bytes: int) -> bytes:
            if max_bytes > 10 * 1024 * 1024:
                raise ValueError("max_bytes is outside the small-file limit")
            self.read_limits[path] = max_bytes
            return self.files[path]

        async def get_sprite(self, name: str):
            assert name == "source-sprite"
            return None

    class Store:
        async def inspect_object(self, *, object_key: str):
            assert object_key == "drill-object"
            return SimpleNamespace(version_count=1, multipart_upload_ids=())

    provider = Provider()
    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.resolve_or_create_user",
        lambda *_args, **_values: Tenant(),
    )
    monkeypatch.setattr(
        "yinshi.managed_recovery_staging.get_managed_runtime_status",
        lambda _user_id: current_runtime,
    )
    boundary = StagingManagedRecoveryBoundary(
        runtime_manager=RuntimeManager(),
        backup_manager=object(),
        provider=provider,
        store=Store(),
    )

    await boundary.provision()
    await boundary.write_fixtures()
    boundary._object_key = "drill-object"
    current_runtime = replacement_runtime

    result = await boundary.verify()

    assert result == (1, 0, True, True)
    assert provider.read_limits["/var/lib/yinshi/sqlite/drill.db"] == 10 * 1024 * 1024
