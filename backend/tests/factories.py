"""Shared test helpers for end-to-end style backend scenarios."""

from __future__ import annotations

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
    mock.query = query_fn
    mock.resolve_model = AsyncMock(return_value={"provider": "minimax", "model": "minimax"})
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
