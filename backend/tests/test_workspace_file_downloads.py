"""Workspace download tests exercise stable file-descriptor handling."""

from pathlib import Path

from fastapi.testclient import TestClient


def _create_workspace(client: TestClient, git_repo: str) -> dict[str, str]:
    """Create a repository and worktree through the public API."""
    repo = client.post(
        "/api/repos",
        json={"name": "download-demo", "local_path": git_repo},
    ).json()
    return client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json()


def test_workspace_file_download_rejects_symlinked_parent(
    noauth_client: TestClient,
    git_repo: str,
    tmp_path: Path,
) -> None:
    """Download should never follow a parent symlink outside the worktree."""
    workspace = _create_workspace(noauth_client, git_repo)
    workspace_path = Path(workspace["path"])
    outside_directory = tmp_path / "download-outside"
    outside_directory.mkdir()
    (outside_directory / "secret.txt").write_text("outside-secret", encoding="utf-8")
    (workspace_path / "linked").symlink_to(outside_directory, target_is_directory=True)

    response = noauth_client.get(
        f"/api/workspaces/{workspace['id']}/files/download",
        params={"path": "linked/secret.txt"},
    )

    assert response.status_code == 403
    assert "outside-secret" not in response.text
