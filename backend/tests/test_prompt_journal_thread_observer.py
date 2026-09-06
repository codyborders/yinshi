"""Check postcommit thread observation without retaining journal transactions."""

import asyncio
import uuid

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.services.prompt_journal import PromptJournal


@pytest.mark.parametrize(
    "terminal_event,expected", [("result", "completed"), (None, "interrupted")]
)
async def test_recovery_observes_the_committed_outcome_once_outside_its_transaction(
    db, git_repo, terminal_event, expected
):
    seed_parent_stack(db, git_repo)
    session_id, run_id = "1" * 32, "2" * 32
    db.execute("UPDATE sessions SET id = ? WHERE id = 'parent-session'", (session_id,))
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, 'initial', 'running')",
        (run_id, session_id),
    )
    if terminal_event is not None:
        db.execute(
            'INSERT INTO prompt_events (run_id, sequence, event_json) VALUES (?, 0, \'{"type":"result"}\')',
            (run_id,),
        )
    db.commit()
    observed = []

    async def observer(request, observed_session_id, observed_run_id, status):
        db.execute("BEGIN IMMEDIATE")
        persisted = db.execute(
            "SELECT status FROM prompt_runs WHERE id = ?", (observed_run_id,)
        ).fetchone()[0]
        db.rollback()
        observed.append((observed_session_id, observed_run_id, status, persisted))

    journal = PromptJournal(terminal_observer=observer)
    request = _orchestration_request()
    try:
        for _ in range(2):
            batch = await journal.events(
                request=request, session_id=session_id, run_id=run_id, next_sequence=0
            )
            assert batch.status == expected
        assert observed == [(session_id, run_id, expected, expected)]
    finally:
        await journal.close()


async def test_terminal_observer_reads_committed_outcome_without_database_lock(db, git_repo):
    seed_parent_stack(db, git_repo)
    session_id = "1" * 32
    db.execute("UPDATE sessions SET id = ? WHERE id = 'parent-session'", (session_id,))
    db.commit()
    observed = []

    async def executor(request, session_id, body):
        yield {"type": "result"}

    async def observer(request, observed_session_id, run_id, status):
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM prompt_runs WHERE id = ?", (run_id,)).fetchone()
        observed.append((observed_session_id, status, row["status"]))
        db.rollback()

    request = _orchestration_request()
    journal = PromptJournal(executor=executor, terminal_observer=observer)
    try:
        run = await journal.start(
            request=request,
            session_id=session_id,
            idempotency_key=str(uuid.uuid4()),
            body={"prompt": "Inspect"},
        )
        for _ in range(200):
            if observed:
                break
            await asyncio.sleep(0.01)
        assert observed == [(session_id, "completed", "completed")]
        assert (
            db.execute("SELECT status FROM prompt_runs WHERE id = ?", (run.id,)).fetchone()[0]
            == "completed"
        )
    finally:
        await journal.close()
