"""Tracked symlink preservation for thread workspace snapshots."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
from pathlib import Path

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
    """Insert one repo plus a parent workspace backed by a real worktree."""
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


def test_snapshot_preserves_tracked_symlinks(db, git_repo, tmp_path):
    """Snapshots store symlinks as Git links and never follow their targets."""
    parent_path = seed_parent(db, git_repo)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside v1\n", encoding="utf-8")
    parent = Path(parent_path)
    link = parent / "config-link"
    link.symlink_to(outside)
    run_git("add", "config-link", cwd=parent_path)
    run_git(
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@t",
        "commit",
        "-m",
        "link",
        cwd=parent_path,
    )
    # Dirty the parent by retargeting the tracked symlink.
    other = tmp_path / "other.txt"
    other.write_text("other\n", encoding="utf-8")
    link.unlink()
    link.symlink_to(other)

    provisioned = asyncio.run(
        ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )

    child_link = Path(provisioned.path) / "config-link"
    assert child_link.is_symlink()
    assert os.readlink(child_link) == str(other)
    # The snapshot tree stores the link itself (mode 120000), never the
    # target's contents, so the snapshot tracks one symlink blob.
    tree_line = run_git(
        "ls-tree",
        "-r",
        provisioned.base_commit,
        "--",
        "config-link",
        cwd=git_repo,
    )
    assert "120000" in tree_line
    assert "blob" in tree_line
    outside_hash = run_git("hash-object", str(outside), cwd=git_repo)
    assert outside_hash not in tree_line
