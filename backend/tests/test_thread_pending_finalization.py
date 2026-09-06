"""Slow Git result sealing cannot hide committed outcomes in sibling reads."""

import asyncio
import time
import uuid

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.config import get_settings
from yinshi.models import ThreadChildCreate
from yinshi.services.orchestration_bridge import VerifiedThreadCaller
from yinshi.services.thread_orchestration import (
    ThreadOrchestrationService,
    initial_run_idempotency_key,
)
from yinshi.services.thread_workspaces import ThreadWorkspaceService


@pytest.mark.parametrize("operation", ["wait", "list"])
async def test_slow_first_result_does_not_hide_other_terminal_children(
    db, git_repo, monkeypatch, operation
):
    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    children = []
    for index in range(2):
        child = await service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title=f"Child {index}",
                task="Inspect",
                start_immediately=False,
            ),
        )
        children.append(child)
        db.execute(
            "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, ?, 'completed')",
            (
                uuid.uuid4().hex,
                child.child_session_id,
                initial_run_idempotency_key(child.delegation_id),
            ),
        )
        db.execute(
            "UPDATE thread_delegations SET status = 'running' WHERE id = ?", (child.delegation_id,)
        )
        db.commit()
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'origin', 'running')",
        ("1" * 32,),
    )
    db.commit()
    release = asyncio.Event()

    async def delayed_finalize(workspaces, context, **kwargs):
        await release.wait()
        raise AssertionError("Read recovery must not wait for Git release")

    monkeypatch.setattr(ThreadWorkspaceService, "finalize_child_context", delayed_finalize)
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="read",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    try:
        if operation == "wait":
            response = await asyncio.wait_for(
                service.wait_for_threads(
                    request,
                    caller=caller,
                    thread_ids=[child.child_session_id for child in children],
                    timeout_seconds=1,
                ),
                3,
            )
            assert response["all_terminal"] is True
            assert response["timed_out"] is False
            threads = response["threads"]
        else:
            response = await asyncio.wait_for(
                service.list_agent_children(request, caller=caller), 3
            )
            threads = response["children"]
        assert len(threads) == 2
        assert all(thread["status"] == "completed" for thread in threads)
        assert all(
            thread["result_pending"] is True and thread["result_available"] is False
            for thread in threads
        )
        assert not any(
            task.get_name() == "thread-wait-reconciliation" for task in asyncio.all_tasks()
        )
    finally:
        release.set()
