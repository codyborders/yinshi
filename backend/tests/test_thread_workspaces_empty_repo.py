"""Unborn empty repository support for thread workspaces."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"


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


def seed_unborn_parent(db: sqlite3.Connection, git_repo: str) -> None:
    """Insert one repo and parent workspace over an unborn repository."""
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'repo', ?)",
        (git_repo,),
    )
    db.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path, state)
           VALUES ('parent-ws', 'repo1', 'parent', 'main', ?, 'ready')""",
        (git_repo,),
    )
    db.execute(
        "INSERT INTO sessions (id, workspace_id) VALUES ('parent-session', 'parent-ws')",
    )
    db.commit()


def test_provision_clean_empty_repository(db, tmp_path):
    """An unborn clean parent provisions through the empty-root behavior."""
    git_repo = str(tmp_path / "empty-repo")
    Path(git_repo).mkdir()
    run_git("init", git_repo, cwd=str(tmp_path))
    seed_unborn_parent(db, git_repo)

    provisioned = asyncio.run(
        ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )

    assert provisioned.base_kind == "head"
    assert provisioned.base_commit != ""
    child_head = run_git("rev-parse", "HEAD", cwd=provisioned.path)
    assert child_head == provisioned.base_commit
    assert (
        run_git("rev-parse", "--verify", f"{provisioned.base_commit}^", cwd=git_repo, check=False)
        == ""
    )
    assert not Path(provisioned.path, "README.md").exists()


def test_provision_dirty_empty_repository(db, tmp_path):
    """A dirty unborn parent snapshots untracked files without a parent commit."""
    git_repo = str(tmp_path / "dirty-empty-repo")
    Path(git_repo).mkdir()
    run_git("init", git_repo, cwd=str(tmp_path))
    seed_unborn_parent(db, git_repo)
    (Path(git_repo) / "seed.txt").write_text("seed\n", encoding="utf-8")

    provisioned = asyncio.run(
        ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )

    assert provisioned.base_kind == "snapshot"
    assert provisioned.base_commit != ""
    assert (
        run_git("rev-parse", "--verify", f"{provisioned.base_commit}^", cwd=git_repo, check=False)
        == ""
    )
    assert Path(provisioned.path, "seed.txt").read_text(encoding="utf-8") == "seed\n"
