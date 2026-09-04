"""Cleanup guarantees for failed thread workspace provisioning."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest

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


def test_provision_failure_removes_partial_artifacts(db, git_repo):
    """A database failure after Git work cleans every created artifact."""
    seed_parent(db, git_repo)
    (Path(git_repo) / "README.md").write_text("# dirty\n", encoding="utf-8")
    db.execute(
        """CREATE TRIGGER fail_delegated_workspace_insert
           BEFORE INSERT ON workspaces
           WHEN NEW.kind = 'delegated'
           BEGIN
               SELECT RAISE(ABORT, 'simulated workspace insert failure');
           END""",
    )
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="simulated"):
        asyncio.run(
            ThreadWorkspaceService().provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id=DELEGATION_ID,
            )
        )

    delegated_rows = db.execute(
        "SELECT count(*) FROM workspaces WHERE kind = 'delegated'",
    ).fetchone()[0]
    assert delegated_rows == 0
    refs = run_git(
        "for-each-ref",
        "--format=%(refname)",
        "refs/yinshi",
        cwd=git_repo,
    )
    assert refs == ""
    branches = run_git("branch", "--list", CHILD_BRANCH, cwd=git_repo)
    assert branches == ""
    assert not Path(git_repo, ".worktrees", CHILD_BRANCH).exists()
    # Parent state still holds the original dirty working file.
    parent_status = run_git("status", "--porcelain", cwd=git_repo)
    assert "M README.md" in parent_status


def test_discard_partial_child_is_idempotent(db, git_repo):
    """Repeated discard calls remove artifacts once, then do nothing."""
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
    assert Path(provisioned.path).exists()

    asyncio.run(
        service.discard_partial_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=provisioned.workspace_id,
        )
    )
    # A second call must stay silent and successful.
    asyncio.run(
        service.discard_partial_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=provisioned.workspace_id,
        )
    )

    assert not Path(provisioned.path).exists()
    assert run_git("branch", "--list", CHILD_BRANCH, cwd=git_repo) == ""
    assert run_git("for-each-ref", "--format=%(refname)", "refs/yinshi", cwd=git_repo) == ""
    assert (
        db.execute(
            "SELECT count(*) FROM workspaces WHERE kind = 'delegated'",
        ).fetchone()[0]
        == 0
    )
    # The parent workspace row must survive cleanup.
    assert (
        db.execute(
            "SELECT count(*) FROM workspaces WHERE id = 'parent-ws'",
        ).fetchone()[0]
        == 1
    )
