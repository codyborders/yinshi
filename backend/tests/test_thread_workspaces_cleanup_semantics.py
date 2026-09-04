"""Cleanup semantics for stored paths, refs, and failure reporting."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest

from yinshi.exceptions import GitError
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


def test_discard_uses_stored_workspace_path(db, git_repo):
    """Discard removes the worktree recorded in the row, not a derived path."""
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
    moved = Path(git_repo) / ".worktrees" / "moved-child"
    Path(provisioned.path).rename(moved)
    db.execute(
        "UPDATE workspaces SET path = ? WHERE id = ?",
        (str(moved), provisioned.workspace_id),
    )
    db.commit()

    asyncio.run(
        service.discard_partial_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=provisioned.workspace_id,
        )
    )

    assert not moved.exists()
    assert (
        db.execute(
            "SELECT count(*) FROM workspaces WHERE kind = 'delegated'",
        ).fetchone()[0]
        == 0
    )
    assert Path(parent_path).exists()


def test_discard_removes_snapshot_and_result_refs(db, git_repo):
    """Discard deletes both hidden refs left by provision and finalize."""
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
    base_commit = run_git("rev-parse", "HEAD", cwd=parent_path)
    asyncio.run(
        service.finalize_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=provisioned.workspace_id,
            base_commit=base_commit,
        )
    )
    result_ref = f"refs/yinshi/results/{DELEGATION_ID}"
    assert run_git("rev-parse", "--verify", result_ref, cwd=git_repo) != ""

    asyncio.run(
        service.discard_partial_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=provisioned.workspace_id,
        )
    )

    refs = run_git("for-each-ref", "--format=%(refname)", "refs/yinshi", cwd=git_repo)
    assert refs == ""


def test_discard_reports_git_failure_and_keeps_row(db, git_repo):
    """A Git cleanup failure keeps the row and raises a reporting error."""
    import shutil

    seed_parent(db, git_repo)
    service = ThreadWorkspaceService()
    provisioned = asyncio.run(
        service.provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )
    # Corrupt the worktree and lock one subdirectory so removal fails.
    child = Path(provisioned.path)
    for item in list(child.iterdir()):
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    locked = child / "locked"
    locked.mkdir()
    (locked / "keep.txt").write_text("keep\n", encoding="utf-8")
    locked.chmod(0o500)

    try:
        with pytest.raises(GitError, match="cleanup failed"):
            asyncio.run(
                service.discard_partial_child(
                    db,
                    None,
                    delegation_id=DELEGATION_ID,
                    workspace_id=provisioned.workspace_id,
                )
            )
    finally:
        locked.chmod(0o700)

    assert (
        db.execute(
            "SELECT count(*) FROM workspaces WHERE id = ?",
            (provisioned.workspace_id,),
        ).fetchone()[0]
        == 1
    )
