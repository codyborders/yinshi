"""Symlinked repository root rejection for child worktree creation."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from yinshi.exceptions import YinshiError
from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"


def run_git(*args: str, cwd: str) -> str:
    """Run one setup git command."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_provision_rejects_symlinked_repo_root(db, tmp_path):
    """A symlinked repository root fails before any child path is created."""
    real_repo = tmp_path / "real-repo"
    real_repo.mkdir()
    run_git("init", str(real_repo), cwd=str(tmp_path))
    run_git("config", "user.name", "T", cwd=str(real_repo))
    run_git("config", "user.email", "t@t", cwd=str(real_repo))
    Path(real_repo, "README.md").write_text("# t\n", encoding="utf-8")
    run_git("add", ".", cwd=str(real_repo))
    run_git(
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@t",
        "commit",
        "-m",
        "init",
        cwd=str(real_repo),
    )
    parent_path = str(Path(real_repo) / ".worktrees" / "parent-branch")
    run_git("worktree", "add", "-b", "parent-branch", parent_path, cwd=str(real_repo))

    link_root = tmp_path / "linked-repo"
    link_root.symlink_to(real_repo)

    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'repo', ?)",
        (str(link_root),),
    )
    db.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path, state)
           VALUES ('parent-ws', 'repo1', 'parent', 'parent-branch', ?, 'ready')""",
        (parent_path,),
    )
    db.execute(
        "INSERT INTO sessions (id, workspace_id) VALUES ('parent-session', 'parent-ws')",
    )
    db.commit()

    with pytest.raises(YinshiError, match="symlink"):
        asyncio.run(
            ThreadWorkspaceService().provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id=DELEGATION_ID,
            )
        )

    # No child path was created anywhere under the real repository.
    assert not Path(real_repo, ".worktrees", "yinshi").exists()
    assert run_git("for-each-ref", "--format=%(refname)", "refs/yinshi", cwd=str(real_repo)) == ""
    assert (
        db.execute(
            "SELECT count(*) FROM workspaces WHERE kind = 'delegated'",
        ).fetchone()[0]
        == 0
    )
