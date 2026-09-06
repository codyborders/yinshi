"""Check durable report-call replay without overwriting a later child report."""

import time
import uuid

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.config import get_settings
from yinshi.models import ThreadChildCreate, ThreadResultReportCreate
from yinshi.services.orchestration_bridge import VerifiedThreadCaller
from yinshi.services.thread_orchestration import (
    ThreadOrchestrationService,
    initial_run_idempotency_key,
)


async def test_report_call_replay_returns_its_receipt_without_overwriting_later_report(
    db, git_repo, monkeypatch
):
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
            title="Child",
            task="Inspect",
            start_immediately=False,
        ),
    )
    run_id = "1" * 32
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, ?, 'running')",
        (run_id, child.child_session_id, initial_run_idempotency_key(child.delegation_id)),
    )
    db.commit()
    caller = VerifiedThreadCaller(
        session_id=child.child_session_id,
        run_id=run_id,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="first-call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    report = ThreadResultReportCreate(expected_version=0, summary="First report")
    first = await service.report_agent_result(request, caller=caller, body=report)
    later = VerifiedThreadCaller(
        session_id=child.child_session_id,
        run_id=run_id,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="later-call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    second = await service.report_agent_result(
        request, caller=later, body=report.model_copy(update={"summary": "Later report"})
    )
    replay = await ThreadOrchestrationService().report_agent_result(
        request, caller=caller, body=report
    )
    assert replay == first
    assert first["version"] == 1
    assert second["version"] == 2
    stored = db.execute(
        "SELECT summary, version FROM thread_results WHERE delegation_id = ?",
        (child.delegation_id,),
    ).fetchone()
    assert tuple(stored) == ("Later report", 2)
    assert (
        db.execute(
            "SELECT COUNT(*) FROM thread_report_calls WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        == 2
    )
