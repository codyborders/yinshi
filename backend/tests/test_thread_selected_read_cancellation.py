"""Selected agent reads retain cancellation barriers without cascading recovery."""

import time

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.config import get_settings
from yinshi.services.orchestration_bridge import VerifiedThreadCaller
from yinshi.services.thread_orchestration import ThreadOrchestrationService


@pytest.mark.parametrize("wait", [False, True])
async def test_selected_agent_read_does_not_replay_subtree_cancellation(
    db, git_repo, monkeypatch, wait
):
    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    service, request = ThreadOrchestrationService(), _orchestration_request()
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'parent-key', 'running')",
        ("1" * 32,),
    )
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('child-session', 'parent-ws')")
    db.execute(
        "INSERT INTO thread_delegations (id, parent_session_id, child_session_id, idempotency_key, initiator, title, task, requested_model, status, cancel_scope) "
        "VALUES (?, 'parent-session', 'child-session', 'child-key', 'user', 'Child', 'Inspect', 'model', 'queued', 'subtree')",
        ("2" * 32,),
    )
    db.execute(
        "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, title, task, requested_model, status) "
        "VALUES (?, 'child-session', 'nested', 'user', 'Nested', 'Inspect', 'model', 'provisioning')",
        ("4" * 32,),
    )
    db.commit()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="read-call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    before = [tuple(row) for row in db.execute("SELECT * FROM thread_delegations ORDER BY id")]
    if wait:
        result = await service.wait_for_threads(
            request, caller=caller, thread_ids=["2" * 32], timeout_seconds=0.1
        )
        assert result["timed_out"] is True
    else:
        result = await service.get_agent_thread(request, caller=caller, thread_id="2" * 32)
        assert result["thread"]["status"] == "queued"
    assert [
        tuple(row) for row in db.execute("SELECT * FROM thread_delegations ORDER BY id")
    ] == before
