"""Tests for the provider/model catalog and unsupported-provider guardrails."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.factories import create_full_stack, make_mock_sidecar


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

    tenant = getattr(auth_client, "yinshi_tenant")
    from yinshi.services.sidecar_runtime import TenantSidecarContext

    tenant_sidecar_context = TenantSidecarContext(
        socket_path="/tmp/tenant-sidecar.sock",
        agent_dir=str(tenant.data_dir),
        settings_payload=None,
    )

    with (
        patch(
            "yinshi.api.catalog.create_sidecar_connection", return_value=mock_sidecar
        ) as create_conn,
        patch(
            "yinshi.api.catalog.resolve_tenant_sidecar_context",
            new=AsyncMock(return_value=tenant_sidecar_context),
        ),
        patch("yinshi.api.catalog.touch_tenant_container") as touch_container,
    ):
        resp = auth_client.get("/api/catalog")

    assert resp.status_code == 200
    payload = resp.json()
    assert [provider["id"] for provider in payload["providers"]] == ["openai"]
    assert [model["provider"] for model in payload["models"]] == ["openai"]
    create_conn.assert_awaited_once_with("/tmp/tenant-sidecar.sock")
    mock_sidecar.get_catalog.assert_awaited_once_with(agent_dir=str(tenant.data_dir))
    touch_container.assert_called_once()


def test_catalog_returns_503_when_tenant_sidecar_is_unavailable(auth_client: TestClient) -> None:
    """Catalog should fail closed when the tenant sidecar socket cannot be reached."""
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
