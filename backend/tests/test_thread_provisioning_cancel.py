"""Provisioning-cancellation tests for the thread orchestration service."""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path

from tests.test_thread_orchestration import _orchestration_request
from tests.test_thread_workspaces import seed_parent_stack
from yinshi.models import ThreadChildCreate
from yinshi.services.thread_orchestration import ThreadOrchestrationService


def _spawn_queued_child(service, request, title: str):
    """Spawn one queued child through the orchestration service."""
    return asyncio.run(
        service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title=title,
                task="Wait for orchestration.",
                start_immediately=False,
            ),
        )
    )


def _force_pre_attach(db, spawned) -> str:
    """Rewind one queued delegation to the pre-attach provisioning window."""
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    workspace = db.execute(
        "SELECT * FROM workspaces WHERE id = ?",
        (delegation["child_workspace_id"],),
    ).fetchone()
    worktree_path = str(workspace["path"])
    db.execute(
        """UPDATE thread_delegations
           SET status = 'provisioning', child_session_id = NULL,
               child_workspace_id = NULL, base_kind = NULL, base_commit = NULL,
               snapshot_ref = NULL, started_at = NULL, completed_at = NULL
           WHERE id = ?""",
        (spawned.delegation_id,),
    )
    db.execute("DELETE FROM sessions WHERE id = ?", (spawned.child_session_id,))
    db.execute("DELETE FROM workspaces WHERE id = ?", (delegation["child_workspace_id"],))
    db.commit()
    return worktree_path


def test_cancel_provisioning_delegation_cleans_owned_artifacts(db, git_repo) -> None:
    """Provisioning cancellation CASes first, then removes owned staged artifacts."""
    from yinshi.services.thread_workspaces import _child_branch_name

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    spawned = _spawn_queued_child(service, request, "Provisioning child")
    branch = _child_branch_name(spawned.delegation_id)
    worktree_path = _force_pre_attach(db, spawned)
    assert Path(worktree_path).is_dir()

    outcome = asyncio.run(service.cancel_child(request, thread_id=spawned.delegation_id))

    assert outcome.status == "cancelled"
    assert outcome.child_session_id is None
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    assert delegation["status"] == "cancelled"
    assert delegation["completed_at"] is not None
    assert not Path(worktree_path).exists()
    branches = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branches == ""
    assert (
        db.execute("SELECT COUNT(*) AS n FROM workspaces WHERE kind = 'delegated'").fetchone()["n"]
        == 0
    )


def test_cancel_provisioning_delegation_never_deletes_attached_winner(db, git_repo) -> None:
    """An attached child keeps its resources when cancellation claims arrive."""
    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    spawned = _spawn_queued_child(service, request, "Attached child")
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    workspace = db.execute(
        "SELECT * FROM workspaces WHERE id = ?",
        (delegation["child_workspace_id"],),
    ).fetchone()
    worktree_path = str(workspace["path"])

    outcome = asyncio.run(service.cancel_child(request, thread_id=spawned.delegation_id))

    assert outcome.status == "cancelled"
    assert outcome.child_session_id == spawned.child_session_id
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    assert delegation["status"] == "cancelled"
    assert delegation["completed_at"] is not None
    assert delegation["child_session_id"] == spawned.child_session_id
    assert Path(worktree_path).is_dir()
    assert (
        db.execute("SELECT COUNT(*) AS n FROM workspaces WHERE kind = 'delegated'").fetchone()["n"]
        == 1
    )
    assert (
        db.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE id = ?",
            (spawned.child_session_id,),
        ).fetchone()["n"]
        == 1
    )
