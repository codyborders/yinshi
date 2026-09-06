"""Cancellation orders initial child admission without restarting accepted work."""

import asyncio
import uuid

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.models import ThreadChildCreate
from yinshi.services.prompt_journal import PromptJournal
from yinshi.services.thread_orchestration import ThreadOrchestrationService


async def test_automatic_agent_queue_does_not_outlive_its_originating_run(
    db, git_repo, monkeypatch
):
    from yinshi.config import get_settings

    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    child = await service.spawn_child(
        request,
        parent_session_id="parent-session",
        body=ThreadChildCreate(
            idempotency_key=str(uuid.uuid4()),
            title="Inspect",
            task="Inspect",
            start_immediately=False,
        ),
    )
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'origin', 'completed')",
        ("1" * 32,),
    )
    db.execute(
        "UPDATE thread_delegations SET initiator = 'agent', delegated_by_run_id = ?, delegated_by_tool_call_id = 'spawn', auto_start = 1 WHERE id = ?",
        ("1" * 32, child.delegation_id),
    )
    db.commit()

    async def execute(request, session_id, body):
        yield {"type": "result"}

    journal = PromptJournal(executor=execute)
    request.app.state.prompt_journal = journal
    try:
        await service.reconcile(request)
        assert (
            db.execute(
                "SELECT COUNT(*) FROM prompt_runs WHERE session_id = ?", (child.child_session_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT status FROM thread_delegations WHERE id = ?", (child.delegation_id,)
            ).fetchone()[0]
            == "failed"
        )
    finally:
        await journal.close()


@pytest.mark.parametrize(
    "flag,agent",
    [
        ("AGENT_DELEGATION_ENABLED", True),
        ("THREAD_HIERARCHY_ENABLED", True),
        ("THREAD_HIERARCHY_ENABLED", False),
    ],
)
async def test_disabled_features_block_recovered_initial_admission(
    db, git_repo, monkeypatch, flag, agent
):
    from yinshi.config import get_settings

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    child = await service.spawn_child(
        request,
        parent_session_id="parent-session",
        body=ThreadChildCreate(
            idempotency_key=str(uuid.uuid4()),
            title="Queued",
            task="Inspect",
            start_immediately=False,
        ),
    )
    executions = []

    async def execute(request, session_id, body):
        executions.append(session_id)
        yield {"type": "result"}

    journal = PromptJournal(executor=execute)
    request.app.state.prompt_journal = journal
    try:
        await journal.recover(request)
        db.execute(
            "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'origin', 'running')",
            ("1" * 32,),
        )
        db.execute(
            "UPDATE thread_delegations SET initiator = ?, delegated_by_run_id = ?, delegated_by_tool_call_id = 'spawn', auto_start = 1 WHERE id = ?",
            ("agent" if agent else "user", "1" * 32, child.delegation_id),
        )
        db.commit()
        monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
        monkeypatch.setenv(flag, "false")
        get_settings.cache_clear()
        await service.reconcile(request)
        assert (
            db.execute(
                "SELECT COUNT(*) FROM prompt_runs WHERE session_id = ?", (child.child_session_id,)
            ).fetchone()[0]
            == 0
        )
        assert executions == []
    finally:
        await journal.close()


@pytest.mark.parametrize("phase", ["before", "after"])
async def test_cancellation_orders_initial_run_admission(db, git_repo, phase):
    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()

    async def execute(request, session_id, body):
        await asyncio.Event().wait()
        yield {"type": "result"}

    class Journal(PromptJournal):
        async def start(self, **kwargs):
            if phase == "before":
                await service.cancel_child(request, thread_id=kwargs["session_id"])
            accepted = await super().start(**kwargs)
            if phase == "after":
                await service.cancel_child(request, thread_id=kwargs["session_id"])
            return accepted

    journal = Journal(executor=execute)
    request.app.state.prompt_journal = journal
    body = ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()), title="Inspect", task="Inspect", start_immediately=True
    )
    try:
        outcome = await service.spawn_child(request, parent_session_id="parent-session", body=body)
        assert outcome.status == "cancelled"
        runs = db.execute(
            "SELECT status FROM prompt_runs WHERE session_id = ?", (outcome.child_session_id,)
        ).fetchall()
        assert [row["status"] for row in runs] == ([] if phase == "before" else ["cancelled"])
        replay = await service.spawn_child(request, parent_session_id="parent-session", body=body)
        assert replay == outcome
    finally:
        await journal.close()
