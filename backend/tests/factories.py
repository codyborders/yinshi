"""Shared test helpers for end-to-end style backend scenarios."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


def _request_kwargs(headers: Mapping[str, str] | None) -> dict[str, Mapping[str, str]]:
    """Build request kwargs only when custom headers are provided."""
    return {"headers": headers} if headers else {}


def create_repo(
    client: TestClient,
    git_repo: str,
    headers: Mapping[str, str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Import a local git repo through the real API."""
    payload: dict[str, Any] = {
        "name": Path(git_repo).name,
        "local_path": git_repo,
    }
    payload.update(overrides)
    response = client.post("/api/repos", json=payload, **_request_kwargs(headers))
    assert response.status_code == 201, response.text
    return response.json()


def create_workspace(
    client: TestClient,
    repo_id: str,
    headers: Mapping[str, str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Create a workspace through the real API."""
    response = client.post(
        f"/api/repos/{repo_id}/workspaces",
        json=overrides,
        **_request_kwargs(headers),
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_session(
    client: TestClient,
    workspace_id: str,
    headers: Mapping[str, str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Create a session through the real API."""
    response = client.post(
        f"/api/workspaces/{workspace_id}/sessions",
        json=overrides,
        **_request_kwargs(headers),
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_full_stack(
    client: TestClient,
    git_repo: str,
    headers: Mapping[str, str] | None = None,
    **repo_overrides: Any,
) -> dict[str, dict[str, Any]]:
    """Create a repo, workspace, and session for journey-style tests."""
    repo = create_repo(client, git_repo, headers=headers, **repo_overrides)
    workspace = create_workspace(client, repo["id"], headers=headers)
    session = create_session(client, workspace["id"], headers=headers)
    return {
        "repo": repo,
        "workspace": workspace,
        "session": session,
    }


def make_mock_sidecar(query_fn: Callable[..., Any], **overrides: Any) -> AsyncMock:
    """Build a mock sidecar client for prompt-stream tests."""
    mock = AsyncMock()

    def query_wrapper(*args: Any, **kwargs: Any) -> Any:
        """Adapt modern sidecar kwargs to older test helper signatures."""
        parameters = inspect.signature(query_fn).parameters
        supported_kwargs = {
            name: value
            for name, value in kwargs.items()
            if name in parameters
        }
        if "api_key" in parameters and "api_key" not in supported_kwargs:
            provider_auth = kwargs.get("provider_auth")
            if isinstance(provider_auth, dict):
                secret = provider_auth.get("secret")
                if isinstance(secret, str):
                    supported_kwargs["api_key"] = secret
        return query_fn(*args, **supported_kwargs)

    mock.query = query_wrapper
    mock.resolve_model = AsyncMock(
        return_value={"provider": "minimax", "model": "minimax/MiniMax-M2.7"},
    )

    async def resolve_provider_auth(
        *,
        provider: str,
        model: str,
        provider_auth: dict[str, object],
        provider_config: dict[str, object] | None = None,
        agent_dir: str | None = None,
    ) -> dict[str, object]:
        """Mirror auth resolution without mutating the stored secret shape."""
        del agent_dir
        runtime_api_key: str | None = None
        secret = provider_auth.get("secret")
        auth_strategy = provider_auth.get("authStrategy")
        if auth_strategy in {"api_key", "api_key_with_config"}:
            if isinstance(secret, str):
                runtime_api_key = secret
            elif isinstance(secret, dict):
                api_key = secret.get("apiKey")
                if isinstance(api_key, str):
                    runtime_api_key = api_key
        else:
            if isinstance(secret, dict):
                access_token = secret.get("accessToken")
                if isinstance(access_token, str):
                    runtime_api_key = access_token
        return {
            "provider": provider,
            "auth": secret,
            "model_ref": model,
            "runtime_api_key": runtime_api_key,
            "model_config": provider_config,
        }

    mock.resolve_provider_auth = AsyncMock(side_effect=resolve_provider_auth)
    mock.get_catalog = AsyncMock(
        return_value={
            "default_model": "minimax/MiniMax-M2.7",
            "providers": [{"id": "minimax", "model_count": 1}],
            "models": [
                {
                    "ref": "minimax/MiniMax-M2.7",
                    "provider": "minimax",
                    "id": "MiniMax-M2.7",
                    "label": "MiniMax M2.7",
                    "api": "openai-completions",
                    "reasoning": False,
                    "inputs": ["text"],
                    "context_window": 200000,
                    "max_tokens": 16384,
                }
            ],
        }
    )
    mock.start_oauth_flow = AsyncMock()
    mock.get_oauth_flow_status = AsyncMock()
    mock.submit_oauth_flow_input = AsyncMock()
    mock.clear_oauth_flow = AsyncMock()
    mock.warmup = AsyncMock()
    mock.disconnect = AsyncMock()
    for key, value in overrides.items():
        setattr(mock, key, value)
    return mock


def parse_sse_events(response_text: str) -> list[dict[str, Any]]:
    """Parse an SSE response body into JSON event payloads."""
    events: list[dict[str, Any]] = []
    for line in response_text.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("data: "):
            events.append(json.loads(stripped[6:]))
    return events
