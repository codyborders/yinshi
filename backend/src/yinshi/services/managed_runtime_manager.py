"""Coordinate managed Fly Sprite provisioning."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, TypeVar

import httpx

from yinshi.services.managed_artifacts import fetch_pinned_artifact
from yinshi.services.managed_runners import (
    ManagedRuntimeStatus,
    ProvisioningClaimResult,
    claim_managed_runtime_provisioning,
    get_managed_runtime_status,
    mark_managed_runtime_failed,
    mark_managed_runtime_ready,
    reconcile_managed_runtime_provisioning,
    refresh_managed_runtime_provisioning,
)
from yinshi.services.runners import (
    _HEARTBEAT_ONLINE_WINDOW_SECONDS,
    _datetime_from_storage,
    get_managed_runner_for_user,
)
from yinshi.services.sprites import SpriteRecord


class ManagedRuntimeWakeError(RuntimeError):
    """Base error for safe managed runtime wake failures."""


class ManagedRuntimeProviderError(ManagedRuntimeWakeError):
    """The managed runtime provider could not restart the service."""


class ManagedRuntimeTimeoutError(ManagedRuntimeWakeError):
    """The managed runner did not become reachable before the deadline."""


class ManagedRuntimeStateError(ManagedRuntimeWakeError):
    """Persisted managed runtime state cannot authorize a wake."""


class ManagedRuntimeIdentityError(ManagedRuntimeWakeError):
    """The managed runner identity changed during a wake."""


_PROVIDER_ERROR_MESSAGE = "Managed runtime provider unavailable"
_TIMEOUT_ERROR_MESSAGE = "Managed runtime wake timed out"
_STATE_ERROR_MESSAGE = "Managed runtime state is invalid"
_IDENTITY_ERROR_MESSAGE = "Managed runtime identity changed"
_PROVISIONING_HEARTBEAT_INTERVAL_SECONDS = 30.0

_Result = TypeVar("_Result")


class ManagedRuntimeProvider(Protocol):
    """Provider operations needed during provisioning and wake."""

    async def get_sprite(self, name: str) -> SpriteRecord | None: ...

    async def create_sprite(self, name: str) -> SpriteRecord: ...

    async def set_network_policy(
        self,
        name: str,
        *,
        allowed_domains: tuple[str, ...],
    ) -> None: ...

    async def restart_service(
        self,
        name: str,
        *,
        service_name: str,
        monitor_duration: float | None,
    ) -> None: ...


class ManagedGuestInstaller(Protocol):
    """Install verified managed runner content inside a Sprite."""

    async def install(
        self,
        *,
        sprite_name: str,
        artifact: bytes,
        environment: dict[str, str],
        artifact_version: str,
        artifact_sha256: str,
    ) -> None: ...


class ManagedRuntimeManager:
    """Coordinate managed runtime provisioning."""

    def __init__(
        self,
        *,
        provider: ManagedRuntimeProvider,
        guest_installer: ManagedGuestInstaller,
        http_client: httpx.AsyncClient,
        name_prefix: str,
        name_key: str,
        artifact_url: str,
        artifact_sha256: str,
        artifact_version: str,
        allowed_domains: tuple[str, ...],
        region: str,
        control_url: str,
        readiness_timeout_seconds: float,
        is_runner_connected: Callable[[str], bool],
        poll_interval_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        heartbeat_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._provider = provider
        self._guest_installer = guest_installer
        self._http_client = http_client
        self._name_prefix = name_prefix
        self._name_key = name_key
        self._artifact_url = artifact_url
        self._artifact_sha256 = artifact_sha256
        self._artifact_version = artifact_version
        self._allowed_domains = allowed_domains
        self._region = region
        self._control_url = control_url
        if readiness_timeout_seconds <= 0:
            raise ValueError("readiness_timeout_seconds must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._readiness_timeout = timedelta(seconds=readiness_timeout_seconds)
        self._poll_interval_seconds = poll_interval_seconds
        self._is_runner_connected = is_runner_connected
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._heartbeat_sleep = heartbeat_sleep
        self._provisioning_tasks: dict[asyncio.Task[ManagedRuntimeStatus], tuple[str, int]] = {}

    async def reconcile_startup(self) -> int:
        """Fail abandoned provisioning before this manager serves requests."""
        active_owners = set(self._provisioning_tasks.values())
        return reconcile_managed_runtime_provisioning(active_owners, self._now())

    async def aclose(self) -> None:
        """Cancel owned provisioning work before closing the artifact client."""
        tracked = list(self._provisioning_tasks.items())
        for task, _ in tracked:
            task.cancel()
        if tracked:
            await asyncio.gather(*(task for task, _ in tracked), return_exceptions=True)
        for task, (user_id, generation) in tracked:
            if task.cancelled():
                try:
                    mark_managed_runtime_failed(
                        user_id,
                        generation,
                        "provider_unavailable",
                        self._now(),
                    )
                except Exception:
                    pass
        await self._http_client.aclose()

    async def ensure_online(self, user_id: str) -> dict[str, Any]:
        """Wake one ready managed runtime and return its connected runner."""
        try:
            runtime = get_managed_runtime_status(user_id)
            runner = get_managed_runner_for_user(user_id)
        except Exception:
            raise ManagedRuntimeStateError(_STATE_ERROR_MESSAGE) from None
        if (
            runtime is None
            or runtime.lifecycle_status != "ready"
            or runtime.provider_name != "fly_sprites"
            or runner is None
            or runner.get("id") != runtime.runner_id
            or runner.get("kind") != "managed"
            or runner.get("cloud_provider") != runtime.provider_name
        ):
            raise ManagedRuntimeStateError(_STATE_ERROR_MESSAGE)
        expected_noise_key = runner.get("noise_public_key")
        if not isinstance(expected_noise_key, str) or not runner.get("noise_key_confirmed"):
            raise ManagedRuntimeStateError(_STATE_ERROR_MESSAGE)
        now = self._now()
        try:
            heartbeat_at = _datetime_from_storage(runner.get("last_heartbeat_at"))
            connected = self._is_runner_connected(runtime.runner_id)
        except Exception:
            raise ManagedRuntimeStateError(_STATE_ERROR_MESSAGE) from None
        heartbeat_age = None if heartbeat_at is None else (now - heartbeat_at).total_seconds()
        if (
            runner.get("status") == "online"
            and heartbeat_age is not None
            and 0 <= heartbeat_age <= _HEARTBEAT_ONLINE_WINDOW_SECONDS
            and connected
        ):
            return runner

        try:
            await self._provider.restart_service(
                runtime.sprite_name,
                service_name="yinshi-runner",
                monitor_duration=None,
            )
        except Exception:
            raise ManagedRuntimeProviderError(_PROVIDER_ERROR_MESSAGE) from None

        deadline = self._now() + self._readiness_timeout
        while True:
            now = self._now()
            try:
                current_runtime = get_managed_runtime_status(user_id)
                runner = get_managed_runner_for_user(user_id)
            except Exception:
                raise ManagedRuntimeStateError(_STATE_ERROR_MESSAGE) from None
            if (
                current_runtime is None
                or current_runtime.lifecycle_status != "ready"
                or current_runtime.provider_name != "fly_sprites"
                or current_runtime.runner_id != runtime.runner_id
                or current_runtime.sprite_name != runtime.sprite_name
                or runner is None
                or runner.get("id") != runtime.runner_id
                or runner.get("kind") != "managed"
                or runner.get("cloud_provider") != runtime.provider_name
            ):
                raise ManagedRuntimeStateError(_STATE_ERROR_MESSAGE)
            if runner.get("noise_public_key") != expected_noise_key or not runner.get(
                "noise_key_confirmed"
            ):
                raise ManagedRuntimeIdentityError(_IDENTITY_ERROR_MESSAGE)
            try:
                heartbeat_at = _datetime_from_storage(runner.get("last_heartbeat_at"))
            except Exception:
                raise ManagedRuntimeStateError(_STATE_ERROR_MESSAGE) from None
            heartbeat_age = None if heartbeat_at is None else (now - heartbeat_at).total_seconds()
            heartbeat_is_current = heartbeat_age is not None and (
                0 <= heartbeat_age <= _HEARTBEAT_ONLINE_WINDOW_SECONDS
            )
            try:
                connected = self._is_runner_connected(runtime.runner_id)
            except Exception:
                raise ManagedRuntimeStateError(_STATE_ERROR_MESSAGE) from None
            if runner.get("status") == "online" and heartbeat_is_current and connected:
                return runner
            if now >= deadline:
                raise ManagedRuntimeTimeoutError(_TIMEOUT_ERROR_MESSAGE)
            await self._sleep(self._poll_interval_seconds)

    async def provision(
        self,
        user_id: str,
        allow_upgrade: bool = False,
    ) -> ManagedRuntimeStatus:
        """Return observer state without external calls when another caller owns claim."""
        claim = claim_managed_runtime_provisioning(
            user_id,
            name_prefix=self._name_prefix,
            name_key=self._name_key,
            artifact_version=self._artifact_version,
            region=self._region,
            control_url=self._control_url,
            allow_upgrade=allow_upgrade,
            now=self._now(),
        )
        if not claim.claimed:
            return claim.runtime

        generation = claim.runtime.generation
        task = asyncio.create_task(self._complete_provision(user_id, claim))
        self._provisioning_tasks[task] = (user_id, generation)
        task.add_done_callback(self._provisioning_task_finished)
        return await asyncio.shield(task)

    async def _complete_provision(
        self,
        user_id: str,
        claim: ProvisioningClaimResult,
    ) -> ManagedRuntimeStatus:
        """Complete manager-owned provisioning after a successful claim."""
        generation = claim.runtime.generation
        ownership_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._maintain_provisioning_heartbeat(
                user_id,
                generation,
                ownership_lost,
            )
        )
        try:
            return await self._run_provisioning(user_id, claim, ownership_lost)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _run_provisioning(
        self,
        user_id: str,
        claim: ProvisioningClaimResult,
        ownership_lost: asyncio.Event,
    ) -> ManagedRuntimeStatus:
        """Run external provisioning work for one current owner."""
        generation = claim.runtime.generation
        if claim.runtime.provider_name != "fly_sprites":
            return self._fail(user_id, generation, "provider_unavailable", claim.runtime)
        if claim.environment is None:
            return self._fail(
                user_id,
                generation,
                "runner_registration_failed",
                claim.runtime,
            )
        try:
            runner = get_managed_runner_for_user(user_id)
        except Exception:
            return self._fail(
                user_id,
                generation,
                "runner_registration_failed",
                claim.runtime,
            )
        if (
            runner is None
            or runner.get("id") != claim.runtime.runner_id
            or runner.get("kind") != "managed"
            or runner.get("cloud_provider") != "fly_sprites"
        ):
            return self._fail(
                user_id,
                generation,
                "runner_registration_failed",
                claim.runtime,
            )
        expected_noise_key = (
            runner.get("noise_public_key") if runner.get("noise_key_confirmed") else None
        )
        try:
            artifact = await self._await_owned(
                fetch_pinned_artifact(
                    self._http_client,
                    self._artifact_url,
                    self._artifact_sha256,
                ),
                user_id,
                generation,
                ownership_lost,
            )
        except ManagedRuntimeStateError:
            raise
        except Exception:
            return self._fail(user_id, generation, "artifact_invalid", claim.runtime)

        try:
            sprite = await self._await_owned(
                self._provider.get_sprite(claim.runtime.sprite_name),
                user_id,
                generation,
                ownership_lost,
            )
            if sprite is None:
                sprite = await self._await_owned(
                    self._provider.create_sprite(claim.runtime.sprite_name),
                    user_id,
                    generation,
                    ownership_lost,
                )
            if sprite.name != claim.runtime.sprite_name:
                raise ValueError("inconsistent Sprite name")
        except ManagedRuntimeStateError:
            raise
        except Exception:
            return self._fail(user_id, generation, "provider_unavailable", claim.runtime)

        try:
            await self._await_owned(
                self._provider.set_network_policy(
                    claim.runtime.sprite_name,
                    allowed_domains=self._allowed_domains,
                ),
                user_id,
                generation,
                ownership_lost,
            )
        except ManagedRuntimeStateError:
            raise
        except Exception:
            return self._fail(
                user_id,
                generation,
                "network_policy_failed",
                claim.runtime,
            )

        try:
            await self._await_owned(
                self._guest_installer.install(
                    sprite_name=claim.runtime.sprite_name,
                    artifact=artifact,
                    environment=dict(claim.environment),
                    artifact_version=self._artifact_version,
                    artifact_sha256=self._artifact_sha256,
                ),
                user_id,
                generation,
                ownership_lost,
            )
        except ManagedRuntimeStateError:
            raise
        except Exception:
            return self._fail(user_id, generation, "bootstrap_failed", claim.runtime)

        deadline = self._now() + self._readiness_timeout
        while True:
            now = self._now()
            try:
                runner = get_managed_runner_for_user(user_id)
            except Exception:
                return self._fail(
                    user_id,
                    generation,
                    "runner_registration_failed",
                    claim.runtime,
                )
            if runner is None or runner.get("id") != claim.runtime.runner_id:
                return self._fail(
                    user_id,
                    generation,
                    "runner_registration_failed",
                    claim.runtime,
                )
            noise_key = runner.get("noise_public_key")
            if expected_noise_key is not None and noise_key != expected_noise_key:
                return self._fail(
                    user_id,
                    generation,
                    "runner_identity_changed",
                    claim.runtime,
                )
            identity_is_confirmed = isinstance(noise_key, str) and bool(
                runner.get("noise_key_confirmed")
            )
            if runner.get("status") == "online" and not identity_is_confirmed:
                return self._fail(
                    user_id,
                    generation,
                    "runner_registration_failed",
                    claim.runtime,
                )
            if runner.get("registered_at") is not None and identity_is_confirmed:
                if not self._artifact_attestation_matches(runner):
                    return self._fail(
                        user_id,
                        generation,
                        "bootstrap_failed",
                        claim.runtime,
                    )
                if mark_managed_runtime_ready(
                    user_id,
                    claim.runtime.generation,
                    now,
                ):
                    status = get_managed_runtime_status(user_id)
                    assert status is not None
                    return status
            self._refresh_ownership(user_id, generation, ownership_lost)
            if now >= deadline:
                return self._fail(user_id, generation, "wake_timeout", claim.runtime)
            await self._await_owned(
                self._sleep(self._poll_interval_seconds),
                user_id,
                generation,
                ownership_lost,
            )

    async def _maintain_provisioning_heartbeat(
        self,
        user_id: str,
        generation: int,
        ownership_lost: asyncio.Event,
    ) -> None:
        """Refresh ownership until completion or generation loss."""
        while True:
            await self._heartbeat_sleep(_PROVISIONING_HEARTBEAT_INTERVAL_SECONDS)
            try:
                refreshed = refresh_managed_runtime_provisioning(
                    user_id,
                    generation,
                    self._now(),
                )
            except Exception:
                refreshed = False
            if not refreshed:
                ownership_lost.set()
                return

    async def _await_owned(
        self,
        awaitable: Awaitable[_Result],
        user_id: str,
        generation: int,
        ownership_lost: asyncio.Event,
    ) -> _Result:
        """Cancel one operation when its provisioning ownership is lost."""
        operation = asyncio.ensure_future(awaitable)
        lost_waiter = asyncio.create_task(ownership_lost.wait())
        try:
            done, _ = await asyncio.wait(
                (operation, lost_waiter),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_waiter in done:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                raise ManagedRuntimeStateError(_STATE_ERROR_MESSAGE)
            lost_waiter.cancel()
            await asyncio.gather(lost_waiter, return_exceptions=True)
            self._refresh_ownership(user_id, generation, ownership_lost)
            return operation.result()
        finally:
            if not operation.done():
                operation.cancel()
            if not lost_waiter.done():
                lost_waiter.cancel()
            await asyncio.gather(operation, lost_waiter, return_exceptions=True)

    def _refresh_ownership(
        self,
        user_id: str,
        generation: int,
        ownership_lost: asyncio.Event,
    ) -> None:
        """Refresh current ownership or raise the fixed state error."""
        try:
            refreshed = refresh_managed_runtime_provisioning(
                user_id,
                generation,
                self._now(),
            )
        except Exception:
            refreshed = False
        if not refreshed:
            ownership_lost.set()
            raise ManagedRuntimeStateError(_STATE_ERROR_MESSAGE)

    def _provisioning_task_finished(
        self,
        task: asyncio.Task[ManagedRuntimeStatus],
    ) -> None:
        """Forget finished work and consume its fixed completion result."""
        self._provisioning_tasks.pop(task, None)
        if not task.cancelled():
            task.exception()

    def _artifact_attestation_matches(self, runner: dict[str, Any]) -> bool:
        """Return whether managed guest reports configured artifact digest."""
        capabilities = runner.get("capabilities")
        if not isinstance(capabilities, dict):
            return False
        artifact_sha256 = capabilities.get("artifact_sha256")
        return isinstance(artifact_sha256, str) and hmac.compare_digest(
            artifact_sha256,
            self._artifact_sha256,
        )

    def _fail(
        self,
        user_id: str,
        generation: int,
        error_code: str,
        fallback: ManagedRuntimeStatus,
    ) -> ManagedRuntimeStatus:
        """Fail only the matching generation and return safe current state."""
        mark_managed_runtime_failed(user_id, generation, error_code, self._now())
        return get_managed_runtime_status(user_id) or fallback

    def _now(self) -> datetime:
        """Return one validated injected UTC clock value."""
        now = self._clock()
        if not isinstance(now, datetime):
            raise TypeError("clock must return a datetime")
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)
