"""Staging recovery boundary provisions isolated runtime state and canaries."""

from __future__ import annotations

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
