"""Tests for application startup behavior around default container isolation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import _configure_test_env


@pytest.fixture(autouse=True)
def explicit_no_auth_environment(monkeypatch: pytest.MonkeyPatch):
    """Main-module unit tests should use explicit loopback development mode."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    from yinshi.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _configure_startup_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    container_enabled: bool,
) -> None:
    """Prepare one isolated startup environment for lifespan tests."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=container_enabled)
    monkeypatch.setenv("CONTAINER_ENABLED", "true" if container_enabled else "false")

    from yinshi.config import get_settings

    get_settings.cache_clear()


def test_create_app_builds_independent_health_applications(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Application factory should preserve health behavior without shared app state."""
    _configure_startup_env(monkeypatch, tmp_path, container_enabled=False)
    import yinshi.main as main

    factory = getattr(main, "create_app", None)
    assert callable(factory), "yinshi.main.create_app must be public"

    first_app = factory()
    second_app = factory()
    assert first_app is not second_app
    assert first_app.state.sprites_public_launch_enabled is False
    assert second_app.state.sprites_public_launch_enabled is False
    with TestClient(first_app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_hosted_fly_mode_uses_control_plane_route_surface(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted Fly mode should expose control APIs without local execution APIs."""
    _configure_startup_env(monkeypatch, tmp_path, container_enabled=False)
    import yinshi.main as main
    from yinshi.config import Settings

    fly_settings = Settings(managed_runtime_provider="fly_sprites")
    monkeypatch.setattr(main, "get_settings", lambda: fly_settings)

    hosted_paths = set(main.create_app(mode="hosted").openapi()["paths"])

    assert "/api/runtime" in hosted_paths
    assert "/api/runtime/provision" in hosted_paths
    assert "/api/settings/runner" in hosted_paths
    assert "/auth/login/google" in hosted_paths
    assert "/api/github/installations" in hosted_paths
    assert "/runner/register" in hosted_paths
    assert "/api/repos" not in hosted_paths
    assert "/api/repos/{repo_id}/workspaces" not in hosted_paths
    assert "/api/workspaces/{workspace_id}/files/tree" not in hosted_paths
    assert "/api/workspaces/{workspace_id}/terminal" not in hosted_paths
    assert "/api/workspaces/{workspace_id}/terminals" not in hosted_paths
    assert "/api/workspaces/{workspace_id}/sessions" not in hosted_paths
    assert "/api/sessions/{session_id}/runs" not in hosted_paths
    assert "/api/catalog" not in hosted_paths
    assert "/api/settings/keys" not in hosted_paths
    assert "/api/settings/connections" not in hosted_paths
    assert "/auth/providers/{provider}/start" not in hosted_paths
    assert "/api/settings/pi-config/uploads" not in hosted_paths
    assert "/api/sessions/{session_id}/prompt" not in hosted_paths


def test_application_mode_limits_worker_route_surface(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker mode should expose execution APIs without control-plane routes."""
    _configure_startup_env(monkeypatch, tmp_path, container_enabled=False)
    from yinshi.main import create_app
    from yinshi.tenant import TenantContext
    from yinshi.worker_auth import WorkerPrincipal

    hosted_paths = set(create_app().openapi()["paths"])
    assert "/api/repos" in hosted_paths
    assert "/api/catalog" in hosted_paths
    assert "/api/settings/pi-config/uploads" in hosted_paths
    assert "/api/runtime" in hosted_paths
    assert "/api/settings/runner" in hosted_paths
    assert "/auth/login/google" in hosted_paths
    assert "/rum/api/v2/{intake_path}" in hosted_paths

    worker_data_directory = tmp_path / "worker"
    worker_principal = WorkerPrincipal(
        tenant=TenantContext(
            user_id="worker-test-user",
            email="worker@runner.invalid",
            data_dir=str(worker_data_directory),
            db_path=str(worker_data_directory / "yinshi.db"),
        ),
        bearer_token="w" * 48,
    )
    worker_paths = set(
        create_app(mode="worker", worker_principal=worker_principal).openapi()["paths"]
    )
    assert "/health" in worker_paths
    assert "/api/repos" in worker_paths
    assert "/api/settings/runner" not in worker_paths
    assert "/auth/providers/{provider}/start" in worker_paths
    assert "/auth/login/google" not in worker_paths
    assert "/rum/api/v2/{intake_path}" not in worker_paths

    desktop_app = create_app(mode="desktop")
    desktop_paths = set(desktop_app.openapi()["paths"])
    assert desktop_app.state.mode == "desktop"
    assert "/health" in desktop_paths
    assert "/api/repos" in desktop_paths
    assert "/api/settings/runner" not in desktop_paths
    assert "/auth/providers/{provider}/start" in desktop_paths
    assert "/auth/login/google" not in desktop_paths
    assert "/rum/api/v2/{intake_path}" not in desktop_paths
    with TestClient(desktop_app) as desktop_client:
        assert desktop_client.get("/api/repos").status_code == 200


def test_hosted_fly_lifespan_builds_and_closes_managed_runtime(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted Fly startup should wire validated settings into managed services."""
    _configure_startup_env(monkeypatch, tmp_path, container_enabled=False)
    bootstrap_path = tmp_path / "bootstrap.sh"
    bootstrap_path.write_bytes(b"#!/bin/sh\nexit 0\n")

    import yinshi.main as main
    from yinshi.config import Settings
    from yinshi.services.runner_relay import runner_relay_broker

    app_settings = Settings(
        managed_runtime_provider="fly_sprites",
        sprites_api_token="provider-token",
        sprites_api_url="https://sprites.invalid/v1",
        sprites_name_prefix="managed",
        sprites_name_key="n" * 32,
        sprites_artifact_url="https://artifacts.invalid/runner.tar.gz",
        sprites_artifact_sha256="a" * 64,
        sprites_allowed_domains="control.example.com,api.github.com",
        sprites_public_control_url="https://control.example.com",
        sprites_bootstrap_script_path=str(bootstrap_path),
        sprites_wake_timeout_seconds=17,
        sprites_operation_stale_seconds=901,
        sprites_reconcile_interval_seconds=601,
        sprites_reconcile_grace_seconds=1801,
        control_db_path=str(tmp_path / "control.db"),
        container_enabled=False,
        backup_encryption_key="b" * 64,
        deployment_environment="staging",
        managed_recovery_drill_enabled=True,
        managed_recovery_operator_token_hash="c" * 64,
    )
    provider_http_client = Mock()
    provider_http_client.aclose = AsyncMock()
    artifact_http_client = Mock()
    artifact_http_client.aclose = AsyncMock()
    provider = object()
    guest_installer = object()
    manager = Mock()
    manager.reconcile_startup = AsyncMock(return_value=0)
    manager.aclose = AsyncMock()
    manager.provider = provider
    backup_store = Mock()
    backup_store.preflight = AsyncMock()
    backup_manager = Mock()
    backup_manager.start = AsyncMock()
    backup_manager.aclose = AsyncMock()
    sprites_constructor = Mock(return_value=provider)
    installer_constructor = Mock(return_value=guest_installer)
    manager_constructor = Mock(return_value=manager)
    backup_manager_constructor = Mock(return_value=backup_manager)
    reconciler = Mock()
    reconciler.reconcile_classified = AsyncMock()
    reconciler.run = AsyncMock()
    recovery_controller = Mock()
    recovery_controller.aclose = AsyncMock()

    monkeypatch.setattr(main, "get_settings", lambda: app_settings)
    monkeypatch.setattr(
        main,
        "_create_provider_http_client",
        Mock(return_value=provider_http_client),
    )
    monkeypatch.setattr(
        main,
        "_create_artifact_http_client",
        Mock(return_value=artifact_http_client),
    )
    monkeypatch.setattr(main, "SpritesClient", sprites_constructor)
    monkeypatch.setattr(main, "ConcreteManagedGuestInstaller", installer_constructor)
    monkeypatch.setattr(main, "ManagedRuntimeManager", manager_constructor)
    monkeypatch.setattr(main, "ManagedBackupManager", backup_manager_constructor)
    monkeypatch.setattr(main, "ManagedSpriteReconciler", Mock(return_value=reconciler))
    monkeypatch.setattr(
        main,
        "ManagedRecoveryDrillController",
        Mock(return_value=recovery_controller),
    )
    monkeypatch.setattr(
        main,
        "create_managed_backup_store",
        Mock(return_value=backup_store),
        raising=False,
    )

    application = main.create_app(mode="hosted")

    async def assert_manager_not_published() -> int:
        assert application.state.managed_runtime_manager is None
        return 0

    manager.reconcile_startup.side_effect = assert_manager_not_published
    with TestClient(application):
        assert application.state.managed_runtime_manager is manager
        assert application.state.managed_backup_store is backup_store
        assert application.state.managed_backup_manager is backup_manager
        assert application.state.managed_recovery_drill_controller is recovery_controller

    recovery_controller.aclose.assert_awaited_once()
    sprites_constructor.assert_called_once_with(
        api_token="provider-token",
        http_client=provider_http_client,
    )
    installer_kwargs = installer_constructor.call_args.kwargs
    assert installer_kwargs["client"] is provider
    assert installer_kwargs["bootstrap_script"] == b"#!/bin/sh\nexit 0\n"
    assert installer_kwargs["relay_idle_timeout_seconds"] == 20.0
    assert installer_kwargs["bootstrap_timeout_seconds"] == 901
    manager_constructor.assert_called_once_with(
        provider=provider,
        guest_installer=guest_installer,
        http_client=artifact_http_client,
        name_prefix="managed",
        name_key="n" * 32,
        artifact_url="https://artifacts.invalid/runner.tar.gz",
        artifact_sha256="a" * 64,
        artifact_version="a" * 64,
        allowed_domains=("control.example.com", "api.github.com"),
        region="global",
        control_url="https://control.example.com",
        readiness_timeout_seconds=17,
        is_runner_connected=runner_relay_broker.is_runner_connected,
    )
    assert not Path(app_settings.db_path).exists()
    backup_store.preflight.assert_awaited_once_with()
    manager.reconcile_startup.assert_awaited_once_with()
    reconciler.reconcile_classified.assert_awaited_once_with(raise_on_failure=True)
    assert reconciler.run.await_args.kwargs["interval_seconds"] == 601
    backup_manager.start.assert_awaited_once_with()
    backup_manager.aclose.assert_awaited_once_with()
    manager.aclose.assert_awaited_once()
    provider_http_client.aclose.assert_awaited_once()
    artifact_http_client.aclose.assert_not_awaited()


def test_hosted_fly_startup_reconciles_provider_inventory_before_serving(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted Fly startup must complete Sprite inventory cleanup before serving."""
    _configure_startup_env(monkeypatch, tmp_path, container_enabled=False)
    import yinshi.main as main
    from yinshi.config import Settings

    events: list[str] = []
    settings = Settings(
        managed_runtime_provider="fly_sprites",
        control_db_path=str(tmp_path / "control.db"),
        container_enabled=False,
        backup_encryption_key="b" * 64,
        sprites_name_key="n" * 32,
    )
    runtime = Mock(provider=object())
    runtime.reconcile_startup = AsyncMock(side_effect=lambda: events.append("runtime"))
    runtime.aclose = AsyncMock()
    provider_client = Mock()
    provider_client.aclose = AsyncMock()
    store = Mock()
    store.preflight = AsyncMock()
    backup = Mock()
    backup.start = AsyncMock(side_effect=lambda: events.append("backup"))
    backup.aclose = AsyncMock()
    reconciler = Mock()
    reconciler.reconcile_classified = AsyncMock(
        side_effect=lambda **_values: events.append("inventory")
    )
    reconciler.run = AsyncMock()

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "create_managed_backup_store", lambda _settings: store)
    managed_runtime = main.HostedManagedRuntime(
        runtime_manager=runtime,
        backup_provider=runtime.provider,
        inventory_provider=runtime.provider,
        provider_http_client=provider_client,
    )
    monkeypatch.setattr(
        main,
        "_initialize_managed_runtime",
        AsyncMock(return_value=managed_runtime),
    )
    monkeypatch.setattr(main, "ManagedBackupManager", Mock(return_value=backup))
    monkeypatch.setattr(main, "ManagedSpriteReconciler", Mock(return_value=reconciler))

    application = main.create_app(mode="hosted")
    with TestClient(application):
        events.append("serving")

    assert events[:4] == ["runtime", "inventory", "backup", "serving"]
    assert reconciler.run.await_count == 1
    assert reconciler.run.await_args.kwargs["interval_seconds"] == 900


@pytest.mark.asyncio
async def test_lifespan_attempts_every_cleanup_before_raising_first_error(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown runs every cleanup and then raises its first non-cancellation error."""
    _configure_startup_env(monkeypatch, tmp_path, container_enabled=True)
    import yinshi.main as main
    import yinshi.services.container as container_service
    from yinshi.config import Settings

    cleanup_calls: list[str] = []
    reaper_started = asyncio.Event()

    class FakePromptJournal:
        async def close(self) -> None:
            cleanup_calls.append("prompt journal")
            raise RuntimeError("prompt cleanup failed")

    class FakeTerminalJournal:
        async def close_all(self) -> None:
            cleanup_calls.append("terminal journal")
            raise RuntimeError("terminal cleanup failed")

    class FakeContainerManager:
        def __init__(self, *, settings: Settings) -> None:
            self.settings = settings

        async def initialize(self) -> None:
            return None

        async def run_reaper(self) -> None:
            reaper_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_calls.append("reaper")
                raise RuntimeError("reaper cleanup failed")

        async def destroy_all(self) -> None:
            cleanup_calls.append("container manager")
            raise RuntimeError("container cleanup failed")

    class FakeManagedRuntimeManager:
        provider = object()

        async def reconcile_startup(self) -> int:
            return 0

        async def aclose(self) -> None:
            cleanup_calls.append("managed manager")
            raise RuntimeError("managed cleanup failed")

    class FakeManagedBackupManager:
        def __init__(self, **_values) -> None:
            pass

        async def start(self) -> None:
            return None

        async def aclose(self) -> None:
            cleanup_calls.append("backup manager")
            raise RuntimeError("backup cleanup failed")

    class FakeProviderClient:
        async def aclose(self) -> None:
            cleanup_calls.append("provider client")
            raise RuntimeError("provider cleanup failed")

    class FakeRelayProcessLock:
        def __init__(self, path: Path) -> None:
            self.path = path

        def acquire(self) -> None:
            return None

        def release(self) -> None:
            cleanup_calls.append("relay lock")
            raise RuntimeError("relay cleanup failed")

    app_settings = Settings(
        managed_runtime_provider="fly_sprites",
        control_db_path=str(tmp_path / "control.db"),
        container_enabled=True,
        backup_encryption_key="b" * 64,
        sprites_name_key="n" * 32,
    )
    managed_manager = FakeManagedRuntimeManager()
    provider_client = FakeProviderClient()
    backup_store = Mock()
    backup_store.preflight = AsyncMock()
    application = FastAPI()
    application.state.mode = "hosted"
    application.state.prompt_journal = FakePromptJournal()
    application.state.terminal_journal = FakeTerminalJournal()

    monkeypatch.setattr(main, "get_settings", lambda: app_settings)
    monkeypatch.setattr(main, "init_control_db", Mock())
    monkeypatch.setattr(main, "setup_oauth", Mock())
    monkeypatch.setattr(main, "PromptJournal", FakePromptJournal)
    monkeypatch.setattr(main, "TerminalJournal", FakeTerminalJournal)
    monkeypatch.setattr(container_service, "ContainerManager", FakeContainerManager)
    monkeypatch.setattr(main, "RelayProcessLock", FakeRelayProcessLock)
    monkeypatch.setattr(main, "ManagedBackupManager", FakeManagedBackupManager)
    reconciler = Mock()
    reconciler.reconcile_classified = AsyncMock()
    reconciler.run = AsyncMock()
    monkeypatch.setattr(main, "ManagedSpriteReconciler", Mock(return_value=reconciler))
    monkeypatch.setattr(main, "create_managed_backup_store", lambda _settings: backup_store)
    managed_runtime = main.HostedManagedRuntime(
        runtime_manager=managed_manager,
        backup_provider=managed_manager.provider,
        inventory_provider=managed_manager.provider,
        provider_http_client=provider_client,
    )
    monkeypatch.setattr(
        main,
        "_initialize_managed_runtime",
        AsyncMock(return_value=managed_runtime),
    )

    with pytest.raises(RuntimeError, match="prompt cleanup failed"):
        async with main.lifespan(application):
            await asyncio.wait_for(reaper_started.wait(), timeout=1)

    assert cleanup_calls == [
        "prompt journal",
        "terminal journal",
        "reaper",
        "container manager",
        "backup manager",
        "managed manager",
        "provider client",
        "relay lock",
    ]


def test_hosted_fly_storage_preflight_logs_stable_alert_code(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hosted storage failure must emit the monitoring alert code."""
    _configure_startup_env(monkeypatch, tmp_path, container_enabled=False)

    import yinshi.main as main
    from yinshi.config import Settings

    app_settings = Settings(
        managed_runtime_provider="fly_sprites",
        control_db_path=str(tmp_path / "control.db"),
        container_enabled=False,
    )
    backup_store = Mock()
    backup_store.preflight = AsyncMock(side_effect=RuntimeError("storage unavailable"))
    initialize_runtime = AsyncMock()
    monkeypatch.setattr(main, "get_settings", lambda: app_settings)
    monkeypatch.setattr(main, "create_managed_backup_store", lambda _settings: backup_store)
    monkeypatch.setattr(main, "_initialize_managed_runtime", initialize_runtime)

    with pytest.raises(RuntimeError, match="storage unavailable"):
        with TestClient(main.create_app(mode="hosted")):
            pass

    initialize_runtime.assert_not_awaited()
    assert "managed_storage_preflight_failed" in caplog.text


def test_hosted_fly_partial_startup_closes_provider_client(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed startup failure should close the provider client before propagating."""
    _configure_startup_env(monkeypatch, tmp_path, container_enabled=False)
    bootstrap_path = tmp_path / "bootstrap.sh"
    bootstrap_path.write_bytes(b"exit 0\n")

    import yinshi.main as main
    from yinshi.config import Settings

    app_settings = Settings(
        managed_runtime_provider="fly_sprites",
        sprites_bootstrap_script_path=str(bootstrap_path),
        control_db_path=str(tmp_path / "control.db"),
        container_enabled=False,
    )
    provider_http_client = Mock()
    provider_http_client.aclose = AsyncMock()
    backup_store = Mock()
    backup_store.preflight = AsyncMock()
    monkeypatch.setattr(main, "get_settings", lambda: app_settings)
    monkeypatch.setattr(main, "create_managed_backup_store", lambda _settings: backup_store)
    monkeypatch.setattr(
        main,
        "_create_provider_http_client",
        Mock(return_value=provider_http_client),
    )
    monkeypatch.setattr(
        main,
        "_create_artifact_http_client",
        Mock(side_effect=RuntimeError("artifact client failed")),
    )

    with pytest.raises(RuntimeError, match="artifact client failed"):
        with TestClient(main.create_app(mode="hosted")):
            pass

    provider_http_client.aclose.assert_awaited_once()


def test_desktop_and_worker_lifespans_skip_managed_provider_clients(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-hosted modes should never initialize managed provider clients."""
    _configure_startup_env(monkeypatch, tmp_path, container_enabled=False)
    import yinshi.main as main
    from yinshi.config import Settings
    from yinshi.tenant import TenantContext
    from yinshi.worker_auth import WorkerPrincipal

    app_settings = Settings(
        managed_runtime_provider="fly_sprites",
        control_db_path=str(tmp_path / "control.db"),
        container_enabled=False,
    )
    provider_factory = Mock(side_effect=AssertionError("provider client initialized"))
    artifact_factory = Mock(side_effect=AssertionError("artifact client initialized"))
    monkeypatch.setattr(main, "get_settings", lambda: app_settings)
    monkeypatch.setattr(main, "_create_provider_http_client", provider_factory)
    monkeypatch.setattr(main, "_create_artifact_http_client", artifact_factory)

    worker_directory = tmp_path / "worker-lifecycle"
    worker_principal = WorkerPrincipal(
        tenant=TenantContext(
            user_id="worker-lifecycle-user",
            email="worker-lifecycle@runner.invalid",
            data_dir=str(worker_directory),
            db_path=str(worker_directory / "yinshi.db"),
        ),
        bearer_token="w" * 48,
    )
    applications = (
        main.create_app(mode="desktop"),
        main.create_app(mode="worker", worker_principal=worker_principal),
    )
    for application in applications:
        with TestClient(application):
            pass

    provider_factory.assert_not_called()
    artifact_factory.assert_not_called()


def test_desktop_mode_serves_spa_with_restricted_fallback_and_csp(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Desktop mode should serve its packaged UI without masking missing API assets."""
    _configure_startup_env(monkeypatch, tmp_path, container_enabled=False)
    asset_dir = tmp_path / "frontend"
    (asset_dir / "assets").mkdir(parents=True)
    (asset_dir / "index.html").write_text("<main>Yinshi desktop</main>", encoding="utf-8")
    (asset_dir / "assets" / "app.js").write_text("window.yinshi = true;", encoding="utf-8")

    from yinshi.main import create_app

    desktop_app = create_app(mode="desktop", desktop_asset_dir=asset_dir)
    with TestClient(desktop_app) as client:
        root_response = client.get("/")
        deep_link_response = client.get("/app/session/session-id")
        asset_response = client.get("/assets/app.js")
        missing_asset_response = client.get("/assets/missing.js")
        missing_api_response = client.get("/api/not-a-route")

    assert root_response.text == "<main>Yinshi desktop</main>"
    assert deep_link_response.text == "<main>Yinshi desktop</main>"
    assert asset_response.text == "window.yinshi = true;"
    assert missing_asset_response.status_code == 404
    assert missing_api_response.status_code == 404
    csp = root_response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_startup_fails_closed_when_podman_is_missing(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container-enabled startup should fail closed when Podman is unavailable."""
    _configure_startup_env(monkeypatch, tmp_path, container_enabled=True)

    from yinshi.config import get_settings
    from yinshi.exceptions import ContainerStartError
    from yinshi.main import app

    with (
        patch(
            "yinshi.services.container.ContainerManager.initialize",
            new=AsyncMock(side_effect=ContainerStartError("podman binary not found")),
        ),
        pytest.raises(ContainerStartError, match="podman binary not found"),
    ):
        with TestClient(app):
            pass

    get_settings.cache_clear()


def test_startup_fails_closed_when_image_is_missing(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container-enabled startup should fail closed when the image is missing."""
    _configure_startup_env(monkeypatch, tmp_path, container_enabled=True)

    from yinshi.config import get_settings
    from yinshi.exceptions import ContainerStartError
    from yinshi.main import app

    with (
        patch(
            "yinshi.services.container.ContainerManager.initialize",
            new=AsyncMock(
                side_effect=ContainerStartError(
                    "Configured sidecar image is not available locally: yinshi-sidecar:latest"
                )
            ),
        ),
        pytest.raises(ContainerStartError, match="Configured sidecar image"),
    ):
        with TestClient(app):
            pass

    get_settings.cache_clear()


def test_transport_security_redirects_plain_http() -> None:
    """Transport middleware should redirect HTTP when HTTPS is required."""
    from yinshi.main import TransportSecurityMiddleware

    test_app = FastAPI()
    test_app.add_middleware(
        TransportSecurityMiddleware,
        require_https=True,
        hsts_enabled=True,
    )

    @test_app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(test_app, follow_redirects=False) as client:
        response = client.get("http://testserver/health")

    assert response.status_code == 307
    assert response.headers["location"] == "https://testserver/health"


def test_transport_security_ignores_forwarded_proto_from_untrusted_client() -> None:
    """Clients outside the proxy allowlist must not spoof HTTPS headers."""
    from yinshi.main import TransportSecurityMiddleware

    test_app = FastAPI()
    test_app.add_middleware(
        TransportSecurityMiddleware,
        require_https=True,
        hsts_enabled=True,
        trusted_proxy_ips={"127.0.0.1"},
    )

    @test_app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(test_app, follow_redirects=False) as client:
        response = client.get("/health", headers={"X-Forwarded-Proto": "https"})

    assert response.status_code == 307
    assert "Strict-Transport-Security" not in response.headers


def test_transport_security_adds_hsts_for_https_forwarded_proto() -> None:
    """Transport middleware should add HSTS when the edge reports HTTPS."""
    from yinshi.main import TransportSecurityMiddleware

    test_app = FastAPI()
    test_app.add_middleware(
        TransportSecurityMiddleware,
        require_https=True,
        hsts_enabled=True,
        trusted_proxy_ips={"testclient"},
    )

    @test_app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(test_app) as client:
        response = client.get("/health", headers={"X-Forwarded-Proto": "https"})

    assert response.status_code == 200
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"


def test_security_headers_are_attached_to_api_responses() -> None:
    """API responses should prevent sniffing, framing, and referrer disclosure."""
    from yinshi.main import SecurityHeadersMiddleware

    test_app = FastAPI()
    test_app.add_middleware(SecurityHeadersMiddleware)

    @test_app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(test_app) as client:
        response = client.get("/health")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_request_body_limit_rejects_oversized_payload() -> None:
    """Global body limits should reject data before endpoint buffering."""
    from yinshi.main import RequestBodyLimitMiddleware

    test_app = FastAPI()
    test_app.add_middleware(RequestBodyLimitMiddleware, body_bytes_max=4)

    @test_app.post("/echo")
    async def echo(request) -> dict[str, int]:
        return {"size": len(await request.body())}

    with TestClient(test_app) as client:
        response = client.post("/echo", content=b"12345")

    assert response.status_code == 413


def test_startup_without_containers_skips_podman(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container-disabled startup should still serve requests without Podman."""
    _configure_startup_env(monkeypatch, tmp_path, container_enabled=False)

    from yinshi.config import get_settings
    from yinshi.main import app

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    get_settings.cache_clear()
