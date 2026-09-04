"""Git guardrail behavior for delegated child worktrees."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"


def run_git(*args: str, cwd: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one setup or assertion Git command."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def seed_parent(db: sqlite3.Connection, repo_path: str) -> None:
    """Insert one repository and parent workspace."""
    branch = run_git("symbolic-ref", "--short", "HEAD", cwd=repo_path).stdout.strip()
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


def test_delegated_child_inherits_common_secret_guardrails(db, git_repo):
    """A created child ignores protected environment files through common Git metadata."""
    seed_parent(db, git_repo)

    provisioned = asyncio.run(
        ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )
    environment_path = Path(provisioned.path) / ".env"
    environment_path.write_text("TOKEN=private\n", encoding="utf-8")

    ignored = run_git(
        "check-ignore",
        "--quiet",
        ".env",
        cwd=provisioned.path,
        check=False,
    )
    assert ignored.returncode == 0
