"""Tests for the provider/model catalog and unsupported-provider guardrails."""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from tests.factories import create_full_stack, make_mock_sidecar


def test_catalog_malformed_host_response_uses_tenant_fallback(
    auth_client: TestClient,
) -> None:
    """Malformed host catalog responses should use tenant fallback."""

    async def unexpected_query(*_args: object, **_kwargs: object):
        if False:
            yield {}
        raise AssertionError("query should not be called")

    host_sidecar = make_mock_sidecar(unexpected_query)
    host_sidecar.get_catalog = AsyncMock(side_effect=json.JSONDecodeError("bad catalog", "{", 0))
    tenant_sidecar = make_mock_sidecar(unexpected_query)
    tenant_sidecar.get_catalog = AsyncMock(
        return_value={
            "default_model": "openai/gpt-4o-mini",
            "providers": [{"id": "openai", "model_count": 1}],
            "models": [],
        }
    )

    with (
        patch(
            "yinshi.api.catalog.create_sidecar_connection",
            new=AsyncMock(side_effect=[host_sidecar, tenant_sidecar]),
        ),
        patch(
            "yinshi.api.catalog.resolve_tenant_sidecar_context",
            new=AsyncMock(return_value=Mock(socket_path="/tmp/tenant.sock", agent_dir=None)),
        ),
        patch("yinshi.api.catalog.touch_tenant_container"),
    ):
        response = auth_client.get("/api/catalog")

    assert response.status_code == 200
    assert response.json()["default_model"] == "openai/gpt-4o-mini"


def test_catalog_filters_unsupported_providers(auth_client: TestClient) -> None:
    """Catalog responses should only expose providers that Yinshi can actually drive."""

    async def unexpected_query(*args, **kwargs):
        if False:
            yield {}
        raise AssertionError("query should not be called")

    mock_sidecar = make_mock_sidecar(unexpected_query)
    mock_sidecar.get_catalog = AsyncMock(
        return_value={
            "default_model": "minimax/MiniMax-M2.7",
            "providers": [
                {"id": "openai", "model_count": 1},
                {"id": "amazon-bedrock", "model_count": 1},
            ],
            "models": [
                {
                    "ref": "openai/gpt-4o-mini",
                    "provider": "openai",
                    "id": "gpt-4o-mini",
                    "label": "GPT-4o Mini",
                    "api": "openai-responses",
                    "reasoning": False,
                    "inputs": ["text"],
                    "context_window": 128000,
                    "max_tokens": 16384,
                },
                {
                    "ref": "amazon-bedrock/us.anthropic.claude-opus-4-6-v1:0",
                    "provider": "amazon-bedrock",
                    "id": "us.anthropic.claude-opus-4-6-v1:0",
                    "label": "Claude Opus 4.6 via Bedrock",
                    "api": "bedrock-converse-stream",
                    "reasoning": True,
                    "inputs": ["text"],
                    "context_window": 200000,
                    "max_tokens": 16384,
                },
            ],
        }
    )

    with (
        patch(
            "yinshi.api.catalog.create_sidecar_connection", return_value=mock_sidecar
        ) as create_conn,
        patch(
            "yinshi.api.catalog.resolve_tenant_sidecar_context",
            new=AsyncMock(),
        ) as resolve_context,
        patch("yinshi.api.catalog.touch_tenant_container") as touch_container,
    ):
        resp = auth_client.get("/api/catalog")

    assert resp.status_code == 200
    payload = resp.json()
    assert [provider["id"] for provider in payload["providers"]] == ["openai"]
    assert [model["provider"] for model in payload["models"]] == ["openai"]
    create_conn.assert_awaited_once_with(None)
    mock_sidecar.get_catalog.assert_awaited_once_with(agent_dir=None)
    resolve_context.assert_not_awaited()
    touch_container.assert_not_called()


def test_catalog_fallback_releases_exact_activity_reservation(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant fallback reserves its current container generation during sidecar work."""
    from yinshi.config import Settings
    from yinshi.exceptions import SidecarNotConnectedError
    from yinshi.main import app
    from yinshi.services.sidecar_runtime import TenantSidecarContext

    settings = Settings(
        container_enabled=True,
        google_client_id="test-client",
        google_client_secret="test-secret",
        managed_runtime_provider="disabled",
        _env_file=None,
    )
    monkeypatch.setattr("yinshi.services.sidecar_runtime.get_settings", lambda: settings)
    tenant_sidecar_context = TenantSidecarContext(
        socket_path="/tmp/tenant-sidecar.sock",
        agent_dir=None,
        settings_payload=None,
    )
    reservation = object()
    container_manager = Mock()
    container_manager.acquire_activity = AsyncMock(return_value=reservation)
    container_manager.release_activity = AsyncMock()

    async def unexpected_query(*args, **kwargs):
        if False:
            yield {}
        raise AssertionError("query should not be called")

    mock_sidecar = make_mock_sidecar(unexpected_query)
    mock_sidecar.get_catalog = AsyncMock(
        return_value={
            "default_model": "openai/gpt-4o-mini",
            "providers": [{"id": "openai", "model_count": 1}],
            "models": [],
        }
    )
    previous_manager = app.state.container_manager
    app.state.container_manager = container_manager
    try:
        with (
            patch(
                "yinshi.api.catalog.create_sidecar_connection",
                new=AsyncMock(
                    side_effect=[
                        SidecarNotConnectedError("host socket missing"),
                        mock_sidecar,
                    ]
                ),
            ),
            patch(
                "yinshi.api.catalog.resolve_tenant_sidecar_context",
                new=AsyncMock(return_value=tenant_sidecar_context),
            ),
        ):
            response = auth_client.get("/api/catalog")
    finally:
        app.state.container_manager = previous_manager

    assert response.status_code == 200
    container_manager.acquire_activity.assert_awaited_once()
    container_manager.release_activity.assert_awaited_once_with(reservation)


def test_catalog_returns_503_when_sidecars_are_unavailable(auth_client: TestClient) -> None:
    """Catalog should fail closed when no sidecar socket can be reached."""
    from yinshi.exceptions import SidecarNotConnectedError
    from yinshi.services.sidecar_runtime import TenantSidecarContext

    tenant_sidecar_context = TenantSidecarContext(
        socket_path="/tmp/tenant-sidecar.sock",
        agent_dir=None,
        settings_payload=None,
    )

    with (
        patch(
            "yinshi.api.catalog.create_sidecar_connection",
            new=AsyncMock(side_effect=SidecarNotConnectedError("socket missing")),
        ),
        patch(
            "yinshi.api.catalog.resolve_tenant_sidecar_context",
            new=AsyncMock(return_value=tenant_sidecar_context),
        ),
        patch("yinshi.api.catalog.touch_tenant_container") as touch_container,
    ):
        response = auth_client.get("/api/catalog")

    assert response.status_code == 503
    assert response.json()["detail"] == "Agent environment temporarily unavailable"
    touch_container.assert_called_once()


def test_prompt_rejects_unsupported_provider(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Prompt execution should fail cleanly for providers hidden from the catalog."""
    stack = create_full_stack(auth_client, git_repo, name="unsupported-provider")

    async def unexpected_query(*args, **kwargs):
        if False:
            yield {}
        raise AssertionError("query should not be called")

    mock_sidecar = make_mock_sidecar(unexpected_query)
    mock_sidecar.resolve_model = AsyncMock(
        return_value={
            "provider": "amazon-bedrock",
            "model": "amazon-bedrock/us.anthropic.claude-opus-4-6-v1:0",
        }
    )

    with patch("yinshi.api.stream.create_sidecar_connection", return_value=mock_sidecar):
        resp = auth_client.post(
            f"/api/sessions/{stack['session']['id']}/prompt",
            json={"prompt": "hello"},
        )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Provider amazon-bedrock is not supported in Yinshi yet"
