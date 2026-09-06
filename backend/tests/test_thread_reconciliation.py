"""Stale provisioning reconciliation tests for Phase 3 orchestration writes."""

from __future__ import annotations

import asyncio
import logging
import uuid

import pytest

from tests.test_thread_orchestration import _orchestration_request
from tests.test_thread_workspaces import seed_parent_stack
from yinshi.services.thread_reconciliation import reconcile_stale_provisioning


def _seed_provisioning(
    db,
    delegation_id: str,
    *,
    status: str = "provisioning",
    stale: bool = False,
    idempotency_key: str | None = None,
) -> None:
    """Insert one delegation row, optionally aged past the stale threshold."""
    db.execute(
        """INSERT INTO thread_delegations (
               id, parent_session_id, idempotency_key, initiator,
               title, task, requested_model, status, updated_at
           ) VALUES (
               ?, 'parent-session', ?, 'user',
               'Stale child', 'task text', 'model-x', ?,
               datetime('now', ?)
           )""",
        (
            delegation_id,
            idempotency_key or f"key-{delegation_id[:8]}",
            status,
            "-700 seconds" if stale else "+0 seconds",
        ),
    )
    db.commit()


def test_reconcile_logs_only_error_type_for_context_failure(
    db,
    git_repo,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reconciliation warnings exclude private exception text."""
    from yinshi.services.thread_workspaces import ThreadWorkspaceService

    seed_parent_stack(db, git_repo)
    _seed_provisioning(db, "4" * 32, stale=True)
    db.execute(
        "UPDATE thread_delegations SET git_artifacts_claimed = 1, git_artifact_namespace = ? WHERE id = ?",
        ("f" * 64, "4" * 32),
    )
    db.commit()
    private_text = "PRIVATE_RECONCILIATION_PATH"

    def fail_context(*_args, **_kwargs):
        raise RuntimeError(private_text)

    monkeypatch.setattr(ThreadWorkspaceService, "load_parent_context", fail_context)
    caplog.set_level(logging.WARNING, logger="yinshi.services.thread_reconciliation")

    asyncio.run(reconcile_stale_provisioning(_orchestration_request()))

    assert private_text not in caplog.text
    assert "RuntimeError" in caplog.text


def test_reconcile_claims_stale_provisioning_row(db, git_repo) -> None:
    """One aged provisioning reservation becomes interrupted with a safe code."""
    from yinshi.services.thread_lifecycle import DELEGATION_STATUS_INTERRUPTED

    seed_parent_stack(db, git_repo)
    delegation_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    _seed_provisioning(db, delegation_id, stale=True)

    asyncio.run(reconcile_stale_provisioning(_orchestration_request()))

    row = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (delegation_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == DELEGATION_STATUS_INTERRUPTED
    assert row["error_code"] == "provisioning_stale"
    assert row["error_detail_safe"]
    assert row["completed_at"] is not None


def test_reconcile_cleans_stale_reservation_artifacts(db, git_repo) -> None:
    """Only the claimed reservation's worktree, branch, and snapshot ref go."""
    import subprocess
    from pathlib import Path

    from tests.test_thread_workspaces import run_git
    from yinshi.models import ThreadChildCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationService
    from yinshi.services.thread_workspaces import _child_branch_name

    seed_parent_stack(db, git_repo)
    # A dirty parent forces a published snapshot ref owned by the reservation.
    Path(git_repo, "dirty.txt").write_text("dirty", encoding="utf-8")
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    spawned = asyncio.run(
        service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Stale child",
                task="task text",
                start_immediately=False,
            ),
        )
    )
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    workspace = db.execute(
        "SELECT * FROM workspaces WHERE id = ?",
        (delegation["child_workspace_id"],),
    ).fetchone()
    worktree_path = str(workspace["path"])
    branch = _child_branch_name(spawned.delegation_id)
    # Snapshot intent commits before publication, so attachment rollback retains it.
    # Rewind only attachment metadata and age the reservation past the threshold.
    db.execute(
        """UPDATE thread_delegations
           SET status = 'provisioning', child_session_id = NULL,
               child_workspace_id = NULL, started_at = NULL, completed_at = NULL,
               updated_at = datetime('now', '-700 seconds')
           WHERE id = ?""",
        (spawned.delegation_id,),
    )
    db.execute("DELETE FROM sessions WHERE id = ?", (spawned.child_session_id,))
    db.execute("DELETE FROM workspaces WHERE id = ?", (delegation["child_workspace_id"],))
    db.commit()
    assert Path(worktree_path).is_dir()

    asyncio.run(reconcile_stale_provisioning(request))

    assert not Path(worktree_path).exists()
    assert branch not in run_git("worktree", "list", "--porcelain", cwd=git_repo)
    branches = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branches == ""
    assert (
        run_git(
            "for-each-ref",
            "--format=%(refname)",
            f"refs/yinshi/snapshots/{spawned.delegation_id}",
            cwd=git_repo,
        )
        == ""
    )


def test_reconcile_leaves_fresh_provisioning_row_alone(db, git_repo) -> None:
    """A recent provisioning reservation is never claimed or cleaned."""
    import subprocess
    from pathlib import Path

    from yinshi.models import ThreadChildCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    spawned = asyncio.run(
        service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Fresh child",
                task="task text",
                start_immediately=False,
            ),
        )
    )
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    workspace = db.execute(
        "SELECT path, branch FROM workspaces WHERE id = ?",
        (delegation["child_workspace_id"],),
    ).fetchone()
    worktree_path = str(workspace["path"])
    branch = str(workspace["branch"])
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

    asyncio.run(reconcile_stale_provisioning(request))

    row = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    assert row["status"] == "provisioning"
    assert row["error_code"] is None
    assert Path(worktree_path).is_dir()
    branches = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branches != ""


def test_reconcile_never_cleans_advanced_row(db, git_repo) -> None:
    """A queued reservation keeps its attached child even when aged."""
    from pathlib import Path

    from yinshi.models import ThreadChildCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    spawned = asyncio.run(
        service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Attached child",
                task="task text",
                start_immediately=False,
            ),
        )
    )
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    workspace = db.execute(
        "SELECT path FROM workspaces WHERE id = ?",
        (delegation["child_workspace_id"],),
    ).fetchone()
    worktree_path = str(workspace["path"])
    db.execute(
        "UPDATE thread_delegations SET updated_at = datetime('now', '-700 seconds')"
        " WHERE id = ?",
        (spawned.delegation_id,),
    )
    db.commit()

    asyncio.run(reconcile_stale_provisioning(request))

    row = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    assert row["status"] == "queued"
    assert row["error_code"] is None
    assert row["child_session_id"] == spawned.child_session_id
    assert Path(worktree_path).is_dir()
    assert (
        db.execute("SELECT COUNT(*) AS n FROM workspaces WHERE kind = 'delegated'").fetchone()["n"]
        == 1
    )
