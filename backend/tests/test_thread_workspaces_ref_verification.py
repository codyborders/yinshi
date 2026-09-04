"""Git ref verification failures during child cleanup."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess

import pytest

from yinshi.exceptions import GitError
from yinshi.services import thread_workspaces
from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
SNAPSHOT_REF = f"refs/yinshi/snapshots/{DELEGATION_ID}"


def run_git(*args: str, cwd: str) -> str:
    """Run one setup Git command."""
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def seed_parent(db: sqlite3.Connection, repo_path: str) -> None:
    """Insert one repository and parent workspace."""
    branch = run_git("symbolic-ref", "--short", "HEAD", cwd=repo_path)
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'repo', ?)",
        (repo_path,),
    )
    db.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path, state)
           VALUES ('parent-ws', 'repo1', 'parent', ?, ?, 'ready')""",
        (branch, repo_path),
    )
    db.commit()


def test_cleanup_preserves_row_when_ref_verification_fails(db, git_repo, monkeypatch):
    """Cleanup cannot report success when Git cannot verify a failed ref deletion."""
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
    real_run_git = thread_workspaces._run_git

    async def failing_ref_commands(args, cwd=None, env=None):
        if args[:2] == ["update-ref", "-d"] and args[2] == SNAPSHOT_REF:
            raise GitError("ref deletion unavailable")
        if SNAPSHOT_REF in args and args[0] in {"rev-parse", "for-each-ref"}:
            raise GitError("ref verification unavailable")
        return await real_run_git(args, cwd=cwd, env=env)

    monkeypatch.setattr(thread_workspaces, "_run_git", failing_ref_commands)

    with pytest.raises(GitError):
        asyncio.run(
            service.discard_partial_child(
                db,
                None,
                delegation_id=DELEGATION_ID,
                workspace_id=provisioned.workspace_id,
            )
        )
    row_count = db.execute(
        "SELECT count(*) FROM workspaces WHERE id = ?",
        (provisioned.workspace_id,),
    ).fetchone()[0]
    assert row_count == 1
