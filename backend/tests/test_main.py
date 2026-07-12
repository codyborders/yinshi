"""Tests for application startup behavior around default container isolation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
    with TestClient(first_app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
