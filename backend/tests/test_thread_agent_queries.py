"""Read a delegated result without exposing an unbounded tool response."""

import json
import time
import uuid

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.config import get_settings
from yinshi.models import ThreadChildCreate
from yinshi.services.orchestration_bridge import VerifiedThreadCaller
from yinshi.services.thread_orchestration import ThreadOrchestrationService


@pytest.mark.parametrize("operation", ["get", "list"])
async def test_thread_read_recovers_missed_terminal_observer(db, git_repo, monkeypatch, operation):
    from yinshi.services.thread_orchestration import initial_run_idempotency_key

    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    service = ThreadOrchestrationService()
    request = _orchestration_request()
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
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'parent-key', 'running')",
        ("1" * 32,),
    )
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, ?, 'completed')",
        ("2" * 32, child.child_session_id, initial_run_idempotency_key(child.delegation_id)),
    )
    db.execute(
        "UPDATE thread_delegations SET status = 'running' WHERE id = ?", (child.delegation_id,)
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
    if operation == "get":
        response = await service.get_agent_thread(
            request, caller=caller, thread_id=child.child_session_id
        )
        thread = response["thread"]
    else:
        response = await service.list_agent_children(request, caller=caller)
        thread = response["children"][0]
    assert thread["status"] == "completed"
    assert thread["result_available"] is True


async def test_list_children_can_exclude_terminal_placeholders(db, git_repo, monkeypatch):
    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'parent-key', 'running')",
        ("1" * 32,),
    )
    for child_id, status in (("2" * 32, "provisioning"), ("3" * 32, "cancelled")):
        db.execute(
            "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, title, task, requested_model, status) VALUES (?, 'parent-session', ?, 'user', 'Child', 'Inspect', 'model', ?)",
            (child_id, child_id, status),
        )
    db.commit()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="list-call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    active = await service.list_agent_children(request, caller=caller, include_terminal=False)
    assert [(row["id"], row["status"]) for row in active["children"]] == [
        ("2" * 32, "provisioning")
    ]
    assert active["children_total"] == 1
    default = await service.list_agent_children(request, caller=caller)
    assert default["children_total"] == 2


async def test_list_children_includes_provisioning_placeholders_and_limits(
    db, git_repo, monkeypatch
):
    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'parent-key', 'running')",
        ("1" * 32,),
    )
    db.execute(
        "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, title, task, requested_model, status) VALUES (?, 'parent-session', 'key', 'user', 'Pending', 'Inspect', 'model', 'provisioning')",
        ("2" * 32,),
    )
    db.commit()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="list-call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    response = await ThreadOrchestrationService().list_agent_children(
        _orchestration_request(), caller=caller
    )
    assert [(row["id"], row["status"]) for row in response["children"]] == [
        ("2" * 32, "provisioning")
    ]
    assert response["limits"]["active_descendants"] == 1
    assert response["children_total"] == 1
    assert response["truncated"] is False


async def test_get_thread_compacts_large_stored_results_with_explicit_counts(
    db, git_repo, monkeypatch
):
    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    service = ThreadOrchestrationService()
    request = _orchestration_request()
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
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'parent-key', 'running')",
        ("1" * 32,),
    )
    tests = [{"command": "pytest", "status": "passed", "summary": "界" * 5000} for _ in range(50)]
    warnings = ["界" * 2000 for _ in range(20)]
    changed_files = [
        {"path": f"src/file-{index}.txt", "status": "M", "kind": "modified", "original_path": None}
        for index in range(5000)
    ]
    db.execute(
        "UPDATE thread_delegations SET status = 'completed' WHERE id = ?", (child.delegation_id,)
    )
    db.execute(
        "INSERT INTO thread_results (delegation_id, source, sealed, summary, tests_json, warnings_json, changed_files_json) VALUES (?, 'reported', 1, 'Done', ?, ?, ?)",
        (child.delegation_id, json.dumps(tests), json.dumps(warnings), json.dumps(changed_files)),
    )
    db.commit()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="get-call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    response = await service.get_agent_thread(
        request, caller=caller, thread_id=child.child_session_id, include_result=True
    )
    metadata_only = await service.get_agent_thread(
        request, caller=caller, thread_id=child.child_session_id, include_result=False
    )
    assert metadata_only["result"] is None
    stored_tests = db.execute(
        "SELECT tests_json FROM thread_results WHERE delegation_id = ?", (child.delegation_id,)
    ).fetchone()[0]
    assert json.loads(stored_tests) == tests
    assert response["thread"]["status"] == "completed"
    assert response["result"]["truncated"] is True
    assert response["result"]["tests_total"] == 50
    assert response["result"]["warnings_total"] == 20
    assert response["result"]["changed_files_total"] == 5000
    assert response["result"]["tests_truncated"] is True
    assert response["result"]["warnings_truncated"] is True
    assert response["result"]["changed_files_truncated"] is True
    assert len(response["result"]["tests"]) < 50
    assert len(json.dumps(response).encode()) < 200_000
