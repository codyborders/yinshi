"""Public list and wait tools return bounded metadata, not transcripts or full reports."""

import json
import time

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.config import get_settings
from yinshi.services.orchestration_bridge import VerifiedThreadCaller
from yinshi.services.thread_orchestration import ThreadOrchestrationService
from yinshi.services.thread_tool_handlers import build_thread_handlers


@pytest.mark.parametrize("operation", ["list_children", "wait_for_threads"])
async def test_tool_snapshots_preserve_public_fields_and_counts_with_unicode_budgets(
    db, git_repo, monkeypatch, operation
):
    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'origin', 'running')",
        ("1" * 32,),
    )
    identifiers = []
    for index in range(20):
        delegation_id, child_id = f"{index + 10:032x}", f"{index + 100:032x}"
        identifiers.append(child_id)
        db.execute("INSERT INTO sessions (id, workspace_id) VALUES (?, 'parent-ws')", (child_id,))
        db.execute(
            "INSERT INTO thread_delegations (id, parent_session_id, child_session_id, idempotency_key, initiator, title, task, role, requested_model, status, started_at, completed_at) VALUES (?, 'parent-session', ?, ?, 'user', ?, 'Inspect', 'research', ?, 'completed', '2026-01-01 00:00:00', '2026-01-01 00:01:00')",
            (delegation_id, child_id, delegation_id, "界" * 2000, "界" * 2000),
        )
        db.execute(
            "INSERT INTO thread_results (delegation_id, source, sealed, summary, changed_files_json) VALUES (?, 'reported', 1, ?, ?)",
            (
                delegation_id,
                "界" * 20_000,
                json.dumps(
                    [{"path": "file.txt", "status": "M", "kind": "modified", "original_path": None}]
                    * 3
                ),
            ),
        )
    db.commit()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="read",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    handlers = build_thread_handlers(_orchestration_request(), ThreadOrchestrationService())
    arguments = (
        {} if operation == "list_children" else {"thread_ids": identifiers, "timeout_seconds": 0}
    )
    response = await handlers[operation](arguments, caller=caller)
    rows = response["children" if operation == "list_children" else "threads"]
    assert len(rows) == 20
    assert rows[0]["thread_id"] == identifiers[0]
    assert rows[0]["state"] == "completed"
    assert rows[0]["role"] == "research"
    assert rows[0]["model"]
    assert rows[0]["started_at"] == "2026-01-01 00:00:00"
    assert rows[0]["completed_at"] == "2026-01-01 00:01:00"
    assert rows[0]["changed_files_count"] == 3
    assert rows[0]["result_available"] is True
    assert response["truncated"] is True
    assert len(json.dumps(response).encode()) < 200_000
    assert db.execute("SELECT length(title) FROM thread_delegations LIMIT 1").fetchone()[0] == 2000
    if operation == "list_children":
        assert response["children_total"] == 20
    else:
        assert response["complete"] is True
        assert response["timed_out"] is False
