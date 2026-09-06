"""Owned sealing must not write through an existing redirected result reflog."""

import uuid
from pathlib import Path

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.models import ThreadChildCreate
from yinshi.services.thread_orchestration import (
    ThreadOrchestrationService,
    initial_run_idempotency_key,
)


async def test_manual_result_recovery_preserves_foreign_reflog_bytes(db, git_repo, tmp_path):
    seed_parent_stack(db, git_repo)
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
        "UPDATE thread_delegations SET status = 'running' WHERE id = ?", (child.delegation_id,)
    )
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, ?, 'completed')",
        (
            uuid.uuid4().hex,
            child.child_session_id,
            initial_run_idempotency_key(child.delegation_id),
        ),
    )
    db.commit()
    foreign = tmp_path / "foreign-reflog"
    original = b"Foreign audit data\n"
    foreign.write_bytes(original)
    reflog = Path(git_repo, ".git/logs/refs/yinshi/results", child.delegation_id)
    reflog.parent.mkdir(parents=True, exist_ok=True)
    reflog.symlink_to(foreign)
    result = await service.get_manual_result(request, session_id=child.child_session_id)
    assert foreign.read_bytes() == original
    assert result is None
    assert reflog.is_symlink()
    assert db.execute("SELECT COUNT(*) FROM thread_results WHERE sealed = 1").fetchone()[0] == 0
