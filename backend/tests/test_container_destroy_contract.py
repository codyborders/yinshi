"""Behavior tests for workspace container destruction contracts."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.test_api import create_full_stack


def test_delete_workspace_returns_conflict_for_non_prompt_busy_runtime(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """A busy runtime should preserve workspace state without an active prompt."""
    from yinshi.main import app

    stack = create_full_stack(auth_client, git_repo, name="non-prompt-busy-delete")
    workspace_id = stack["workspace"]["id"]
    workspace_path = Path(stack["workspace"]["path"])
    container_manager = AsyncMock()
    container_manager.destroy_container.return_value = False
    coordinator = AsyncMock()
    coordinator.request_cancel.return_value = False
    app.state.container_manager = container_manager

    with patch("yinshi.api.workspaces.get_run_coordinator", return_value=coordinator):
        response = auth_client.delete(f"/api/workspaces/{workspace_id}")

    assert response.status_code == 409
    assert response.json() == {"detail": "Workspace is still stopping; deletion can be retried"}
    assert workspace_path.exists()
    assert auth_client.get(f"/api/workspaces/{workspace_id}/sessions").status_code == 200
