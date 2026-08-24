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

    def __init__(self, *, artifact_version: str = "runner-v1") -> None:
        self.artifact_version = artifact_version
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

    async def ensure_online(self, user_id: str):
        from yinshi.services.managed_runtime_manager import OnlineManagedRunner

        self.calls.append(("ensure_online", user_id))
        if self.error is not None:
            raise self.error
        assert self.runner is not None
        return OnlineManagedRunner(
            runner_id=str(self.runner["id"]),
            runner_public_key=str(self.runner["noise_public_key"]),
        )


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


def test_managed_backup_list_returns_only_safe_catalog_fields(
    auth_client: TestClient,
) -> None:
    """Authenticated backup listing should omit object and key metadata."""
    from datetime import datetime, timezone

    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import start_managed_backup_creation
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=now,
    )
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'ready' WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.commit()
    start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=1,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e8f",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e90",
        object_key="managed/v1/private.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="a" * 64,
        now=now,
    )

    response = auth_client.get("/api/runtime/backups")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e8f",
            "status": "creating",
            "size_bytes": None,
            "created_at": "2026-08-12T12:00:00Z",
            "completed_at": None,
            "last_error": None,
        }
    ]


def test_managed_backup_create_returns_safe_accepted_job(
    auth_client: TestClient,
) -> None:
    """Authenticated tenants can queue one encrypted managed backup."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import Mock

    from yinshi.db import get_control_db
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=now,
    )
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'ready' WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.commit()
    manager = Mock()
    manager.enqueue_create = Mock(
        return_value=SimpleNamespace(
            job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e91",
            archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e92",
            operation="create",
            status="running",
            phase="claimed",
            started_at="2026-08-12T12:00:00Z",
            updated_at="2026-08-12T12:00:00Z",
            last_error=None,
        )
    )
    manager.wake = Mock()
    auth_client.app.state.managed_backup_manager = manager

    response = auth_client.post("/api/runtime/backups")

    assert response.status_code == 202
    assert response.json() == {
        "id": "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e91",
        "archive_id": "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e92",
        "operation": "create",
        "status": "running",
        "phase": "claimed",
        "started_at": "2026-08-12T12:00:00Z",
        "updated_at": "2026-08-12T12:00:00Z",
        "last_error": None,
    }
    manager.enqueue_create.assert_called_once_with(tenant.user_id)
    manager.wake.assert_called_once_with()


def test_managed_backup_job_status_omits_lease_and_provider_fields(
    auth_client: TestClient,
) -> None:
    """Job status should expose progress without worker ownership or provider IDs."""
    from datetime import datetime, timezone

    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import start_managed_backup_creation
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=now,
    )
    with get_control_db() as database:
        database.execute(
            "UPDATE managed_runtimes SET lifecycle_status = 'ready' WHERE user_id = ?",
            (tenant.user_id,),
        )
        database.commit()
    start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=1,
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e93",
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e94",
        object_key="managed/v1/job.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="a" * 64,
        now=now,
    )

    response = auth_client.get("/api/runtime/backup-jobs/018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e94")

    assert response.status_code == 200
    assert response.json() == {
        "id": "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e94",
        "archive_id": "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e93",
        "operation": "create",
        "status": "running",
        "phase": "claimed",
        "started_at": "2026-08-12T12:00:00Z",
        "updated_at": "2026-08-12T12:00:00Z",
        "last_error": None,
    }


def test_managed_backup_mutation_cross_tenant_archive_returns_not_found(
    auth_client: TestClient,
) -> None:
    """Mutation APIs should conceal archives owned by another tenant."""
    from unittest.mock import Mock

    manager = Mock()
    manager.enqueue_restore = Mock(side_effect=LookupError("other tenant"))
    auth_client.app.state.managed_backup_manager = manager

    response = auth_client.post("/api/runtime/backups/private-archive/restore")

    assert response.status_code == 404
    assert response.json() == {"detail": "Managed backup was not found"}


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
        "history_bundle_supported": False,
        "runner_rpc_push_supported": False,
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

    with _runtime_client(tenant, FakeManager(artifact_version="runner-v2")) as client:
        response = client.get("/api/runtime")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "fly_sprites",
        "status": "ready",
        "artifact_version": "runner-v2",
        "last_error": None,
        "runner_public_key": _RUNNER_PUBLIC_KEY,
        "history_bundle_supported": True,
        "runner_rpc_push_supported": True,
    }
    for secret in (
        tenant.user_id,
        "secret-runner-token",
        "secret-registration-token",
        "secret-sprite-name",
        "https://provider.example/private",
    ):
        assert secret not in response.text


def test_get_runtime_does_not_advertise_bundle_for_stale_artifact(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready runtime must match the configured artifact before advertising support."""
    from yinshi.api import managed_runtime
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    tenant = getattr(auth_client, "yinshi_tenant")
    runtime = ManagedRuntimeStatus(
        user_id=tenant.user_id,
        runner_id="managed-runner",
        provider_name="fly_sprites",
        sprite_name="sprite",
        lifecycle_status="ready",
        generation=2,
        artifact_version="runner-v1",
        created_at="2026-08-11T12:00:00Z",
        updated_at="2026-08-11T12:01:00Z",
        last_error=None,
    )
    monkeypatch.setattr(managed_runtime, "get_managed_runtime_status", lambda user_id: runtime)
    monkeypatch.setattr(managed_runtime, "get_managed_runner_for_user", lambda user_id: None)

    with _runtime_client(tenant, FakeManager(artifact_version="runner-v2")) as client:
        response = client.get("/api/runtime")

    assert response.status_code == 200
    assert response.json()["history_bundle_supported"] is False
    assert response.json()["runner_rpc_push_supported"] is False


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
        "history_bundle_supported": False,
        "runner_rpc_push_supported": False,
    }
    assert manager.calls == [("provision", tenant.user_id)]
    assert tenant.user_id not in response.text
    assert "private-sprite" not in response.text


def test_provision_runtime_advertises_bundle_for_current_ready_artifact(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provision response advertises support only for the manager's ready artifact."""
    from yinshi.api import managed_runtime
    from yinshi.services.managed_runners import ManagedRuntimeStatus

    tenant = getattr(auth_client, "yinshi_tenant")
    manager = FakeManager(artifact_version="runner-v2")
    manager.provision_result = ManagedRuntimeStatus(
        user_id=tenant.user_id,
        runner_id="managed-runner",
        provider_name="fly_sprites",
        sprite_name="private-sprite",
        lifecycle_status="ready",
        generation=1,
        artifact_version="runner-v2",
        created_at="2026-08-11T12:00:00Z",
        updated_at="2026-08-11T12:00:00Z",
        last_error=None,
    )
    monkeypatch.setattr(managed_runtime, "get_managed_runner_for_user", lambda user_id: None)

    with _runtime_client(tenant, manager, public_launch_enabled=True) as client:
        response = client.post("/api/runtime/provision")

    assert response.status_code == 200
    assert response.json()["history_bundle_supported"] is True
    assert response.json()["runner_rpc_push_supported"] is True


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

    tenant = getattr(auth_client, "yinshi_tenant")
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

    async def ensure_online(user_id: str):
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

    def fail_reload(user_id: str):
        raise AssertionError("capability route must not reload managed runtime state")

    monkeypatch.setattr(managed_runtime, "get_managed_runtime_status", fail_reload)
    monkeypatch.setattr(managed_runtime, "get_managed_runner_for_user", fail_reload)
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
