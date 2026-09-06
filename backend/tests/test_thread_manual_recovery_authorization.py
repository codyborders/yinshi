"""Invalid manual writes cannot trigger reconciliation before target authorization."""

import uuid

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.models import ThreadResultReportCreate, ThreadRetryCreate
from yinshi.services.thread_orchestration import ThreadOrchestrationService
from yinshi.services.thread_queries import ThreadNotFoundError


@pytest.mark.parametrize("operation", ["cancel", "retry", "report"])
async def test_unknown_manual_target_does_not_reconcile_other_threads(db, git_repo, operation):
    seed_parent_stack(db, git_repo)
    db.execute(
        "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, title, task, requested_model, status, updated_at) VALUES (?, 'parent-session', 'stale', 'user', 'Stale', 'Inspect', 'model', 'provisioning', '2000-01-01 00:00:00')",
        ("2" * 32,),
    )
    db.commit()
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    with pytest.raises(ThreadNotFoundError):
        if operation == "cancel":
            await service.cancel_child(request, thread_id="unknown")
        elif operation == "retry":
            await service.retry_child(
                request,
                child_session_id="unknown",
                body=ThreadRetryCreate(idempotency_key=str(uuid.uuid4())),
            )
        else:
            await service.report_result(
                request,
                child_session_id="unknown",
                body=ThreadResultReportCreate(expected_version=0, summary="Done"),
            )
    assert (
        db.execute("SELECT status FROM thread_delegations WHERE id = ?", ("2" * 32,)).fetchone()[0]
        == "provisioning"
    )
