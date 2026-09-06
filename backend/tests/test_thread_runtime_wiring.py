"""Verify application-owned journal and thread lifecycle wiring."""

import asyncio
import uuid
from contextlib import AsyncExitStack

import httpx
import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.models import ThreadChildCreate
from yinshi.services.thread_orchestration import ThreadOrchestrationService


@pytest.mark.parametrize("entry", ["lifespan", "request"])
async def test_runtime_activation_recovers_and_seals_committed_child_outcomes(db, git_repo, entry):
    from yinshi.main import create_app
    from yinshi.services.thread_orchestration import initial_run_idempotency_key

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    child = await ThreadOrchestrationService().spawn_child(
        request,
        parent_session_id="parent-session",
        body=ThreadChildCreate(
            idempotency_key=str(uuid.uuid4()),
            title="Child",
            task="Inspect",
            start_immediately=False,
        ),
    )
    run_id = "2" * 32
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, ?, 'running')",
        (run_id, child.child_session_id, initial_run_idempotency_key(child.delegation_id)),
    )
    db.execute(
        'INSERT INTO prompt_events (run_id, sequence, event_json) VALUES (?, 0, \'{"type":"result"}\')',
        (run_id,),
    )
    db.execute(
        "UPDATE thread_delegations SET status = 'running' WHERE id = ?", (child.delegation_id,)
    )
    db.commit()
    app = create_app(mode="desktop")
    try:
        async with AsyncExitStack() as stack:
            if entry == "lifespan":
                await stack.enter_async_context(app.router.lifespan_context(app))
            else:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                    )
                )
                response = await client.get(f"/api/threads/{child.child_session_id}")
                assert response.status_code == 200
            assert (
                db.execute("SELECT status FROM prompt_runs WHERE id = ?", (run_id,)).fetchone()[0]
                == "completed"
            )
            result = db.execute(
                "SELECT sealed FROM thread_results WHERE delegation_id = ?", (child.delegation_id,)
            ).fetchone()
            assert result is not None and result["sealed"] == 1
    finally:
        await app.state.prompt_journal.close()


async def test_application_journal_seals_child_results_through_its_shared_service(
    db, git_repo, monkeypatch
):
    import yinshi.services.prompt_journal as journal_module
    from yinshi.main import create_app

    seed_parent_stack(db, git_repo)

    async def executor(request, session_id, body):
        yield {"type": "result"}

    monkeypatch.setattr(journal_module, "_default_prompt_executor", executor)
    app = create_app()
    request = _orchestration_request()
    request.scope["app"] = app
    journal = app.state.prompt_journal
    try:
        child = await ThreadOrchestrationService().spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()), title="Child", task="Inspect"
            ),
        )
        result = None
        for _ in range(200):
            result = db.execute(
                "SELECT sealed FROM thread_results WHERE delegation_id = ?", (child.delegation_id,)
            ).fetchone()
            if result is not None and result["sealed"]:
                break
            await asyncio.sleep(0.01)
        assert result is not None and result["sealed"] == 1
    finally:
        await journal.close()
