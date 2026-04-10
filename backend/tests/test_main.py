"""Tests for application startup behavior around default container isolation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import _configure_test_env


def _configure_startup_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    container_enabled: bool,
) -> None:
    """Prepare one isolated startup environment for lifespan tests."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("CONTAINER_ENABLED", "true" if container_enabled else "false")

    from yinshi.config import get_settings

    get_settings.cache_clear()


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
