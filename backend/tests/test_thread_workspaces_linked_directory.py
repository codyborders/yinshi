"""Child worktree directory path validation."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from yinshi.exceptions import YinshiError
from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"


def run_git(*args: str, cwd: str) -> str:
    """Run one setup Git command."""
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def test_provision_rejects_linked_worktree_directory(db, tmp_path):
    """Provisioning cannot create child paths through a linked directory."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    run_git("init", str(repo_path), cwd=str(tmp_path))
    (repo_path / "README.md").write_text("# test\n", encoding="utf-8")
    run_git("add", ".", cwd=str(repo_path))
    run_git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "initial",
        cwd=str(repo_path),
    )
    external_path = tmp_path / "external"
    external_path.mkdir()
    (repo_path / ".worktrees").symlink_to(external_path, target_is_directory=True)

    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'repo', ?)",
        (str(repo_path),),
    )
    db.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path, state)
           VALUES ('parent-ws', 'repo1', 'parent', 'main', ?, 'ready')""",
        (str(repo_path),),
    )
    db.commit()

    with pytest.raises(YinshiError, match="worktree directory"):
        asyncio.run(
            ThreadWorkspaceService().provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id=DELEGATION_ID,
            )
        )

    assert list(external_path.iterdir()) == []
