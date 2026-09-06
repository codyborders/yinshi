"""Recover a committed prompt outcome when terminal observation was interrupted."""

import uuid

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.models import ThreadChildCreate
from yinshi.services.thread_orchestration import (
    ThreadOrchestrationService,
    initial_run_idempotency_key,
)


async def test_recovery_starts_only_persisted_automatic_queue_once(db, git_repo):
    from yinshi.services.prompt_journal import PromptJournal

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    body = ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()), title="Child", task="Inspect", start_immediately=False
    )
    automatic = await service.spawn_child(request, parent_session_id="parent-session", body=body)
    manual = await service.spawn_child(
        request,
        parent_session_id="parent-session",
        body=body.model_copy(update={"idempotency_key": str(uuid.uuid4())}),
    )
    db.execute(
        "UPDATE thread_delegations SET auto_start = 1 WHERE id = ?", (automatic.delegation_id,)
    )
    db.commit()

    async def executor(request, session_id, body):
        yield {"type": "result"}

    journal = PromptJournal(executor=executor, terminal_observer=service.observe_terminal)
    request.app.state.prompt_journal = journal
    try:
        await service.reconcile(request)
        await service.reconcile(request)
        runs = db.execute("SELECT session_id FROM prompt_runs").fetchall()
        assert [row["session_id"] for row in runs] == [automatic.child_session_id]
        assert (
            db.execute(
                "SELECT status FROM thread_delegations WHERE id = ?", (manual.delegation_id,)
            ).fetchone()[0]
            == "queued"
        )
    finally:
        await journal.close()


async def test_reconciliation_seals_a_terminal_run_left_with_queued_delegation(db, git_repo):
    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    child = await service.spawn_child(
        request,
        parent_session_id="parent-session",
        body=ThreadChildCreate(
            idempotency_key=str(uuid.uuid4()),
            title="Child",
            task="Inspect",
            start_immediately=False,
        ),
    )
    run_id = "1" * 32
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, ?, 'completed')",
        (run_id, child.child_session_id, initial_run_idempotency_key(child.delegation_id)),
    )
    db.execute(
        "INSERT INTO messages (session_id, role, content, turn_id) VALUES (?, 'assistant', 'Recovered answer', ?)",
        (child.child_session_id, run_id),
    )
    db.commit()
    await service.reconcile(request)
    row = db.execute(
        "SELECT d.status, r.sealed, r.summary FROM thread_delegations d JOIN thread_results r ON r.delegation_id = d.id WHERE d.id = ?",
        (child.delegation_id,),
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("completed", 1, "Recovered answer")
