"""Injected Git failures during snapshot ref updates and worktree creation."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest

from yinshi.exceptions import GitError
from yinshi.services import thread_workspaces

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
SNAPSHOT_REF = f"refs/yinshi/snapshots/{DELEGATION_ID}"
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


def test_snapshot_ref_update_failure_cleans_owned_artifacts(db, git_repo, monkeypatch):
    """A failed snapshot ref update removes the worktree and branch."""
    parent_path = seed_parent(db, git_repo)
    real_run_git = thread_workspaces._run_git

    async def failing_ref_update(args, **kwargs):
        if args[:1] == ["update-ref"] and SNAPSHOT_REF in args:
            raise GitError("injected update-ref failure")
        return await real_run_git(args, **kwargs)

    monkeypatch.setattr(thread_workspaces, "_run_git", failing_ref_update)

    with pytest.raises(GitError, match="injected update-ref failure"):
        asyncio.run(
            thread_workspaces.ThreadWorkspaceService().provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id=DELEGATION_ID,
            )
        )

    monkeypatch.undo()
    assert not Path(git_repo, ".worktrees", CHILD_BRANCH).exists()
    assert run_git("branch", "--list", CHILD_BRANCH, cwd=git_repo) == ""
    assert run_git("for-each-ref", "--format=%(refname)", "refs/yinshi", cwd=git_repo) == ""
    assert (
        db.execute(
            "SELECT count(*) FROM workspaces WHERE kind = 'delegated'",
        ).fetchone()[0]
        == 0
    )
    assert "dirty" in Path(parent_path, "README.md").read_text(encoding="utf-8")


def test_worktree_creation_failure_cleans_owned_branch(db, git_repo, monkeypatch):
    """A worktree creation failure leaves no branch, ref, or row behind."""
    seed_parent(db, git_repo)

    async def failing_create_worktree(*args, **kwargs):
        # Simulate git failing after the branch was created.
        repo_path, branch = args[0], args[2]
        subprocess.run(
            ["git", "branch", branch, "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        raise GitError("injected worktree creation failure")

    monkeypatch.setattr(thread_workspaces, "create_worktree", failing_create_worktree)

    with pytest.raises(GitError, match="injected worktree creation failure"):
        asyncio.run(
            thread_workspaces.ThreadWorkspaceService().provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id=DELEGATION_ID,
            )
        )

    monkeypatch.undo()
    assert run_git("branch", "--list", CHILD_BRANCH, cwd=git_repo) == ""
    assert run_git("for-each-ref", "--format=%(refname)", "refs/yinshi", cwd=git_repo) == ""
    assert (
        db.execute(
            "SELECT count(*) FROM workspaces WHERE kind = 'delegated'",
        ).fetchone()[0]
        == 0
    )
