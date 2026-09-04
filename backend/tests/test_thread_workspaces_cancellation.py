"""Cancellation safety for thread workspace provisioning."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

from yinshi.services import thread_workspaces
from yinshi.services.git import create_worktree as real_create_worktree
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


def test_cancellation_removes_partial_artifacts(db, git_repo, monkeypatch):
    """Cancelling mid-provisioning cleans every artifact and propagates."""
    seed_parent(db, git_repo)
    (Path(git_repo) / "README.md").write_text("# dirty\n", encoding="utf-8")

    async def cancelling_create_worktree(*args, **kwargs):
        """Create the real worktree, then simulate caller cancellation."""
        await real_create_worktree(*args, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        thread_workspaces,
        "create_worktree",
        cancelling_create_worktree,
    )

    async def run_provision():
        await ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )

    async def scenario():
        task = asyncio.ensure_future(run_provision())
        try:
            await task
        except asyncio.CancelledError:
            return task
        raise AssertionError("provisioning was not cancelled")

    task = asyncio.run(scenario())
    assert task.cancelled()

    monkeypatch.undo()
    delegated_rows = db.execute(
        "SELECT count(*) FROM workspaces WHERE kind = 'delegated'",
    ).fetchone()[0]
    assert delegated_rows == 0
    assert run_git("for-each-ref", "--format=%(refname)", "refs/yinshi", cwd=git_repo) == ""
    assert run_git("branch", "--list", CHILD_BRANCH, cwd=git_repo) == ""
    assert not Path(git_repo, ".worktrees", CHILD_BRANCH).exists()
    assert "M README.md" in run_git("status", "--porcelain", cwd=git_repo)
