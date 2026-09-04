"""Unusual filename preservation for snapshots and changed files."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

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


def test_non_utf8_paths_survive_snapshot_and_finalization(db, git_repo):
    """Non-UTF-8 filenames round-trip without decoding failures."""
    parent_path = seed_parent(db, git_repo)
    raw_name = os.fsdecode(b"bad\xffname.txt")
    raw_path = Path(parent_path) / raw_name
    try:
        raw_path.write_bytes(b"data\n")
    except OSError:
        pytest.skip("platform rejects non-UTF-8 filenames")
    run_git("add", "--", raw_name, cwd=parent_path)
    run_git(
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@t",
        "commit",
        "-m",
        "raw",
        cwd=parent_path,
    )
    base_commit = run_git("rev-parse", "HEAD", cwd=parent_path)

    provisioned = asyncio.run(
        ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )
    assert os.fsdecode(b"bad\xffname.txt") in os.listdir(provisioned.path)

    child = Path(provisioned.path)
    (child / raw_name).write_bytes(b"changed\n")

    finalized = asyncio.run(
        ThreadWorkspaceService().finalize_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=provisioned.workspace_id,
            base_commit=base_commit,
        )
    )

    encoded_paths = {os.fsencode(change.path) for change in finalized.changed_files}
    assert b"bad\xffname.txt" in encoded_paths


def test_strip_corruption_does_not_false_reject_space_env_names(db, git_repo):
    """A tracked ' .env' name is not a protected path and must snapshot."""
    parent_path = seed_parent(db, git_repo)
    root = Path(parent_path)
    odd_secret = root / " .env"
    odd_secret.write_text("not-a-real-secret\n", encoding="utf-8")
    run_git("add", "--", " .env", cwd=parent_path)
    run_git(
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@t",
        "commit",
        "-m",
        "odd name",
        cwd=parent_path,
    )
    # Dirty the parent with a plain tracked change.
    (root / "README.md").write_text("changed\n", encoding="utf-8")

    provisioned = asyncio.run(
        ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )

    assert provisioned.base_kind == "snapshot"
    assert Path(provisioned.path, " .env").exists()
