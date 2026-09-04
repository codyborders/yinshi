"""Discard idempotency when artifacts are already absent."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
CHILD_BRANCH = "yinshi/thread-d4e5f6a7"


def run_git(*args: str, cwd: str, check: bool = True) -> str:
    """Run one setup git command."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def seed_parent(db: sqlite3.Connection, git_repo: str) -> str:
    """Insert one repo plus a parent workspace worktree."""
    run_git("config", "user.name", "T", cwd=git_repo)
    run_git("config", "user.email", "t@t", cwd=git_repo)
    parent_path = str(Path(git_repo) / ".worktrees" / "parent-branch")
    run_git("worktree", "add", "-b", "parent-branch", parent_path, cwd=git_repo)
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'repo', ?)",
        (git_repo,),
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
    return parent_path


def test_discard_absent_artifacts_with_repo_id_is_idempotent(db, git_repo):
    """Discard with repo_id succeeds when branch and refs are already gone."""
    parent_path = seed_parent(db, git_repo)
    service = ThreadWorkspaceService()
    provisioned = asyncio.run(
        service.provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )
    # Simulate another actor removing the Git artifacts only.
    run_git("worktree", "remove", "--force", provisioned.path, cwd=git_repo)
    run_git("branch", "-D", CHILD_BRANCH, cwd=git_repo)
    db.execute(
        "DELETE FROM workspaces WHERE id = ?",
        (provisioned.workspace_id,),
    )
    db.commit()

    # No workspace row is known; repo_id scopes the Git-side absence check.
    asyncio.run(
        service.discard_partial_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=None,
            repo_id="repo1",
        )
    )
    # A second call must stay silent.
    asyncio.run(
        service.discard_partial_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=None,
            repo_id="repo1",
        )
    )

    assert run_git("branch", "--list", CHILD_BRANCH, cwd=git_repo) == ""
    assert run_git("for-each-ref", "--format=%(refname)", "refs/yinshi", cwd=git_repo) == ""
    assert Path(parent_path).exists()
