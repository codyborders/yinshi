"""Reconcile wiring tests: every Phase 3 write reconciles stale rows first."""

from __future__ import annotations

import asyncio
import uuid

from tests.test_thread_orchestration import _orchestration_request
from tests.test_thread_reconciliation import _seed_provisioning
from tests.test_thread_workspaces import seed_parent_stack


def test_spawn_reconciles_stale_rows_before_reserving(db, git_repo, monkeypatch) -> None:
    """Spawn reconciles the database before inserting its own reservation."""
    import yinshi.services.thread_orchestration as orchestration_module
    from yinshi.models import ThreadChildCreate
    from yinshi.services import thread_reconciliation as reconciliation_module

    seed_parent_stack(db, git_repo)
    stale_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    _seed_provisioning(db, stale_id, stale=True)
    observed: dict[str, int] = {}

    real_reconcile = reconciliation_module.reconcile_stale_provisioning

    async def recording_reconcile(request):
        observed["rows_before_reserve"] = db.execute(
            "SELECT COUNT(*) AS n FROM thread_delegations"
        ).fetchone()["n"]
        await real_reconcile(request)

    monkeypatch.setattr(
        orchestration_module,
        "reconcile_stale_provisioning",
        recording_reconcile,
    )
    outcome = asyncio.run(
        orchestration_module.ThreadOrchestrationService().spawn_child(
            _orchestration_request(),
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Fresh spawn",
                task="task text",
                start_immediately=False,
            ),
        )
    )

    assert outcome.status == "queued"
    assert observed["rows_before_reserve"] == 1
    assert db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"] == 2
    stale_row = db.execute(
        "SELECT status, error_code FROM thread_delegations WHERE id = ?",
        (stale_id,),
    ).fetchone()
    assert stale_row["status"] == "interrupted"
    assert stale_row["error_code"] == "provisioning_stale"


def test_cancel_reconciles_stale_rows_before_cancelling(db, git_repo, monkeypatch) -> None:
    """Cancellation reconciles the database before its own status decision."""
    import yinshi.services.thread_orchestration as orchestration_module
    from yinshi.models import ThreadChildCreate
    from yinshi.services import thread_reconciliation as reconciliation_module

    seed_parent_stack(db, git_repo)
    stale_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    _seed_provisioning(db, stale_id, stale=True)
    observed: dict[str, int] = {}

    real_reconcile = reconciliation_module.reconcile_stale_provisioning

    async def recording_reconcile(request):
        observed["called"] = True
        await real_reconcile(request)

    monkeypatch.setattr(
        orchestration_module,
        "reconcile_stale_provisioning",
        recording_reconcile,
    )
    service = orchestration_module.ThreadOrchestrationService()
    spawned = asyncio.run(
        service.spawn_child(
            _orchestration_request(),
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Cancel target",
                task="task text",
                start_immediately=False,
            ),
        )
    )
    observed.clear()

    outcome = asyncio.run(
        service.cancel_child(request=_orchestration_request(), thread_id=spawned.child_session_id)
    )

    assert outcome.status == "cancelled"
    assert observed.get("called") is True
    stale_row = db.execute(
        "SELECT status, error_code FROM thread_delegations WHERE id = ?",
        (stale_id,),
    ).fetchone()
    assert stale_row["status"] == "interrupted"
    assert stale_row["error_code"] == "provisioning_stale"
