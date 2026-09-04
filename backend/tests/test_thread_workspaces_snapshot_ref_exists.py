"""Pre-existing snapshot ref rejection for provisioning."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest

from yinshi.exceptions import YinshiError
from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
SNAPSHOT_REF = f"refs/yinshi/snapshots/{DELEGATION_ID}"


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
    """Insert one repo plus a dirty parent workspace worktree."""
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
    (Path(parent_path) / "README.md").write_text("dirty\n", encoding="utf-8")
    return parent_path


def test_provision_rejects_pre_existing_snapshot_ref(db, git_repo):
    """A pre-existing snapshot ref is never overwritten and survives failure."""
    seed_parent(db, git_repo)
    keep_commit = run_git("rev-parse", "HEAD", cwd=git_repo)
    run_git("update-ref", SNAPSHOT_REF, keep_commit, cwd=git_repo)

    with pytest.raises(YinshiError, match="snapshot ref already exists"):
        asyncio.run(
            ThreadWorkspaceService().provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id=DELEGATION_ID,
            )
        )

    assert run_git("rev-parse", "--verify", SNAPSHOT_REF, cwd=git_repo) == keep_commit
    assert (
        db.execute(
            "SELECT count(*) FROM workspaces WHERE kind = 'delegated'",
        ).fetchone()[0]
        == 0
    )
    assert run_git("branch", "--list", "yinshi/thread-d4e5f6a7", cwd=git_repo) == ""
