"""Secret guardrail installation on created child worktrees."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest

from yinshi.services import thread_workspaces
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


def seed_parent(db: sqlite3.Connection, git_repo: str) -> str:
    """Insert one repo plus a clean parent workspace worktree."""
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


def test_guardrail_failure_cleans_only_this_attempt(db, git_repo, monkeypatch):
    """A guardrail failure removes this attempt's artifacts and nothing else."""
    parent_path = seed_parent(db, git_repo)

    def failing_guardrails(repo_root_path: str) -> None:
        del repo_root_path
        raise RuntimeError("injected guardrail failure")

    monkeypatch.setattr(
        thread_workspaces,
        "ensure_secret_guardrails",
        failing_guardrails,
    )

    with pytest.raises(RuntimeError, match="injected guardrail failure"):
        asyncio.run(
            ThreadWorkspaceService().provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id=DELEGATION_ID,
            )
        )

    monkeypatch.undo()
    assert (
        db.execute(
            "SELECT count(*) FROM workspaces WHERE kind = 'delegated'",
        ).fetchone()[0]
        == 0
    )
    assert run_git("branch", "--list", "yinshi/thread-d4e5f6a7", cwd=git_repo) == ""
    assert not Path(git_repo, ".worktrees", "yinshi", "thread-d4e5f6a7").exists()
    # Pre-existing parent workspace stays untouched.
    assert Path(parent_path).exists()
