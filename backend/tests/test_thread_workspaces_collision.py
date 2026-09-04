"""Branch collision rejection for thread workspace provisioning."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess

from yinshi.exceptions import YinshiError
from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
CHILD_BRANCH = "yinshi/thread-d4e5f6a7"


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


def seed_parent(db: sqlite3.Connection, git_repo: str) -> None:
    """Insert one repo, parent workspace, and parent session."""
    branch = run_git("symbolic-ref", "--short", "HEAD", cwd=git_repo)
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'repo', ?)",
        (git_repo,),
    )
    db.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path, state)
           VALUES ('parent-ws', 'repo1', 'parent', ?, ?, 'ready')""",
        (branch, git_repo),
    )
    db.execute(
        "INSERT INTO sessions (id, workspace_id) VALUES ('parent-session', 'parent-ws')",
    )
    db.commit()


def test_provision_rejects_existing_child_branch(db, git_repo):
    """A pre-existing generated branch fails provisioning without side effects."""
    seed_parent(db, git_repo)
    run_git("branch", CHILD_BRANCH, cwd=git_repo)

    with pytest_raises_collision():
        asyncio.run(
            ThreadWorkspaceService().provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id=DELEGATION_ID,
            )
        )

    delegated = db.execute(
        "SELECT count(*) FROM workspaces WHERE kind = 'delegated'",
    ).fetchone()[0]
    assert delegated == 0
    worktrees = run_git("worktree", "list", "--porcelain", cwd=git_repo)
    assert "worktree" in worktrees  # only the main checkout remains
    assert worktrees.count("worktree ") == 1


def pytest_raises_collision():
    """Return a matcher for the collision error."""
    import pytest

    return pytest.raises(YinshiError, match="already exists")
