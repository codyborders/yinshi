"""Exercise descendant waiting without blocking unrelated database writers."""

import asyncio
import time

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.config import get_settings
from yinshi.services.orchestration_bridge import VerifiedThreadCaller
from yinshi.services.thread_orchestration import ThreadOrchestrationService


async def test_waiter_recovers_only_requested_descendant_after_missed_observer(
    db, git_repo, monkeypatch
):
    import uuid

    from yinshi.models import ThreadChildCreate
    from yinshi.services.thread_orchestration import initial_run_idempotency_key

    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('other-root', 'parent-ws')")
    db.commit()
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    children = []
    for parent_id in ("parent-session", "other-root"):
        child = await service.spawn_child(
            request,
            parent_session_id=parent_id,
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Child",
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
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'parent-key', 'running')",
        ("1" * 32,),
    )
    db.commit()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="wait-call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    response = await asyncio.wait_for(
        service.wait_for_threads(
            request, caller=caller, thread_ids=[children[0].child_session_id], timeout_seconds=1
        ),
        timeout=3,
    )
    assert response["all_terminal"] is True
    assert response["timed_out"] is False
    assert [(thread["id"], thread["status"]) for thread in response["threads"]] == [
        (children[0].child_session_id, "completed")
    ]
    assert (
        db.execute(
            "SELECT status FROM thread_delegations WHERE id = ?", (children[1].delegation_id,)
        ).fetchone()[0]
        == "running"
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM thread_results WHERE delegation_id = ?",
            (children[1].delegation_id,),
        ).fetchone()[0]
        == 0
    )


async def test_configured_wait_limit_bounds_request_without_cancelling_child(
    db, git_repo, monkeypatch
):
    import uuid

    from yinshi.models import ThreadChildCreate
    from yinshi.services.thread_orchestration import initial_run_idempotency_key

    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    monkeypatch.setenv("THREAD_WAIT_TIMEOUT_SECONDS_MAX", "1")
    get_settings.cache_clear()
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
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) "
        "VALUES (?, 'parent-session', 'parent-key', 'running')",
        ("1" * 32,),
    )
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) "
        "VALUES (?, ?, ?, 'running')",
        ("2" * 32, child.child_session_id, initial_run_idempotency_key(child.delegation_id)),
    )
    db.execute(
        "UPDATE thread_delegations SET status = 'running' WHERE id = ?",
        (child.delegation_id,),
    )
    db.commit()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="wait-call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    started = time.monotonic()
    response = await asyncio.wait_for(
        service.wait_for_threads(
            request, caller=caller, thread_ids=[child.child_session_id], timeout_seconds=60
        ),
        timeout=3,
    )
    assert time.monotonic() - started < 3
    assert response["timed_out"] is True
    assert response["all_terminal"] is False
    assert tuple(
        db.execute(
            "SELECT status, cancel_scope FROM thread_delegations WHERE id = ?",
            (child.delegation_id,),
        ).fetchone()
    ) == ("running", None)
    assert (
        db.execute("SELECT status FROM prompt_runs WHERE id = ?", ("2" * 32,)).fetchone()[0]
        == "running"
    )


async def test_waiter_observes_committed_placeholder_completion(db, git_repo, monkeypatch):
    seed_parent_stack(db, git_repo)
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'key', 'running')",
        ("1" * 32,),
    )
    child_id = "2" * 32
    db.execute(
        "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, title, task, requested_model, status) VALUES (?, 'parent-session', 'child-key', 'user', 'Child', 'Inspect', 'model', 'provisioning')",
        (child_id,),
    )
    db.commit()
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    request = _orchestration_request()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="wait-call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    service = ThreadOrchestrationService()
    waiter = asyncio.create_task(
        service.wait_for_threads(request, caller=caller, thread_ids=[child_id], timeout_seconds=1)
    )
    try:
        await asyncio.sleep(0.05)
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE thread_delegations SET status = 'cancelled' WHERE id = ?", (child_id,))
        db.commit()
        result = await asyncio.wait_for(waiter, timeout=2)
        assert result["all_terminal"] is True
        assert result["timed_out"] is False
        assert [(thread["id"], thread["status"]) for thread in result["threads"]] == [
            (child_id, "cancelled")
        ]
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
