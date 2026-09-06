"""Cancel a descendant subtree without reopening completed ancestor outcomes."""

import asyncio
import time
import uuid
from unittest.mock import AsyncMock

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.config import get_settings
from yinshi.services.orchestration_bridge import VerifiedThreadCaller
from yinshi.services.prompt_journal import PromptJournal, PromptRun
from yinshi.services.thread_orchestration import (
    ThreadOrchestrationService,
    initial_run_idempotency_key,
)
from yinshi.services.thread_workspaces import ThreadWorkspaceService


async def test_restart_resumes_a_committed_cascade_without_restarting_model_work(
    db, git_repo, monkeypatch
):
    from yinshi.models import ThreadChildCreate

    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    monkeypatch.setenv("THREAD_MAX_DEPTH", "3")
    get_settings.cache_clear()
    child_id, leaf_id = "2" * 32, "3" * 32
    for session_id in (child_id, leaf_id):
        db.execute("INSERT INTO sessions (id, workspace_id) VALUES (?, 'parent-ws')", (session_id,))
    for identifier, parent, child, status in (
        ("a" * 32, "parent-session", child_id, "completed"),
        ("b" * 32, child_id, leaf_id, "running"),
    ):
        db.execute(
            "INSERT INTO thread_delegations (id, parent_session_id, child_session_id, idempotency_key, initiator, title, task, requested_model, status) VALUES (?, ?, ?, ?, 'user', 'Child', 'Inspect', 'model', ?)",
            (identifier, parent, child, identifier, status),
        )
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'parent-key', 'running')",
        ("1" * 32,),
    )
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, ?, 'running')",
        ("4" * 32, leaf_id, initial_run_idempotency_key("b" * 32)),
    )
    db.commit()

    class InterruptedJournal(PromptJournal):
        async def cancel(self, **kwargs):
            raise asyncio.CancelledError()

    request = _orchestration_request()
    request.app.state.prompt_journal = InterruptedJournal()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="cancel-call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    with pytest.raises(asyncio.CancelledError):
        await ThreadOrchestrationService().cancel_child(
            request, thread_id=child_id, caller=caller, cascade=True
        )
    calls = []

    async def execute(request, session_id, body):
        calls.append(session_id)
        yield {"type": "result"}

    journal = PromptJournal(executor=execute)
    request.app.state.prompt_journal = journal
    recovered = ThreadOrchestrationService()
    try:
        await recovered.reconcile(request)
        states = {
            row["id"]: row["status"]
            for row in db.execute("SELECT id, status FROM thread_delegations")
        }
        assert states == {"a" * 32: "completed", "b" * 32: "interrupted"}
        follow_up = await recovered.spawn_child(
            request,
            parent_session_id=child_id,
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Later",
                task="Inspect",
                start_immediately=False,
            ),
        )
        assert follow_up.status == "queued"
        assert calls == []
    finally:
        await journal.close()


async def test_cascade_claim_blocks_new_manual_children_until_stop_finishes(
    db, git_repo, monkeypatch
):
    from yinshi.models import ThreadChildCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationError

    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    monkeypatch.setenv("THREAD_MAX_DEPTH", "3")
    get_settings.cache_clear()
    child_id, leaf_id = "2" * 32, "3" * 32
    for session_id in (child_id, leaf_id):
        db.execute("INSERT INTO sessions (id, workspace_id) VALUES (?, 'parent-ws')", (session_id,))
    for identifier, parent, child, status in (
        ("a" * 32, "parent-session", child_id, "completed"),
        ("b" * 32, child_id, leaf_id, "running"),
    ):
        db.execute(
            "INSERT INTO thread_delegations (id, parent_session_id, child_session_id, idempotency_key, initiator, title, task, requested_model, status) VALUES (?, ?, ?, ?, 'user', 'Child', 'Inspect', 'model', ?)",
            (identifier, parent, child, identifier, status),
        )
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'parent-key', 'running')",
        ("1" * 32,),
    )
    db.commit()
    started, release = asyncio.Event(), asyncio.Event()
    request = _orchestration_request()
    service = ThreadOrchestrationService()

    async def stop(request, delegation_id, session_id):
        started.set()
        await release.wait()
        return PromptRun(id="4" * 32, session_id=session_id, status="cancelled")

    monkeypatch.setattr(service, "_cancel_child_prompt_run", stop)
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="cancel-call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    cancellation = asyncio.create_task(
        service.cancel_child(request, thread_id=child_id, caller=caller, cascade=True)
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(ThreadOrchestrationError) as error:
            await service.spawn_child(
                request,
                parent_session_id=child_id,
                body=ThreadChildCreate(
                    idempotency_key=str(uuid.uuid4()),
                    title="Late",
                    task="Inspect",
                    start_immediately=False,
                ),
            )
        assert error.value.code == "thread_cancel_pending"
    finally:
        release.set()
        await cancellation


async def test_cascade_stops_active_descendants_beneath_a_completed_child_once(
    db, git_repo, monkeypatch
):
    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    child_id, leaf_id = "2" * 32, "3" * 32
    for session_id in (child_id, leaf_id):
        db.execute("INSERT INTO sessions (id, workspace_id) VALUES (?, 'parent-ws')", (session_id,))
    for identifier, parent, child, status in (
        ("a" * 32, "parent-session", child_id, "completed"),
        ("b" * 32, child_id, leaf_id, "running"),
        ("c" * 32, child_id, None, "provisioning"),
    ):
        db.execute(
            "INSERT INTO thread_delegations (id, parent_session_id, child_session_id, idempotency_key, initiator, title, task, requested_model, status) VALUES (?, ?, ?, ?, 'user', 'Child', 'Inspect', 'model', ?)",
            (identifier, parent, child, identifier, status),
        )
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'parent-key', 'running')",
        ("1" * 32,),
    )
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, ?, 'running')",
        ("4" * 32, leaf_id, initial_run_idempotency_key("b" * 32)),
    )
    db.commit()
    stopped = []

    class Journal(PromptJournal):
        async def cancel(self, *, request, session_id, run_id):
            if (
                db.execute("SELECT status FROM prompt_runs WHERE id = ?", (run_id,)).fetchone()[0]
                == "running"
            ):
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE prompt_runs SET status = 'cancelled' WHERE id = ?", (run_id,))
                db.commit()
                stopped.append(session_id)
            return PromptRun(id=run_id, session_id=session_id, status="cancelled")

    monkeypatch.setattr(
        ThreadWorkspaceService, "discard_staged_child_git_artifacts", AsyncMock(return_value=True)
    )
    request = _orchestration_request()
    request.app.state.prompt_journal = Journal()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="cancel-call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    service = ThreadOrchestrationService()
    first = await service.cancel_child(request, thread_id=child_id, caller=caller, cascade=True)
    replay = await service.cancel_child(request, thread_id=child_id, caller=caller, cascade=True)
    assert first.status == replay.status == "completed"
    states = {
        row["id"]: row["status"] for row in db.execute("SELECT id, status FROM thread_delegations")
    }
    assert states == {"a" * 32: "completed", "b" * 32: "cancelled", "c" * 32: "cancelled"}
    assert stopped == [leaf_id]
