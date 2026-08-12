"""Tests for managed Sprite provisioning coordination."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import httpx
import pytest

from yinshi.db import get_control_db
from yinshi.services.sprites import SpriteRecord

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
ARTIFACT = b"managed runner"
ARTIFACT_SHA256 = "2e532cd0f718f065282ebd8dc9f0dbdb31ae7fd1e0916467e7dfde6e1651d1ff"


class FakeProvider:
    """Record provider calls and return configured Sprite records."""

    def __init__(self, sprite: SpriteRecord | None = None) -> None:
        self.sprite = sprite
        self.calls: list[tuple[str, object]] = []
        self.fail_operation: str | None = None

    async def get_sprite(self, name: str) -> SpriteRecord | None:
        self.calls.append(("get", name))
        if self.fail_operation == "get":
            raise RuntimeError("provider secret response")
        return self.sprite

    async def create_sprite(self, name: str) -> SpriteRecord:
        self.calls.append(("create", name))
        if self.fail_operation == "create":
            raise RuntimeError("provider secret response")
        self.sprite = SpriteRecord(id="sprite-id", name=name, status="running")
        return self.sprite

    async def set_network_policy(
        self,
        name: str,
        *,
        allowed_domains: tuple[str, ...],
    ) -> None:
        self.calls.append(("policy", (name, allowed_domains)))
        if self.fail_operation == "policy":
            raise RuntimeError("provider secret response")

    async def restart_service(
        self,
        name: str,
        *,
        service_name: str,
        monitor_duration: float | None,
    ) -> None:
        self.calls.append(("restart", (name, service_name, monitor_duration)))
        if self.fail_operation == "restart":
            raise RuntimeError("provider secret response")


class FakeInstaller:
    """Record guest installation arguments and run an optional callback."""

    def __init__(self, callback: Callable[[], None] | None = None) -> None:
        self.callback = callback
        self.calls: list[dict[str, object]] = []
        self.error: Exception | None = None

    async def install(
        self,
        *,
        sprite_name: str,
        artifact: bytes,
        environment: dict[str, str],
        artifact_version: str,
        artifact_sha256: str,
    ) -> None:
        self.calls.append(
            {
                "sprite_name": sprite_name,
                "artifact": artifact,
                "environment": environment,
                "artifact_version": artifact_version,
                "artifact_sha256": artifact_sha256,
            }
        )
        if self.error is not None:
            raise self.error
        if self.callback is not None:
            self.callback()


def _manager(
    provider: FakeProvider,
    installer: FakeInstaller,
    *,
    is_runner_connected: Callable[[str], bool] = lambda runner_id: True,
    clock: Callable[[], datetime] = lambda: NOW,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    heartbeat_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
):
    from yinshi.services.managed_runtime_manager import ManagedRuntimeManager

    return ManagedRuntimeManager(
        provider=provider,
        guest_installer=installer,
        http_client=httpx.AsyncClient(),
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_url="https://artifacts.example/runner",
        artifact_sha256=ARTIFACT_SHA256,
        artifact_version="runner-v1",
        allowed_domains=("control.example",),
        region="ord",
        control_url="https://control.example",
        readiness_timeout_seconds=10.0,
        poll_interval_seconds=1.0,
        is_runner_connected=is_runner_connected,
        clock=clock,
        sleep=sleep,
        heartbeat_sleep=heartbeat_sleep,
    )


def _store_ready_runtime(
    user_id: str,
    now: datetime,
    *,
    noise_byte: bytes = b"w",
) -> tuple[str, str, str]:
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    noise_key = base64.urlsafe_b64encode(noise_byte * 32).rstrip(b"=").decode("ascii")
    claim = claim_managed_runtime_provisioning(
        user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=now,
    )
    with get_control_db() as database:
        database.execute(
            """
            UPDATE user_runners
            SET status = 'online', registered_at = ?, last_heartbeat_at = ?,
                noise_public_key = ?, noise_public_key_confirmed_at = ?
            WHERE id = ?
            """,
            (
                now.isoformat(),
                now.isoformat(),
                noise_key,
                now.isoformat(),
                claim.runtime.runner_id,
            ),
        )
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'ready' WHERE user_id = ?",
            (user_id,),
        )
        database.commit()
    return claim.runtime.runner_id, claim.runtime.sprite_name, noise_key


async def test_startup_reconciliation_fails_abandoned_without_external_calls(
    auth_client,
) -> None:
    """Startup fails interrupted provisioning without contacting external systems."""
    from yinshi.services.managed_runners import (
        claim_managed_runtime_provisioning,
        get_managed_runtime_status,
    )

    tenant = getattr(auth_client, "yinshi_tenant")
    claim = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=NOW,
    )
    provider = FakeProvider()
    installer = FakeInstaller()
    manager = _manager(provider, installer)

    reconcile = getattr(manager, "reconcile_startup", None)
    changed = 0 if reconcile is None else await reconcile()
    status = get_managed_runtime_status(tenant.user_id)
    await manager.aclose()

    assert changed == 1
    assert status is not None
    assert status.lifecycle_status == "failed"
    assert status.generation == claim.runtime.generation
    assert status.last_error == "provider_unavailable"
    assert provider.calls == []
    assert installer.calls == []


async def test_observer_returns_claim_state_without_external_calls(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh existing claim must not fetch artifacts or call the provider."""
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    existing = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=NOW,
    )
    provider = FakeProvider()
    installer = FakeInstaller()

    async def fail_fetch(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("observer must not fetch the artifact")

    monkeypatch.setattr(
        "yinshi.services.managed_runtime_manager.fetch_pinned_artifact",
        fail_fetch,
    )
    manager = _manager(provider, installer)

    status = await manager.provision(tenant.user_id)
    await manager.aclose()

    assert status == existing.runtime
    assert provider.calls == []
    assert installer.calls == []


async def test_owner_provisions_sprite_and_marks_linked_runner_ready(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claim owner installs the pinned artifact and waits for confirmed identity."""
    tenant = getattr(auth_client, "yinshi_tenant")
    noise_key = base64.urlsafe_b64encode(b"n" * 32).rstrip(b"=").decode("ascii")

    def register_runner() -> None:
        with get_control_db() as database:
            database.execute(
                """
                UPDATE user_runners
                SET status = 'online', registered_at = ?, last_heartbeat_at = ?,
                    noise_public_key = ?, noise_public_key_confirmed_at = ?,
                    capabilities_json = ?
                WHERE user_id = ? AND kind = 'managed'
                """,
                (
                    NOW.isoformat(),
                    NOW.isoformat(),
                    noise_key,
                    NOW.isoformat(),
                    f'{{"artifact_sha256":"{ARTIFACT_SHA256}",'
                    '"storage_profile":"fly_sprites_posix"}',
                    tenant.user_id,
                ),
            )
            database.commit()

    provider = FakeProvider()
    installer = FakeInstaller(register_runner)

    async def fetch(
        client: httpx.AsyncClient,
        artifact_url: str,
        artifact_sha256: str,
    ) -> bytes:
        assert artifact_url == "https://artifacts.example/runner"
        assert artifact_sha256 == ARTIFACT_SHA256
        return ARTIFACT

    monkeypatch.setattr(
        "yinshi.services.managed_runtime_manager.fetch_pinned_artifact",
        fetch,
    )
    manager = _manager(provider, installer)

    status = await manager.provision(tenant.user_id)
    await manager.aclose()

    assert status.lifecycle_status == "ready"
    assert status.last_error is None
    assert [call[0] for call in provider.calls] == ["get", "create", "policy"]
    assert provider.sprite is not None
    assert installer.calls == [
        {
            "sprite_name": status.sprite_name,
            "artifact": ARTIFACT,
            "environment": installer.calls[0]["environment"],
            "artifact_version": "runner-v1",
            "artifact_sha256": ARTIFACT_SHA256,
        }
    ]
    environment = installer.calls[0]["environment"]
    assert isinstance(environment, dict)
    assert environment["YINSHI_REGISTRATION_TOKEN"]


async def test_missing_artifact_attestation_fails_first_provision(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A managed guest without an artifact digest cannot become ready."""
    tenant = getattr(auth_client, "yinshi_tenant")
    current = NOW
    noise_key = base64.urlsafe_b64encode(b"m" * 32).rstrip(b"=").decode("ascii")

    def register_runner_without_digest() -> None:
        with get_control_db() as database:
            database.execute(
                """
                UPDATE user_runners
                SET status = 'online', registered_at = ?, last_heartbeat_at = ?,
                    noise_public_key = ?, noise_public_key_confirmed_at = ?
                WHERE user_id = ? AND kind = 'managed'
                """,
                (
                    current.isoformat(),
                    current.isoformat(),
                    noise_key,
                    current.isoformat(),
                    tenant.user_id,
                ),
            )
            database.commit()

    def clock() -> datetime:
        return current

    async def sleep(seconds: float) -> None:
        nonlocal current
        current += timedelta(seconds=seconds)

    async def fetch(*args: object, **kwargs: object) -> bytes:
        return ARTIFACT

    monkeypatch.setattr(
        "yinshi.services.managed_runtime_manager.fetch_pinned_artifact",
        fetch,
    )
    manager = _manager(
        FakeProvider(),
        FakeInstaller(register_runner_without_digest),
        clock=clock,
        sleep=sleep,
    )

    status = await manager.provision(tenant.user_id)
    await manager.aclose()

    assert status.lifecycle_status == "failed"
    assert status.last_error == "bootstrap_failed"
    assert ARTIFACT_SHA256 not in repr(status)


async def test_mismatched_artifact_attestation_fails_explicit_upgrade(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upgraded guest reporting an old artifact digest cannot become ready."""
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    old_digest = "0" * 64
    initial = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v0",
        region="ord",
        control_url="https://control.example",
        now=NOW - timedelta(minutes=1),
    )
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'ready' WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.commit()

    noise_key = base64.urlsafe_b64encode(b"u" * 32).rstrip(b"=").decode("ascii")

    def register_runner_with_old_digest() -> None:
        with get_control_db() as database:
            database.execute(
                """
                UPDATE user_runners
                SET status = 'online', registered_at = ?, last_heartbeat_at = ?,
                    noise_public_key = ?, noise_public_key_confirmed_at = ?,
                    capabilities_json = ?
                WHERE user_id = ? AND kind = 'managed'
                """,
                (
                    NOW.isoformat(),
                    NOW.isoformat(),
                    noise_key,
                    NOW.isoformat(),
                    '{"artifact_sha256":"'
                    + old_digest
                    + '","storage_profile":"fly_sprites_posix"}',
                    tenant.user_id,
                ),
            )
            database.commit()

    async def fetch(*args: object, **kwargs: object) -> bytes:
        return ARTIFACT

    monkeypatch.setattr(
        "yinshi.services.managed_runtime_manager.fetch_pinned_artifact",
        fetch,
    )
    manager = _manager(FakeProvider(), FakeInstaller(register_runner_with_old_digest))

    status = await manager.provision(tenant.user_id, allow_upgrade=True)
    await manager.aclose()

    assert initial.runtime.generation == 1
    assert status.generation == 2
    assert status.lifecycle_status == "failed"
    assert status.last_error == "bootstrap_failed"
    assert old_digest not in repr(status)
    assert ARTIFACT_SHA256 not in repr(status)


async def test_request_cancellation_does_not_cancel_owned_provisioning(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request cancellation leaves manager-owned completion running."""
    from yinshi.services.managed_runners import get_managed_runtime_status

    tenant = getattr(auth_client, "yinshi_tenant")
    noise_key = base64.urlsafe_b64encode(b"c" * 32).rstrip(b"=").decode("ascii")
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()
    installed = asyncio.Event()

    async def fetch(*args: object, **kwargs: object) -> bytes:
        fetch_started.set()
        await release_fetch.wait()
        return ARTIFACT

    def register_runner() -> None:
        with get_control_db() as database:
            database.execute(
                """
                UPDATE user_runners
                SET status = 'online', registered_at = ?, last_heartbeat_at = ?,
                    noise_public_key = ?, noise_public_key_confirmed_at = ?,
                    capabilities_json = ?
                WHERE user_id = ? AND kind = 'managed'
                """,
                (
                    NOW.isoformat(),
                    NOW.isoformat(),
                    noise_key,
                    NOW.isoformat(),
                    f'{{"artifact_sha256":"{ARTIFACT_SHA256}",'
                    '"storage_profile":"fly_sprites_posix"}',
                    tenant.user_id,
                ),
            )
            database.commit()
        installed.set()

    monkeypatch.setattr(
        "yinshi.services.managed_runtime_manager.fetch_pinned_artifact",
        fetch,
    )
    manager = _manager(FakeProvider(), FakeInstaller(register_runner))
    request = asyncio.create_task(manager.provision(tenant.user_id))
    await fetch_started.wait()

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    release_fetch.set()
    await asyncio.wait_for(installed.wait(), timeout=1.0)
    for _ in range(100):
        status = get_managed_runtime_status(tenant.user_id)
        if status is not None and status.lifecycle_status == "ready":
            break
        await asyncio.sleep(0.01)

    await manager.aclose()
    assert status is not None
    assert status.lifecycle_status == "ready"


async def test_aclose_cancels_owned_work_and_fails_matching_generation(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manager shutdown drains owned work and records a fixed failure code."""
    from yinshi.services.managed_runners import get_managed_runtime_status

    tenant = getattr(auth_client, "yinshi_tenant")
    fetch_started = asyncio.Event()

    async def fetch(*args: object, **kwargs: object) -> bytes:
        fetch_started.set()
        await asyncio.Event().wait()
        return ARTIFACT

    monkeypatch.setattr(
        "yinshi.services.managed_runtime_manager.fetch_pinned_artifact",
        fetch,
    )
    manager = _manager(FakeProvider(), FakeInstaller())
    request = asyncio.create_task(manager.provision(tenant.user_id))
    await fetch_started.wait()

    reconciled = await manager.reconcile_startup()
    active_status = get_managed_runtime_status(tenant.user_id)

    assert reconciled == 0
    assert active_status is not None
    assert active_status.lifecycle_status == "provisioning"

    await manager.aclose()
    with pytest.raises(asyncio.CancelledError):
        await request

    status = get_managed_runtime_status(tenant.user_id)
    assert status is not None
    assert status.lifecycle_status == "failed"
    assert status.last_error == "provider_unavailable"


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        ("artifact", "artifact_invalid"),
        ("provider", "provider_unavailable"),
        ("policy", "network_policy_failed"),
        ("guest", "bootstrap_failed"),
    ],
)
async def test_external_failures_store_only_allowlisted_code(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    error_code: str,
) -> None:
    """External failures become a short persisted code without raw details."""
    tenant = getattr(auth_client, "yinshi_tenant")
    provider = FakeProvider()
    installer = FakeInstaller()
    if failure == "provider":
        provider.fail_operation = "get"
    elif failure == "policy":
        provider.sprite = SpriteRecord(id="sprite-id", name="placeholder", status="running")
    elif failure == "guest":
        installer.error = RuntimeError("guest secret response")

    async def fetch(*args: object, **kwargs: object) -> bytes:
        if failure == "artifact":
            raise RuntimeError("artifact URL secret response")
        return ARTIFACT

    monkeypatch.setattr(
        "yinshi.services.managed_runtime_manager.fetch_pinned_artifact",
        fetch,
    )
    manager = _manager(provider, installer)
    if failure == "policy":
        original_get = provider.get_sprite

        async def matching_get(name: str) -> SpriteRecord | None:
            sprite = await original_get(name)
            assert sprite is not None
            return SpriteRecord(id=sprite.id, name=name, status=sprite.status)

        provider.get_sprite = matching_get  # type: ignore[method-assign]
        provider.fail_operation = "policy"

    status = await manager.provision(tenant.user_id)
    await manager.aclose()

    assert status.lifecycle_status == "failed"
    assert status.last_error == error_code
    assert "secret" not in repr(status)


async def test_readiness_timeout_uses_injected_clock_and_sleep(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that never registers fails at the bounded readiness deadline."""
    tenant = getattr(auth_client, "yinshi_tenant")
    current = NOW
    sleeps: list[float] = []

    def clock() -> datetime:
        return current

    async def sleep(seconds: float) -> None:
        nonlocal current
        sleeps.append(seconds)
        current += timedelta(seconds=seconds)

    async def fetch(*args: object, **kwargs: object) -> bytes:
        return ARTIFACT

    monkeypatch.setattr(
        "yinshi.services.managed_runtime_manager.fetch_pinned_artifact",
        fetch,
    )
    manager = _manager(FakeProvider(), FakeInstaller(), clock=clock, sleep=sleep)

    status = await manager.provision(tenant.user_id)
    await manager.aclose()

    assert status.lifecycle_status == "failed"
    assert status.last_error == "wake_timeout"
    assert sleeps == [1.0] * 10


async def test_changed_confirmed_noise_identity_fails_current_generation(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replaced identity cannot make a retried managed runtime ready."""
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    old_key = base64.urlsafe_b64encode(b"o" * 32).rstrip(b"=").decode("ascii")
    new_key = base64.urlsafe_b64encode(b"p" * 32).rstrip(b"=").decode("ascii")
    first = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=NOW - timedelta(minutes=20),
    )
    with get_control_db() as database:
        database.execute(
            """
            UPDATE user_runners
            SET noise_public_key = ?, noise_public_key_confirmed_at = ?
            WHERE id = ?
            """,
            (old_key, NOW.isoformat(), first.runtime.runner_id),
        )
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'failed' WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.commit()

    def replace_identity() -> None:
        with get_control_db() as database:
            database.execute(
                """
                UPDATE user_runners
                SET status = 'online', registered_at = ?, last_heartbeat_at = ?,
                    noise_public_key = ?, noise_public_key_confirmed_at = ?
                WHERE user_id = ? AND kind = 'managed'
                """,
                (NOW.isoformat(), NOW.isoformat(), new_key, NOW.isoformat(), tenant.user_id),
            )
            database.commit()

    async def fetch(*args: object, **kwargs: object) -> bytes:
        return ARTIFACT

    monkeypatch.setattr(
        "yinshi.services.managed_runtime_manager.fetch_pinned_artifact",
        fetch,
    )
    manager = _manager(FakeProvider(), FakeInstaller(replace_identity))

    status = await manager.provision(tenant.user_id)
    await manager.aclose()

    assert status.lifecycle_status == "failed"
    assert status.last_error == "runner_identity_changed"


async def test_stale_generation_stops_before_provider_call(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost heartbeat cancels stale artifact work before provider calls."""
    from yinshi.services.managed_runtime_manager import ManagedRuntimeStateError

    tenant = getattr(auth_client, "yinshi_tenant")
    provider = FakeProvider()
    provider.fail_operation = "get"
    fetch_started = asyncio.Event()
    fetch_canceled = asyncio.Event()

    async def fetch(*args: object, **kwargs: object) -> bytes:
        fetch_started.set()
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            fetch_canceled.set()
            raise
        return ARTIFACT

    async def heartbeat_sleep(seconds: float) -> None:
        await fetch_started.wait()
        with get_control_db() as database:
            database.execute(
                "UPDATE managed_runtimes SET generation = generation + 1 WHERE user_id = ?",
                (tenant.user_id,),
            )
            database.commit()

    monkeypatch.setattr(
        "yinshi.services.managed_runtime_manager.fetch_pinned_artifact",
        fetch,
    )
    manager = _manager(
        provider,
        FakeInstaller(),
        heartbeat_sleep=heartbeat_sleep,
    )

    with pytest.raises(
        ManagedRuntimeStateError,
        match="^Managed runtime state is invalid$",
    ):
        await manager.provision(tenant.user_id)
    await manager.aclose()

    assert fetch_canceled.is_set()
    assert provider.calls == []


async def test_provider_wait_heartbeat_prevents_overlapping_stale_claim(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider waits keep their current provisioning generation fresh."""
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    current = NOW
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()
    heartbeat_refreshed = asyncio.Event()
    heartbeat_calls = 0

    class BlockingProvider(FakeProvider):
        async def get_sprite(self, name: str) -> SpriteRecord | None:
            provider_started.set()
            await release_provider.wait()
            return await super().get_sprite(name)

    def clock() -> datetime:
        return current

    async def heartbeat_sleep(seconds: float) -> None:
        nonlocal current, heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 1:
            await provider_started.wait()
            current += timedelta(minutes=9)
            return
        heartbeat_refreshed.set()
        await asyncio.Event().wait()

    async def fetch(*args: object, **kwargs: object) -> bytes:
        return ARTIFACT

    monkeypatch.setattr(
        "yinshi.services.managed_runtime_manager.fetch_pinned_artifact",
        fetch,
    )
    manager = _manager(
        BlockingProvider(),
        FakeInstaller(),
        clock=clock,
        heartbeat_sleep=heartbeat_sleep,
    )
    request = asyncio.create_task(manager.provision(tenant.user_id))
    await provider_started.wait()
    await asyncio.wait_for(heartbeat_refreshed.wait(), timeout=1.0)

    observer = claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=current + timedelta(minutes=2),
    )

    assert observer.claimed is False
    assert observer.runtime.generation == 1
    await manager.aclose()
    with pytest.raises(asyncio.CancelledError):
        await request


async def test_concurrent_ensure_online_calls_share_one_restart(
    auth_client,
) -> None:
    """Concurrent waiters must share one manager-owned wake operation."""
    tenant = getattr(auth_client, "yinshi_tenant")
    wake_now = datetime.now(timezone.utc)
    runner_id, _, noise_key = _store_ready_runtime(tenant.user_id, wake_now)
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()
    connected = False

    class BlockingProvider(FakeProvider):
        async def restart_service(
            self,
            name: str,
            *,
            service_name: str,
            monitor_duration: float | None,
        ) -> None:
            await super().restart_service(
                name,
                service_name=service_name,
                monitor_duration=monitor_duration,
            )
            provider_started.set()
            await release_provider.wait()

    def is_runner_connected(candidate_runner_id: str) -> bool:
        assert candidate_runner_id == runner_id
        return connected

    provider = BlockingProvider()
    manager = _manager(
        provider,
        FakeInstaller(),
        is_runner_connected=is_runner_connected,
        clock=lambda: wake_now,
    )

    first = asyncio.create_task(manager.ensure_online(tenant.user_id))
    await provider_started.wait()
    second = asyncio.create_task(manager.ensure_online(tenant.user_id))
    await asyncio.sleep(0)
    connected = True
    release_provider.set()

    first_runner, second_runner = await asyncio.gather(first, second)
    await manager.aclose()

    assert first_runner.runner_id == runner_id
    assert first_runner.runner_public_key == noise_key
    assert second_runner == first_runner
    assert [call[0] for call in provider.calls].count("restart") == 1


async def test_cancelled_ensure_online_waiter_does_not_cancel_shared_wake(
    auth_client,
) -> None:
    """Cancelling one waiter must preserve wake work for another waiter."""
    tenant = getattr(auth_client, "yinshi_tenant")
    wake_now = datetime.now(timezone.utc)
    runner_id, _, _ = _store_ready_runtime(tenant.user_id, wake_now)
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()
    connected = False

    class BlockingProvider(FakeProvider):
        async def restart_service(
            self,
            name: str,
            *,
            service_name: str,
            monitor_duration: float | None,
        ) -> None:
            await super().restart_service(
                name,
                service_name=service_name,
                monitor_duration=monitor_duration,
            )
            provider_started.set()
            await release_provider.wait()

    provider = BlockingProvider()
    manager = _manager(
        provider,
        FakeInstaller(),
        is_runner_connected=lambda candidate_runner_id: (
            candidate_runner_id == runner_id and connected
        ),
        clock=lambda: wake_now,
    )

    cancelled_waiter = asyncio.create_task(manager.ensure_online(tenant.user_id))
    await provider_started.wait()
    surviving_waiter = asyncio.create_task(manager.ensure_online(tenant.user_id))
    await asyncio.sleep(0)
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    connected = True
    release_provider.set()

    result = await surviving_waiter
    await manager.aclose()

    assert result.runner_id == runner_id
    assert [call[0] for call in provider.calls].count("restart") == 1


async def test_provision_rejects_running_managed_backup(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provisioning should fail while durable managed maintenance is active."""
    from yinshi.services.managed_runtime_manager import ManagedRuntimeStateError

    tenant = getattr(auth_client, "yinshi_tenant")
    manager = _manager(FakeProvider(), FakeInstaller())
    monkeypatch.setattr(
        "yinshi.services.managed_runtime_manager.managed_backup_operation_is_running",
        lambda user_id: user_id == tenant.user_id,
    )

    with pytest.raises(ManagedRuntimeStateError, match="maintenance"):
        await manager.provision(tenant.user_id)
    await manager.aclose()


@pytest.mark.asyncio
async def test_ensure_online_rejects_running_managed_backup(
    auth_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capability wake should fail while durable managed maintenance is active."""
    from yinshi.services.managed_runtime_manager import (
        ManagedRuntimeManager,
        ManagedRuntimeStateError,
    )

    tenant = getattr(auth_client, "yinshi_tenant")
    manager = object.__new__(ManagedRuntimeManager)
    manager._online_lock = asyncio.Lock()
    manager._online_tasks = {}
    manager._closing = False
    monkeypatch.setattr(
        "yinshi.services.managed_runtime_manager.managed_backup_operation_is_running",
        lambda user_id: user_id == tenant.user_id,
        raising=False,
    )

    with pytest.raises(ManagedRuntimeStateError, match="maintenance"):
        await manager.ensure_online(tenant.user_id)


@pytest.mark.asyncio
async def test_ensure_online_returns_connected_runtime_without_restart(
    auth_client,
) -> None:
    """A connected managed runtime must not disrupt active relay transfers."""
    tenant = getattr(auth_client, "yinshi_tenant")
    wake_now = datetime.now(timezone.utc)
    runner_id, sprite_name, noise_key = _store_ready_runtime(
        tenant.user_id,
        wake_now,
    )
    provider = FakeProvider()
    connected_runner_ids: list[str] = []

    def is_runner_connected(runner_id: str) -> bool:
        connected_runner_ids.append(runner_id)
        return True

    manager = _manager(
        provider,
        FakeInstaller(),
        is_runner_connected=is_runner_connected,
        clock=lambda: wake_now,
    )

    runner = await manager.ensure_online(tenant.user_id)
    await manager.aclose()

    assert runner.runner_id == runner_id
    assert runner.runner_public_key == noise_key
    assert sprite_name
    assert provider.calls == []
    assert connected_runner_ids == [runner_id]


async def test_ensure_online_maps_provider_failure_without_changing_ready_runtime(
    auth_client,
) -> None:
    """A restart failure exposes only the fixed local provider error."""
    from yinshi.services.managed_runners import get_managed_runtime_status
    from yinshi.services.managed_runtime_manager import ManagedRuntimeProviderError

    tenant = getattr(auth_client, "yinshi_tenant")
    wake_now = datetime.now(timezone.utc)
    _store_ready_runtime(tenant.user_id, wake_now)
    provider = FakeProvider()
    provider.fail_operation = "restart"
    manager = _manager(
        provider,
        FakeInstaller(),
        is_runner_connected=lambda runner_id: False,
        clock=lambda: wake_now,
    )

    with pytest.raises(
        ManagedRuntimeProviderError,
        match="^Managed runtime provider unavailable$",
    ) as error:
        await manager.ensure_online(tenant.user_id)
    await manager.aclose()

    assert "secret" not in repr(error.value)
    runtime = get_managed_runtime_status(tenant.user_id)
    assert runtime is not None
    assert runtime.lifecycle_status == "ready"
    assert runtime.last_error is None


async def test_ensure_online_times_out_while_relay_connection_is_absent(
    auth_client,
) -> None:
    """A current heartbeat alone cannot satisfy the bounded wake deadline."""
    from yinshi.services.managed_runners import get_managed_runtime_status
    from yinshi.services.managed_runtime_manager import ManagedRuntimeTimeoutError

    tenant = getattr(auth_client, "yinshi_tenant")
    current = datetime.now(timezone.utc)
    _store_ready_runtime(tenant.user_id, current)
    sleeps: list[float] = []

    def clock() -> datetime:
        return current

    async def sleep(seconds: float) -> None:
        nonlocal current
        sleeps.append(seconds)
        current += timedelta(seconds=seconds)

    manager = _manager(
        FakeProvider(),
        FakeInstaller(),
        is_runner_connected=lambda runner_id: False,
        clock=clock,
        sleep=sleep,
    )

    with pytest.raises(
        ManagedRuntimeTimeoutError,
        match="^Managed runtime wake timed out$",
    ):
        await manager.ensure_online(tenant.user_id)
    await manager.aclose()

    assert sleeps == [1.0] * 10
    runtime = get_managed_runtime_status(tenant.user_id)
    assert runtime is not None
    assert runtime.lifecycle_status == "ready"
    assert runtime.last_error is None


async def test_ensure_online_rejects_changed_identity_immediately(
    auth_client,
) -> None:
    """A changed confirmed identity fails before sleeping or checking connection."""
    from yinshi.services.managed_runners import get_managed_runtime_status
    from yinshi.services.managed_runtime_manager import ManagedRuntimeIdentityError

    tenant = getattr(auth_client, "yinshi_tenant")
    wake_now = datetime.now(timezone.utc)
    runner_id, _, _ = _store_ready_runtime(tenant.user_id, wake_now)
    changed_key = base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode("ascii")

    class ChangingProvider(FakeProvider):
        async def restart_service(
            self,
            name: str,
            *,
            service_name: str,
            monitor_duration: float | None,
        ) -> None:
            await super().restart_service(
                name,
                service_name=service_name,
                monitor_duration=monitor_duration,
            )
            with get_control_db() as database:
                database.execute(
                    """
                    UPDATE user_runners
                    SET noise_public_key = ?, noise_public_key_confirmed_at = ?
                    WHERE id = ?
                    """,
                    (changed_key, wake_now.isoformat(), runner_id),
                )
                database.commit()

    async def fail_sleep(seconds: float) -> None:
        raise AssertionError("identity change must fail before sleep")

    manager = _manager(
        ChangingProvider(),
        FakeInstaller(),
        is_runner_connected=lambda runner_id: False,
        clock=lambda: wake_now,
        sleep=fail_sleep,
    )

    with pytest.raises(
        ManagedRuntimeIdentityError,
        match="^Managed runtime identity changed$",
    ):
        await manager.ensure_online(tenant.user_id)
    await manager.aclose()

    runtime = get_managed_runtime_status(tenant.user_id)
    assert runtime is not None
    assert runtime.lifecycle_status == "ready"
    assert runtime.last_error is None


async def test_ensure_online_rejects_byoc_observer_without_mutation(
    auth_client,
) -> None:
    """A BYOC runner cannot authorize a managed wake and remains unchanged."""
    from yinshi.services.managed_runtime_manager import ManagedRuntimeStateError
    from yinshi.services.runners import create_runner_registration, get_runner_for_user

    tenant = getattr(auth_client, "yinshi_tenant")
    create_runner_registration(
        tenant.user_id,
        name="Private AWS runner",
        cloud_provider="aws",
        region="us-east-1",
        storage_profile="aws_ebs_s3_files",
        control_url="https://control.example",
    )
    before = get_runner_for_user(tenant.user_id)
    provider = FakeProvider()
    manager = _manager(provider, FakeInstaller())

    with pytest.raises(
        ManagedRuntimeStateError,
        match="^Managed runtime state is invalid$",
    ):
        await manager.ensure_online(tenant.user_id)
    await manager.aclose()

    assert get_runner_for_user(tenant.user_id) == before
    assert provider.calls == []


async def test_ensure_online_requires_stored_confirmed_identity(
    auth_client,
) -> None:
    """An unconfirmed managed identity cannot authorize a provider restart."""
    from yinshi.services.managed_runtime_manager import ManagedRuntimeStateError

    tenant = getattr(auth_client, "yinshi_tenant")
    wake_now = datetime.now(timezone.utc)
    runner_id, _, _ = _store_ready_runtime(tenant.user_id, wake_now)
    with get_control_db() as database:
        database.execute(
            """
            UPDATE user_runners
            SET noise_public_key_confirmed_at = NULL
            WHERE id = ?
            """,
            (runner_id,),
        )
        database.commit()
    provider = FakeProvider()
    manager = _manager(provider, FakeInstaller(), clock=lambda: wake_now)

    with pytest.raises(
        ManagedRuntimeStateError,
        match="^Managed runtime state is invalid$",
    ):
        await manager.ensure_online(tenant.user_id)
    await manager.aclose()

    assert provider.calls == []
