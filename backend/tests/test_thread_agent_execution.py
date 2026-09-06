"""Exercise durable child execution and replay through orchestration operations."""

import asyncio
import uuid

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.models import ThreadChildCreate
from yinshi.services.thread_orchestration import ThreadOrchestrationService
from yinshi.services.thread_workspaces import ThreadWorkspaceService


async def test_agent_spawn_persists_trusted_identity_and_auto_start(db, git_repo, monkeypatch):
    import time

    from yinshi.config import get_settings
    from yinshi.services.orchestration_bridge import VerifiedThreadCaller
    from yinshi.services.prompt_journal import PromptJournal

    seed_parent_stack(db, git_repo)
    parent_session_id = "1" * 32
    db.execute("UPDATE sessions SET id = ? WHERE id = 'parent-session'", (parent_session_id,))
    db.commit()
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    request = _orchestration_request()
    parent_started = asyncio.Event()

    async def executor(request, session_id, body):
        if session_id == parent_session_id:
            parent_started.set()
            await asyncio.Event().wait()
        yield {"type": "result"}

    journal = PromptJournal(executor=executor)
    request.app.state.prompt_journal = journal
    try:
        parent = await journal.start(
            request=request,
            session_id=parent_session_id,
            idempotency_key=str(uuid.uuid4()),
            body={"prompt": "Delegate"},
        )
        await asyncio.wait_for(parent_started.wait(), timeout=2)
        caller = VerifiedThreadCaller(
            session_id=parent_session_id,
            run_id=parent.id,
            tenant_id=None,
            runtime_id=None,
            tool_call_id="sdk-call",
            expires_at=time.monotonic() + 60,
            database_path=db.execute("PRAGMA database_list").fetchone()[2],
        )
        service = ThreadOrchestrationService()
        body = ThreadChildCreate(idempotency_key=str(uuid.uuid4()), title="Child", task="Inspect")
        first = await service.spawn_child(
            request, parent_session_id=parent_session_id, body=body, caller=caller
        )
        replay = await service.spawn_child(
            request,
            parent_session_id=parent_session_id,
            body=body.model_copy(update={"idempotency_key": str(uuid.uuid4())}),
            caller=caller,
        )
        row = db.execute(
            "SELECT * FROM thread_delegations WHERE id = ?", (first.delegation_id,)
        ).fetchone()
        assert replay.delegation_id == first.delegation_id
        assert (
            row["initiator"],
            row["delegated_by_run_id"],
            row["delegated_by_tool_call_id"],
            row["auto_start"],
        ) == ("agent", parent.id, "sdk-call", 1)
        assert (
            db.execute(
                "SELECT COUNT(*) FROM prompt_runs WHERE session_id = ?", (first.child_session_id,)
            ).fetchone()[0]
            == 1
        )
    finally:
        await journal.close()


async def test_turn_spawn_limit_counts_new_calls_but_not_same_call_replay(
    db, git_repo, monkeypatch
):
    import time
    from dataclasses import replace

    import pytest

    from yinshi.config import get_settings
    from yinshi.services.orchestration_bridge import VerifiedThreadCaller
    from yinshi.services.thread_orchestration import ThreadOrchestrationError

    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    monkeypatch.setenv("THREAD_MAX_SPAWNS_PER_TURN", "1")
    get_settings.cache_clear()
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'origin', 'running')",
        ("1" * 32,),
    )
    db.commit()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="first",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    body = ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()), title="Child", task="Inspect", start_immediately=False
    )
    first = await service.spawn_child(
        request, parent_session_id="parent-session", caller=caller, body=body
    )
    assert (
        await service.spawn_child(
            request, parent_session_id="parent-session", caller=caller, body=body
        )
        == first
    )
    with pytest.raises(ThreadOrchestrationError) as error:
        await service.spawn_child(
            request,
            parent_session_id="parent-session",
            caller=replace(caller, tool_call_id="second"),
            body=body,
        )
    assert error.value.code == "spawn_limit_exceeded"
    assert db.execute("SELECT COUNT(*) FROM thread_delegations").fetchone()[0] == 1


async def test_agent_spawn_requires_a_live_caller_run(db, git_repo, monkeypatch):
    import time

    import pytest

    from yinshi.config import get_settings
    from yinshi.services.orchestration_bridge import VerifiedThreadCaller
    from yinshi.services.thread_queries import ThreadNotFoundError

    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="missing-run",
        tenant_id=None,
        runtime_id=None,
        tool_call_id="call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    body = ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()), title="Child", task="Inspect", start_immediately=False
    )
    with pytest.raises(ThreadNotFoundError):
        await ThreadOrchestrationService().spawn_child(
            _orchestration_request(),
            parent_session_id="parent-session",
            body=body,
            caller=caller,
        )
    assert db.execute("SELECT COUNT(*) FROM thread_delegations").fetchone()[0] == 0


async def test_same_key_inflight_replay_has_one_git_writer(db, git_repo, monkeypatch):
    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    body = ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()), title="Child", task="Inspect", start_immediately=False
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original = ThreadWorkspaceService.create_child_git_artifacts
    calls = 0

    async def provision(self, context, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await original(self, context, **kwargs)

    monkeypatch.setattr(ThreadWorkspaceService, "create_child_git_artifacts", provision)
    first = asyncio.create_task(
        service.spawn_child(request, parent_session_id="parent-session", body=body)
    )
    await entered.wait()
    second = asyncio.create_task(
        service.spawn_child(request, parent_session_id="parent-session", body=body)
    )
    await asyncio.sleep(0.05)
    release.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert calls == 1
    assert not any(isinstance(item, BaseException) for item in outcomes)
    assert outcomes[0].delegation_id == outcomes[1].delegation_id
    assert db.execute("SELECT COUNT(*) FROM workspaces WHERE kind = 'delegated'").fetchone()[0] == 1
