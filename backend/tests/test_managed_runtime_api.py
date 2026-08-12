"""Tests for safe managed runtime HTTP routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from yinshi.rate_limit import limiter
from yinshi.tenant import TenantContext

_RUNNER_PUBLIC_KEY = "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I"
_CLIENT_PUBLIC_KEY = "a8OCKiqn9OaYHWU4aSs83z5t-e6m7SaetB2TwidXt1o"


class FakeManager:
    """Record managed runtime operations and return configured values."""

    def __init__(self) -> None:
        self.provision_result: object | None = None
        self.runner: dict[str, Any] | None = None
        self.error: Exception | None = None
        self.calls: list[tuple[str, str]] = []

    async def provision(self, user_id: str) -> object:
        self.calls.append(("provision", user_id))
        if self.error is not None:
            raise self.error
        assert self.provision_result is not None
        return self.provision_result

    async def ensure_online(self, user_id: str) -> dict[str, Any]:
        self.calls.append(("ensure_online", user_id))
        if self.error is not None:
            raise self.error
        assert self.runner is not None
        return self.runner


@contextmanager
def _runtime_client(
    tenant: TenantContext | None,
    manager: object | None = None,
    *,
    public_launch_enabled: bool | None = None,
) -> Iterator[TestClient]:
    """Build an isolated app that mounts only managed runtime routes."""
    from yinshi.api.managed_runtime import router

    application = FastAPI()
    application.state.limiter = limiter
    if manager is not None:
        application.state.managed_runtime_manager = manager
    if public_launch_enabled is not None:
        application.state.sprites_public_launch_enabled = public_launch_enabled

    @application.middleware("http")
    async def authenticated_tenant(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if tenant is not None:
            request.state.tenant = tenant
        return await call_next(request)

    application.add_exception_handler(
        RateLimitExceeded,
        cast(Any, _rate_limit_exceeded_handler),
    )
    application.include_router(router)
    limiter.reset()
    with TestClient(application) as client:
        yield client
    limiter.reset()


def test_get_runtime_returns_local_compatibility_status(auth_client: TestClient) -> None:
    """An app without a manager reports the compatible local runtime."""
    tenant = getattr(auth_client, "yinshi_tenant")

    with _runtime_client(tenant) as client:
        response = client.get("/api/runtime")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "local",
        "status": "ready",
        "artifact_version": None,
        "last_error": None,
        "runner_public_key": None,
    }


def test_get_runtime_returns_only_safe_managed_fields(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed status exposes fixed fields and a confirmed linked key only."""
    from yinshi.api import managed_runtime
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    tenant = getattr(auth_client, "yinshi_tenant")
    runtime = ManagedRuntimeStatus(
        user_id=tenant.user_id,
        runner_id="managed-runner",
        provider_name="fly_sprites",
        sprite_name="secret-sprite-name",
        lifecycle_status="ready",
        generation=2,
        artifact_version="runner-v2",
        created_at="2026-08-11T12:00:00Z",
        updated_at="2026-08-11T12:01:00Z",
        last_error=None,
    )
    runner = {
        "id": "managed-runner",
        "user_id": tenant.user_id,
        "kind": "managed",
        "cloud_provider": "fly_sprites",
        "noise_public_key": _RUNNER_PUBLIC_KEY,
        "noise_key_confirmed": True,
        "runner_token": "secret-runner-token",
        "registration_token": "secret-registration-token",
        "provider_url": "https://provider.example/private",
    }
    monkeypatch.setattr(managed_runtime, "get_managed_runtime_status", lambda user_id: runtime)
    monkeypatch.setattr(managed_runtime, "get_managed_runner_for_user", lambda user_id: runner)

    with _runtime_client(tenant, FakeManager()) as client:
        response = client.get("/api/runtime")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "fly_sprites",
        "status": "ready",
        "artifact_version": "runner-v2",
        "last_error": None,
        "runner_public_key": _RUNNER_PUBLIC_KEY,
    }
    for secret in (
        tenant.user_id,
        "secret-runner-token",
        "secret-registration-token",
        "secret-sprite-name",
        "https://provider.example/private",
    ):
        assert secret not in response.text


def test_provision_runtime_awaits_manager_and_returns_safe_status(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provisioning uses tenant identity but omits private runtime fields."""
    from yinshi.api import managed_runtime
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    tenant = getattr(auth_client, "yinshi_tenant")
    manager = FakeManager()
    manager.provision_result = ManagedRuntimeStatus(
        user_id=tenant.user_id,
        runner_id="managed-runner",
        provider_name="fly_sprites",
        sprite_name="private-sprite",
        lifecycle_status="provisioning",
        generation=1,
        artifact_version="runner-v1",
        created_at="2026-08-11T12:00:00Z",
        updated_at="2026-08-11T12:00:00Z",
        last_error=None,
    )
    monkeypatch.setattr(managed_runtime, "get_managed_runner_for_user", lambda user_id: None)

    with _runtime_client(tenant, manager, public_launch_enabled=True) as client:
        response = client.post("/api/runtime/provision")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "fly_sprites",
        "status": "provisioning",
        "artifact_version": "runner-v1",
        "last_error": None,
        "runner_public_key": None,
    }
    assert manager.calls == [("provision", tenant.user_id)]
    assert tenant.user_id not in response.text
    assert "private-sprite" not in response.text


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_detail"),
    [
        ("state", 409, "Managed runtime state is invalid"),
        ("identity", 409, "Managed runtime identity changed"),
        ("provider", 503, "Managed runtime provider unavailable"),
        ("timeout", 503, "Managed runtime wake timed out"),
    ],
)
def test_provision_runtime_maps_fixed_manager_errors(
    auth_client: TestClient,
    error: str,
    expected_status: int,
    expected_detail: str,
) -> None:
    """Manager failures retain fixed public messages and stable statuses."""
    from yinshi.services.managed_runtime_manager import (
        ManagedRuntimeIdentityError,
        ManagedRuntimeProviderError,
        ManagedRuntimeStateError,
        ManagedRuntimeTimeoutError,
    )

    errors = {
        "state": ManagedRuntimeStateError("private provider state"),
        "identity": ManagedRuntimeIdentityError("private runner identity"),
        "provider": ManagedRuntimeProviderError("private provider URL"),
        "timeout": ManagedRuntimeTimeoutError("private timeout metadata"),
    }
    tenant = getattr(auth_client, "yinshi_tenant")
    manager = FakeManager()
    manager.error = errors[error]

    with _runtime_client(tenant, manager, public_launch_enabled=True) as client:
        response = client.post("/api/runtime/provision")

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert "private" not in response.text


@pytest.mark.parametrize("public_launch_enabled", [None, False])
def test_provision_runtime_requires_public_launch_gate(
    auth_client: TestClient,
    public_launch_enabled: bool | None,
) -> None:
    """Provisioning should not reach its manager without the public launch gate."""
    tenant = getattr(auth_client, "yinshi_tenant")
    manager = FakeManager()

    with _runtime_client(
        tenant,
        manager,
        public_launch_enabled=public_launch_enabled,
    ) as client:
        response = client.post("/api/runtime/provision")

    assert response.status_code == 503
    assert response.json() == {"detail": "Managed runtime public launch is disabled"}
    assert manager.calls == []


def test_provision_runtime_requires_managed_manager(auth_client: TestClient) -> None:
    """Provisioning is unavailable when the gated manager is missing."""
    tenant = getattr(auth_client, "yinshi_tenant")

    with _runtime_client(tenant, public_launch_enabled=True) as client:
        response = client.post("/api/runtime/provision")

    assert response.status_code == 503
    assert response.json() == {"detail": "Managed runtime is unavailable"}


def test_issue_managed_capability_wakes_before_signing_and_stores_grant(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capability issuance uses the revalidated managed runner and stores its grant."""
    from yinshi.api import managed_runtime
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    tenant = getattr(auth_client, "yinshi_tenant")
    runtime = ManagedRuntimeStatus(
        user_id=tenant.user_id,
        runner_id="managed-runner",
        provider_name="fly_sprites",
        sprite_name="private-sprite",
        lifecycle_status="ready",
        generation=1,
        artifact_version="runner-v1",
        created_at="2026-08-11T12:00:00Z",
        updated_at="2026-08-11T12:01:00Z",
        last_error=None,
    )
    runner = {
        "id": "managed-runner",
        "user_id": tenant.user_id,
        "kind": "managed",
        "cloud_provider": "fly_sprites",
        "status": "online",
        "noise_public_key": _RUNNER_PUBLIC_KEY,
        "noise_key_confirmed": True,
        "runner_token": "private-runner-token",
        "registration_token": "private-registration-token",
        "provider_url": "https://provider.example/private",
    }
    manager = FakeManager()
    manager.runner = runner
    calls: list[str] = []
    original_create = managed_runtime.create_runner_capability
    original_ensure_online = manager.ensure_online

    async def ensure_online(user_id: str) -> dict[str, Any]:
        calls.append("wake")
        return await original_ensure_online(user_id)

    def create_capability(**kwargs: Any):
        calls.append("sign")
        assert manager.calls == [("ensure_online", tenant.user_id)]
        assert kwargs["runner_id"] == "managed-runner"
        assert kwargs["runner_public_key"] == _RUNNER_PUBLIC_KEY
        return original_create(**kwargs)

    def store_grant(capability: str, claims: object) -> None:
        calls.append("store")
        assert capability
        assert getattr(claims, "runner_id") == "managed-runner"

    monkeypatch.setattr(manager, "ensure_online", ensure_online)
    monkeypatch.setattr(managed_runtime, "get_managed_runtime_status", lambda user_id: runtime)
    monkeypatch.setattr(managed_runtime, "get_managed_runner_for_user", lambda user_id: runner)
    monkeypatch.setattr(managed_runtime, "create_runner_capability", create_capability)
    monkeypatch.setattr(managed_runtime, "store_runner_transfer_grant", store_grant)

    with _runtime_client(tenant, manager) as client:
        response = client.post(
            "/api/runtime/capabilities",
            json={
                "initiator_public_key": _CLIENT_PUBLIC_KEY,
                "scopes": ["workspace.read", "session.stream"],
                "max_session_bytes": 1_048_576,
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert calls == ["wake", "sign", "store"]
    assert payload["runner_id"] == "managed-runner"
    assert payload["runner_public_key"] == _RUNNER_PUBLIC_KEY
    assert payload["relay_url"] == (f"ws://testserver/api/runner/relay/{payload['transfer_id']}")
    for secret in (
        tenant.user_id,
        "private-runner-token",
        "private-registration-token",
        "private-sprite",
        "https://provider.example/private",
    ):
        assert secret not in response.text
