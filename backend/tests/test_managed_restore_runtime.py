"""Tests for private replacement Sprite provisioning during managed restore."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_restore_candidate_provisioning_reuses_persisted_candidate_authority() -> None:
    """Crash retry should reuse the exact recorded candidate instead of rotating authority."""
    from yinshi.services.managed_runtime_manager import ManagedRuntimeManager

    provider = Mock()
    provider.get_sprite = AsyncMock(return_value=SimpleNamespace(name="candidate-sprite"))
    provider.stop_service = AsyncMock()
    installer = Mock()
    installer.install = AsyncMock()
    manager = ManagedRuntimeManager(
        provider=provider,
        guest_installer=installer,
        http_client=Mock(),
        name_prefix="managed",
        name_key="secret",
        artifact_url="https://artifact.invalid/runner.tar.gz",
        artifact_sha256="a" * 64,
        artifact_version="runner-v1",
        allowed_domains=("control.example",),
        region="ord",
        control_url="https://control.example",
        readiness_timeout_seconds=5,
        is_runner_connected=lambda runner_id: runner_id == "candidate-runner",
        clock=lambda: datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
        sleep=lambda _seconds: asyncio.sleep(0),
        register_sprite_identity=lambda **_values: None,
    )
    manager._create_restore_registration = Mock(side_effect=AssertionError("rotated authority"))
    manager._get_restore_runner = Mock(
        return_value={
            "id": "candidate-runner",
            "kind": "managed_restore",
            "status": "online",
            "registered_at": "2026-08-12T12:00:00Z",
            "last_heartbeat_at": "2026-08-12T12:00:00Z",
            "noise_public_key": "candidate-key",
            "noise_key_confirmed": True,
            "capabilities": {"artifact_sha256": "a" * 64},
        }
    )

    candidate = await manager.provision_restore_candidate(
        "user-1",
        job_id="job-1",
        candidate_sprite_name="candidate-sprite",
        candidate_runner_id="candidate-runner",
    )

    assert candidate.runner_id == "candidate-runner"
    manager._get_restore_runner.assert_called_once_with("user-1", "job-1")
    manager._create_restore_registration.assert_not_called()
    installer.install.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_restore_candidate_adoption_registers_exact_owner(
    tmp_path, monkeypatch
) -> None:
    """Successful adoption must leave exact durable ownership for reconciliation."""
    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db
    from yinshi.services.managed_runtime_manager import ManagedRuntimeManager
    from yinshi.services.managed_sprite_registry import list_managed_sprite_identities

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

    provider = Mock()
    provider.get_sprite = AsyncMock(return_value=SimpleNamespace(name="candidate-sprite"))
    manager = ManagedRuntimeManager(
        provider=provider,
        guest_installer=Mock(),
        http_client=Mock(),
        name_prefix="managed",
        name_key="secret",
        artifact_url="https://artifact.invalid/runner.tar.gz",
        artifact_sha256="a" * 64,
        artifact_version="runner-v1",
        allowed_domains=("control.example",),
        region="ord",
        control_url="https://control.example",
        readiness_timeout_seconds=5,
        is_runner_connected=lambda runner_id: runner_id == "candidate-runner",
        clock=lambda: datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    manager._get_restore_runner = Mock(
        return_value={
            "id": "candidate-runner",
            "kind": "managed_restore",
            "status": "online",
            "registered_at": "2026-08-12T12:00:00Z",
            "noise_public_key": "candidate-key",
            "noise_key_confirmed": True,
            "capabilities": {"artifact_sha256": "a" * 64},
        }
    )

    await manager.provision_restore_candidate(
        "user-1",
        job_id="job-1",
        candidate_sprite_name="candidate-sprite",
        candidate_runner_id="candidate-runner",
    )

    identities = list_managed_sprite_identities()
    assert [
        (identity.sprite_name, identity.user_id, identity.job_id) for identity in identities
    ] == [("candidate-sprite", "user-1", "job-1")]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_restore_candidate_provisioning_uses_private_non_active_runner() -> None:
    """Restore candidates should install privately and wait for fresh confirmed identity."""
    from yinshi.services.managed_runtime_manager import ManagedRuntimeManager

    events: list[str] = []
    runner_states = iter(
        (
            None,
            {
                "id": "candidate-runner",
                "kind": "managed_restore",
                "status": "online",
                "registered_at": "2026-08-12T12:00:00Z",
                "last_heartbeat_at": "2026-08-12T12:00:00Z",
                "noise_public_key": "candidate-key",
                "noise_key_confirmed": True,
                "capabilities": {"artifact_sha256": "a" * 64},
            },
        )
    )

    class Provider:
        async def get_sprite(self, name: str):
            events.append(f"get:{name}")
            return None

        async def create_sprite(self, name: str):
            events.append(f"create:{name}")
            return SimpleNamespace(name=name)

        async def set_network_policy(self, name: str, *, allowed_domains) -> None:
            events.append(f"policy:{name}:{','.join(allowed_domains)}")

        async def stop_service(
            self, name: str, *, service_name: str, timeout_seconds: float
        ) -> None:
            events.append(f"stop:{name}:{service_name}")

    class Installer:
        async def install(self, **values) -> None:
            events.append(f"install:{values['sprite_name']}")
            assert values["environment"]["YINSHI_REGISTRATION_TOKEN"] == "token"

    manager = ManagedRuntimeManager(
        provider=Provider(),
        guest_installer=Installer(),
        http_client=Mock(),
        name_prefix="managed",
        name_key="secret",
        artifact_url="https://artifact.invalid/runner.tar.gz",
        artifact_sha256="a" * 64,
        artifact_version="runner-v1",
        allowed_domains=("control.example",),
        region="ord",
        control_url="https://control.example",
        readiness_timeout_seconds=5,
        is_runner_connected=lambda runner_id: runner_id == "candidate-runner",
        clock=lambda: datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    manager._register_sprite_identity = Mock()
    manager._fetch_restore_artifact = AsyncMock(return_value=b"artifact")
    manager._create_restore_registration = Mock(
        return_value={
            "runner": {"id": "candidate-runner"},
            "environment": {"YINSHI_REGISTRATION_TOKEN": "token"},
        }
    )
    manager._get_restore_runner = Mock(side_effect=lambda _user_id, _job_id: next(runner_states))

    candidate = await manager.provision_restore_candidate(
        "user-1",
        job_id="job-1",
        candidate_sprite_name="candidate-sprite",
    )

    assert candidate.runner_id == "candidate-runner"
    assert candidate.runner_public_key == "candidate-key"
    manager._create_restore_registration.assert_called_once_with("user-1", "job-1")
    assert events == [
        "get:candidate-sprite",
        "create:candidate-sprite",
        "policy:candidate-sprite:control.example",
        "install:candidate-sprite",
        "stop:candidate-sprite:yinshi-runner",
        "stop:candidate-sprite:yinshi-sidecar",
    ]
