"""Verify that a fast delegated prompt seals a durable result after completion."""

import asyncio
import json
import uuid

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.models import ThreadChildCreate
from yinshi.services.prompt_journal import PromptJournal
from yinshi.services.thread_orchestration import ThreadOrchestrationService


async def test_terminal_outcome_is_visible_while_git_finalization_is_pending(
    db, git_repo, monkeypatch
):
    from yinshi.services.thread_queries import get_thread
    from yinshi.services.thread_workspaces import ThreadWorkspaceService

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    started, release = asyncio.Event(), asyncio.Event()
    original_finalize = ThreadWorkspaceService.finalize_child_context

    async def delayed_finalize(self, context, **kwargs):
        started.set()
        await release.wait()
        return await original_finalize(self, context, **kwargs)

    monkeypatch.setattr(ThreadWorkspaceService, "finalize_child_context", delayed_finalize)

    async def executor(request, session_id, body):
        yield {"type": "result"}

    journal = PromptJournal(executor=executor, terminal_observer=service.observe_terminal)
    request.app.state.prompt_journal = journal
    try:
        child = await service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()), title="Child", task="Inspect"
            ),
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        thread = get_thread(db, child.child_session_id)
        assert thread["state"] == "completed"
        draft = db.execute(
            "SELECT sealed FROM thread_results WHERE delegation_id = ?", (child.delegation_id,)
        ).fetchone()
        assert draft is not None and draft["sealed"] == 0
    finally:
        release.set()
        await journal.close()


async def test_fast_child_completion_seals_fallback_result(db, git_repo):
    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    request = _orchestration_request()

    async def executor(request, session_id, body):
        yield {"type": "result"}

    journal = PromptJournal(executor=executor, terminal_observer=service.observe_terminal)
    request.app.state.prompt_journal = journal
    try:
        child = await service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()), title="Child", task="Inspect"
            ),
        )
        result = None
        for _ in range(200):
            result = db.execute(
                "SELECT * FROM thread_results WHERE delegation_id = ? AND sealed = 1",
                (child.delegation_id,),
            ).fetchone()
            if result is not None:
                break
            await asyncio.sleep(0.01)
        assert result is not None
        assert result["source"] == "derived"
        assert "Child did not submit a structured result report." in json.loads(
            result["warnings_json"]
        )
        row = db.execute(
            "SELECT status FROM thread_delegations WHERE id = ?", (child.delegation_id,)
        ).fetchone()
        assert row["status"] == "completed"
        assert result["result_commit"]
        assert result["result_ref"] == f"refs/yinshi/results/{child.delegation_id}"
    finally:
        await journal.close()
