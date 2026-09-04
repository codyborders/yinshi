"""Thread workspace provisioning, snapshot, and finalization tests."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from yinshi.exceptions import YinshiError
from yinshi.services.thread_workspaces import (
    ProvisionedChildWorkspace,
    ThreadWorkspaceService,
)

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
CHILD_BRANCH = "yinshi/thread-d4e5f6a7"


def run_git(*args: str, cwd: str, check: bool = True) -> str:
    """Run one setup git command for test arrangements."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def head_commit(repo_path: str) -> str:
    """Return the current HEAD commit of one repository."""
    return run_git("rev-parse", "HEAD", cwd=repo_path)


def seed_parent_stack(db: sqlite3.Connection, git_repo: str) -> None:
    """Insert one repo, parent workspace, and parent session."""
    branch = run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=git_repo)
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


def seed_delegation(
    db: sqlite3.Connection,
    delegation_id: str = DELEGATION_ID,
    status: str = "provisioning",
) -> None:
    """Insert one provisioning delegation under the seeded parent session."""
    db.execute(
        """INSERT INTO thread_delegations (
               id, parent_session_id, idempotency_key, initiator,
               title, task, requested_model, status
           ) VALUES (
               ?, 'parent-session', 'idem-1', 'user',
               'Child', 'task text', 'model-x', ?
           )""",
        (delegation_id, status),
    )
    db.commit()


def workspace_row(db: sqlite3.Connection, workspace_id: str) -> sqlite3.Row:
    """Load one workspace row."""
    row = db.execute(
        "SELECT * FROM workspaces WHERE id = ?",
        (workspace_id,),
    ).fetchone()
    assert row is not None
    return row


@pytest.fixture
def service() -> ThreadWorkspaceService:
    """Provide one stateless thread workspace service."""
    return ThreadWorkspaceService()


@pytest.fixture
def provisioned(db: sqlite3.Connection, git_repo: str, service):
    """Seed one clean parent stack and provision one child workspace."""
    seed_parent_stack(db, git_repo)
    seed_delegation(db)
    return asyncio.run(
        service.provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )


def test_provision_dirty_parent_creates_snapshot_base(db, git_repo, service):
    """A dirty parent provisions from an immutable hidden snapshot commit."""
    seed_parent_stack(db, git_repo)
    seed_delegation(db)
    parent_head = head_commit(git_repo)
    (Path(git_repo) / "README.md").write_text("# changed\n", encoding="utf-8")

    provisioned = asyncio.run(
        service.provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )

    assert provisioned.base_kind == "snapshot"
    assert provisioned.snapshot_ref == f"refs/yinshi/snapshots/{DELEGATION_ID}"
    assert provisioned.base_commit != parent_head
    snapshot_parent = run_git(
        "rev-parse",
        f"{provisioned.base_commit}^",
        cwd=git_repo,
    )
    assert snapshot_parent == parent_head
    ref_commit = run_git(
        "rev-parse",
        f"refs/yinshi/snapshots/{DELEGATION_ID}",
        cwd=git_repo,
    )
    assert ref_commit == provisioned.base_commit
    child_head = run_git("rev-parse", "HEAD", cwd=provisioned.path)
    assert child_head == provisioned.base_commit
    assert head_commit(git_repo) == parent_head


def test_snapshot_rejects_protected_secret_path(db, git_repo, service):
    """Dirty parents carrying unprotected .env files fail closed."""
    seed_parent_stack(db, git_repo)
    seed_delegation(db)
    (Path(git_repo) / ".env").write_text("SECRET=1\n", encoding="utf-8")

    with pytest.raises(YinshiError, match="protected secret path"):
        asyncio.run(
            service.provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id=DELEGATION_ID,
            )
        )

    assert (
        run_git(
            "for-each-ref",
            "--format=%(refname)",
            "refs/yinshi",
            cwd=git_repo,
        )
        == ""
    )
    assert head_commit(git_repo) == run_git(
        "rev-parse",
        "HEAD",
        cwd=git_repo,
    )
    assert db.execute("SELECT count(*) FROM workspaces WHERE kind = 'delegated'").fetchone()[0] == 0


def test_provision_clean_parent_uses_exact_head(db, git_repo, provisioned):
    """A clean parent provisions a child worktree from exact HEAD."""
    expected_head = head_commit(git_repo)
    assert isinstance(provisioned, ProvisionedChildWorkspace)
    assert provisioned.base_kind == "head"
    assert provisioned.base_commit == expected_head
    assert provisioned.snapshot_ref is None
    assert provisioned.branch == CHILD_BRANCH
    assert os.path.isdir(provisioned.path)
    child_head = run_git("rev-parse", "HEAD", cwd=provisioned.path)
    assert child_head == expected_head
