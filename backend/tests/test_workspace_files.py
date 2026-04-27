"""Workspace file APIs hide secrets, expose Git status, and guard path access."""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient


def _create_workspace(client: TestClient, git_repo: str) -> dict[str, str]:
    """Create a repo and workspace through the public API."""
    repo = client.post(
        "/api/repos",
        json={"name": "demo", "local_path": git_repo},
    ).json()
    workspace = client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json()
    return workspace


def test_workspace_file_tree_hides_env_and_dependency_dirs(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """File tree should show source files while hiding secrets and noisy directories."""
    workspace = _create_workspace(noauth_client, git_repo)
    workspace_path = Path(workspace["path"])
    (workspace_path / "src").mkdir()
    (workspace_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (workspace_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (workspace_path / "node_modules").mkdir()
    (workspace_path / "node_modules" / "package.js").write_text("bad\n", encoding="utf-8")

    response = noauth_client.get(f"/api/workspaces/{workspace['id']}/files/tree")

    assert response.status_code == 200
    payload = response.json()
    serialized = repr(payload)
    assert "app.py" in serialized
    assert ".env" not in serialized
    assert "node_modules" not in serialized


def test_workspace_changed_files_clear_after_commit(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """Changed files endpoint should reflect current worktree Git status."""
    workspace = _create_workspace(noauth_client, git_repo)
    workspace_path = Path(workspace["path"])
    readme_path = workspace_path / "README.md"
    readme_path.write_text("# Test\n\nChanged\n", encoding="utf-8")

    changed_response = noauth_client.get(f"/api/workspaces/{workspace['id']}/files/changed")
    assert changed_response.status_code == 200
    assert changed_response.json()["files"] == [
        {
            "path": "README.md",
            "status": " M",
            "kind": "modified",
            "original_path": None,
        }
    ]

    subprocess.run(["git", "add", "README.md"], cwd=workspace_path, check=True)
    subprocess.run(["git", "commit", "-m", "update readme"], cwd=workspace_path, check=True)

    cleared_response = noauth_client.get(f"/api/workspaces/{workspace['id']}/files/changed")
    assert cleared_response.status_code == 200
    assert cleared_response.json()["files"] == []


def test_workspace_file_preview_rejects_env_and_path_traversal(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """Preview endpoint should reject secret files and paths outside the worktree."""
    workspace = _create_workspace(noauth_client, git_repo)
    workspace_path = Path(workspace["path"])
    (workspace_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    env_response = noauth_client.get(
        f"/api/workspaces/{workspace['id']}/files/preview",
        params={"path": ".env"},
    )
    traversal_response = noauth_client.get(
        f"/api/workspaces/{workspace['id']}/files/preview",
        params={"path": "../README.md"},
    )

    assert env_response.status_code == 403
    assert traversal_response.status_code == 400


def test_workspace_creation_installs_env_git_guardrails(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """Workspace creation should add repo-local Git excludes and commit hook for env files."""
    workspace = _create_workspace(noauth_client, git_repo)
    workspace_path = Path(workspace["path"])
    repo_path = Path(git_repo)
    exclude_text = (repo_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    hook_path = repo_path / ".git" / "hooks" / "pre-commit"
    hook_text = hook_path.read_text(encoding="utf-8")

    assert ".env" in exclude_text
    assert ".env.*" in exclude_text
    assert "Yinshi secret commit guard" in hook_text

    (workspace_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".env"], cwd=workspace_path, check=True)
    commit = subprocess.run(
        ["git", "commit", "-m", "try env"],
        cwd=workspace_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert commit.returncode != 0
    assert "Yinshi blocks committing .env files" in commit.stderr
