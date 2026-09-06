"""A capability that expires before a recovery claim cannot mutate reservations."""

import time
import uuid
from types import SimpleNamespace

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.config import get_settings
from yinshi.models import ThreadChildCreate
from yinshi.services import thread_orchestration as orchestration
from yinshi.services.orchestration_bridge import VerifiedThreadCaller
from yinshi.services.thread_orchestration import ThreadOrchestrationService
from yinshi.services.thread_queries import ThreadNotFoundError


async def test_expiry_before_recovered_run_admission_leaves_queue_untouched(
    db, git_repo, monkeypatch
):
    from yinshi.services.prompt_journal import PromptJournal

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
    executed = []
    expiry = time.monotonic() + 60

    async def executor(request, session_id, body):
        executed.append(session_id)
        yield {"type": "result"}

    class ExpiringJournal(PromptJournal):
        async def start(self, **kwargs):
            monkeypatch.setattr(
                orchestration, "time", SimpleNamespace(monotonic=lambda: expiry + 1)
            )
            return await super().start(**kwargs)

    journal = ExpiringJournal(executor=executor)
    request.app.state.prompt_journal = journal
    try:
        await journal.recover(request)
        db.execute(
            "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'origin', 'running')",
            ("1" * 32,),
        )
        db.execute(
            "UPDATE thread_delegations SET auto_start = 1 WHERE id = ?", (child.delegation_id,)
        )
        db.commit()
        caller = VerifiedThreadCaller(
            session_id="parent-session",
            run_id="1" * 32,
            tenant_id=None,
            runtime_id=None,
            tool_call_id="read",
            expires_at=expiry,
            database_path=db.execute("PRAGMA database_list").fetchone()[2],
        )
        with pytest.raises(ThreadNotFoundError):
            await service.get_agent_thread(request, caller=caller, thread_id=child.child_session_id)
        assert (
            db.execute(
                "SELECT COUNT(*) FROM prompt_runs WHERE session_id = ?", (child.child_session_id,)
            ).fetchone()[0]
            == 0
        )
        assert executed == []
        assert (
            db.execute(
                "SELECT status FROM thread_delegations WHERE id = ?", (child.delegation_id,)
            ).fetchone()[0]
            == "queued"
        )
    finally:
        await journal.close()


async def test_expiry_before_terminal_publication_preserves_the_existing_rows(
    db, git_repo, monkeypatch
):
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
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'origin', 'running')",
        ("1" * 32,),
    )
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, ?, 'completed')",
        ("3" * 32, child.child_session_id, initial_run_idempotency_key(child.delegation_id)),
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
        tool_call_id="read",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    prepare = service._prepare_terminal_result

    def expire_before_publication(*args, **kwargs):
        monkeypatch.setattr(
            orchestration, "time", SimpleNamespace(monotonic=lambda: caller.expires_at + 1)
        )
        return prepare(*args, **kwargs)

    monkeypatch.setattr(service, "_prepare_terminal_result", expire_before_publication)
    with pytest.raises(ThreadNotFoundError):
        await service.get_agent_thread(request, caller=caller, thread_id=child.child_session_id)
    assert (
        db.execute(
            "SELECT status FROM thread_delegations WHERE id = ?", (child.delegation_id,)
        ).fetchone()[0]
        == "running"
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM thread_results WHERE delegation_id = ?", (child.delegation_id,)
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("operation", ["spawn", "get"])
async def test_expiry_after_precheck_rolls_back_stale_recovery_claim(
    db, git_repo, monkeypatch, operation
):
    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'origin', 'running')",
        ("1" * 32,),
    )
    db.execute(
        "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, title, task, requested_model, status, updated_at) VALUES (?, 'parent-session', 'stale', 'user', 'Stale', 'Inspect', 'model', 'provisioning', '2000-01-01 00:00:00')",
        ("2" * 32,),
    )
    db.commit()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="claim",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    reconcile = orchestration.reconcile_stale_provisioning

    async def expire_before_claim(request, **kwargs):
        monkeypatch.setattr(
            orchestration, "time", SimpleNamespace(monotonic=lambda: caller.expires_at + 1)
        )
        await reconcile(request, **kwargs)

    monkeypatch.setattr(orchestration, "reconcile_stale_provisioning", expire_before_claim)
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    with pytest.raises(ThreadNotFoundError):
        if operation == "spawn":
            await service.spawn_child(
                request,
                parent_session_id="parent-session",
                caller=caller,
                body=ThreadChildCreate(
                    idempotency_key=str(uuid.uuid4()),
                    title="Child",
                    task="Inspect",
                    start_immediately=False,
                ),
            )
        else:
            await service.get_agent_thread(request, caller=caller, thread_id="2" * 32)
    assert (
        db.execute("SELECT status FROM thread_delegations WHERE id = ?", ("2" * 32,)).fetchone()[0]
        == "provisioning"
    )
