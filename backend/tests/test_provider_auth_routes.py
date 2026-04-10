"""Tests for provider OAuth routes, including hosted manual callback input."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.factories import make_mock_sidecar


def _tenant_sidecar_context() -> object:
    """Build a fixed tenant sidecar context for route tests."""
    from yinshi.services.sidecar_runtime import TenantSidecarContext

    return TenantSidecarContext(
        socket_path="/tmp/tenant-oauth.sock",
        agent_dir=None,
        settings_payload=None,
    )


def test_start_provider_auth_exposes_manual_input_metadata(auth_client: TestClient) -> None:
    """Start should expose whether the UI must accept pasted localhost callback URLs."""

    async def unexpected_query(*args, **kwargs):
        if False:
            yield {}
        raise AssertionError("query should not be called")

    mock_sidecar = make_mock_sidecar(unexpected_query)
    mock_sidecar.start_oauth_flow = AsyncMock(
        return_value={
            "flow_id": "flow-openai-codex",
            "provider": "openai-codex",
            "auth_url": "https://auth.openai.com/oauth/authorize?redirect_uri=http://localhost:1455/auth/callback",
            "instructions": "Open the browser and sign in.",
            "manual_input_required": True,
            "manual_input_prompt": "Paste the final redirect URL or authorization code here.",
            "manual_input_submitted": False,
        }
    )

    with (
        patch(
            "yinshi.api.auth_routes.create_sidecar_connection", return_value=mock_sidecar
        ) as create_conn,
        patch(
            "yinshi.api.auth_routes.resolve_tenant_sidecar_context",
            new=AsyncMock(return_value=_tenant_sidecar_context()),
        ),
        patch("yinshi.api.auth_routes.protect_tenant_container") as protect_container,
        patch("yinshi.api.auth_routes.touch_tenant_container") as touch_container,
    ):
        response = auth_client.post("/auth/providers/openai-codex/start")

    assert response.status_code == 200
    assert response.json() == {
        "flow_id": "flow-openai-codex",
        "provider": "openai-codex",
        "auth_url": "https://auth.openai.com/oauth/authorize?redirect_uri=http://localhost:1455/auth/callback",
        "instructions": "Open the browser and sign in.",
        "manual_input_required": True,
        "manual_input_prompt": "Paste the final redirect URL or authorization code here.",
        "manual_input_submitted": False,
    }
    create_conn.assert_awaited_once_with("/tmp/tenant-oauth.sock")
    protect_container.assert_called_once()
    touch_container.assert_called_once()


def test_submit_provider_auth_callback_feeds_manual_input(auth_client: TestClient) -> None:
    """Manual callback submission should reach the sidecar and keep polling in the UI."""

    async def unexpected_query(*args, **kwargs):
        if False:
            yield {}
        raise AssertionError("query should not be called")

    mock_sidecar = make_mock_sidecar(unexpected_query)
    mock_sidecar.get_oauth_flow_status = AsyncMock(
        return_value={
            "flow_id": "flow-openai-codex",
            "provider": "openai-codex",
            "status": "pending",
            "instructions": "Open the browser and sign in.",
            "progress": ["Waiting for OAuth callback..."],
            "manual_input_required": True,
            "manual_input_prompt": "Paste the final redirect URL or authorization code here.",
            "manual_input_submitted": False,
        }
    )
    mock_sidecar.submit_oauth_flow_input = AsyncMock(return_value={"type": "oauth_submitted"})

    with (
        patch(
            "yinshi.api.auth_routes.create_sidecar_connection", return_value=mock_sidecar
        ) as create_conn,
        patch(
            "yinshi.api.auth_routes.resolve_tenant_sidecar_context",
            new=AsyncMock(return_value=_tenant_sidecar_context()),
        ),
        patch("yinshi.api.auth_routes.protect_tenant_container") as protect_container,
        patch("yinshi.api.auth_routes.touch_tenant_container") as touch_container,
    ):
        response = auth_client.post(
            "/auth/providers/openai-codex/callback",
            json={
                "flow_id": "flow-openai-codex",
                "authorization_input": "http://localhost:1455/auth/callback?code=test-code&state=test-state",
            },
        )

    assert response.status_code == 202
    assert response.json() == {
        "status": "pending",
        "provider": "openai-codex",
        "flow_id": "flow-openai-codex",
        "instructions": "Open the browser and sign in.",
        "progress": ["Waiting for OAuth callback..."],
        "manual_input_required": True,
        "manual_input_prompt": "Paste the final redirect URL or authorization code here.",
        "manual_input_submitted": True,
        "error": None,
    }
    mock_sidecar.submit_oauth_flow_input.assert_awaited_once_with(
        "flow-openai-codex",
        "http://localhost:1455/auth/callback?code=test-code&state=test-state",
    )
    create_conn.assert_awaited_once_with("/tmp/tenant-oauth.sock")
    protect_container.assert_called_once()
    touch_container.assert_called_once()


def test_callback_provider_auth_persists_completed_oauth_connection(
    auth_client: TestClient,
) -> None:
    """Polling a completed flow should persist the OAuth connection and clear the flow."""

    async def unexpected_query(*args, **kwargs):
        if False:
            yield {}
        raise AssertionError("query should not be called")

    mock_sidecar = make_mock_sidecar(unexpected_query)
    mock_sidecar.get_oauth_flow_status = AsyncMock(
        return_value={
            "flow_id": "flow-openai-codex",
            "provider": "openai-codex",
            "status": "complete",
            "instructions": "Open the browser and sign in.",
            "progress": ["Exchanging authorization code for tokens..."],
            "manual_input_required": True,
            "manual_input_prompt": "Paste the final redirect URL or authorization code here.",
            "manual_input_submitted": True,
            "credentials": {
                "access": "oauth-access-token",
                "refresh": "oauth-refresh-token",
                "expires": 1_800_000_000_000,
                "accountId": "acct_123",
            },
        }
    )
    mock_sidecar.clear_oauth_flow = AsyncMock()

    with (
        patch(
            "yinshi.api.auth_routes.create_sidecar_connection", return_value=mock_sidecar
        ) as create_conn,
        patch(
            "yinshi.api.auth_routes.resolve_tenant_sidecar_context",
            new=AsyncMock(return_value=_tenant_sidecar_context()),
        ),
        patch("yinshi.api.auth_routes.release_tenant_container") as release_container,
        patch("yinshi.api.auth_routes.touch_tenant_container") as touch_container,
    ):
        response = auth_client.get(
            "/auth/providers/openai-codex/callback?flow_id=flow-openai-codex"
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "complete",
        "provider": "openai-codex",
        "flow_id": "flow-openai-codex",
        "instructions": "Open the browser and sign in.",
        "progress": ["Exchanging authorization code for tokens..."],
        "manual_input_required": True,
        "manual_input_prompt": "Paste the final redirect URL or authorization code here.",
        "manual_input_submitted": True,
        "error": None,
    }

    connections_response = auth_client.get("/api/settings/connections")
    assert connections_response.status_code == 200
    providers = [row["provider"] for row in connections_response.json()]
    assert "openai-codex" in providers
    mock_sidecar.clear_oauth_flow.assert_awaited_once_with("flow-openai-codex")
    create_conn.assert_awaited_once_with("/tmp/tenant-oauth.sock")
    release_container.assert_called_once()
    touch_container.assert_called_once()


def test_start_provider_auth_returns_503_when_sidecar_is_unavailable(
    auth_client: TestClient,
) -> None:
    """Provider auth start should hide transient tenant-sidecar failures behind 503."""
    from yinshi.exceptions import SidecarNotConnectedError

    with (
        patch(
            "yinshi.api.auth_routes.create_sidecar_connection",
            new=AsyncMock(side_effect=SidecarNotConnectedError("socket missing")),
        ),
        patch(
            "yinshi.api.auth_routes.resolve_tenant_sidecar_context",
            new=AsyncMock(return_value=_tenant_sidecar_context()),
        ),
        patch("yinshi.api.auth_routes.touch_tenant_container") as touch_container,
    ):
        response = auth_client.post("/auth/providers/openai-codex/start")

    assert response.status_code == 503
    assert response.json()["detail"] == "Agent environment temporarily unavailable"
    touch_container.assert_called_once()


def test_callback_provider_auth_returns_404_when_flow_is_missing(auth_client: TestClient) -> None:
    """Polling a missing OAuth flow should return a stable not-found response."""
    from yinshi.exceptions import SidecarError

    async def unexpected_query(*args, **kwargs):
        if False:
            yield {}
        raise AssertionError("query should not be called")

    mock_sidecar = make_mock_sidecar(unexpected_query)
    mock_sidecar.get_oauth_flow_status = AsyncMock(
        side_effect=SidecarError("OAuth status failed: OAuth flow not found")
    )

    with (
        patch(
            "yinshi.api.auth_routes.create_sidecar_connection", return_value=mock_sidecar
        ),
        patch(
            "yinshi.api.auth_routes.resolve_tenant_sidecar_context",
            new=AsyncMock(return_value=_tenant_sidecar_context()),
        ),
        patch("yinshi.api.auth_routes.touch_tenant_container") as touch_container,
    ):
        response = auth_client.get(
            "/auth/providers/openai-codex/callback?flow_id=flow-openai-codex"
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "OAuth flow not found"
    touch_container.assert_called_once()
