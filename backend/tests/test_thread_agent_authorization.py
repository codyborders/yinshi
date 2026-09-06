"""Check agent authority before durable child mutations."""

import time

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.config import get_settings
from yinshi.services.orchestration_bridge import VerifiedThreadCaller
from yinshi.services.thread_orchestration import ThreadOrchestrationService
from yinshi.services.thread_queries import ThreadNotFoundError


def test_caller_is_bound_to_the_selected_database_not_only_matching_actor_ids(
    db, git_repo, tmp_path, monkeypatch
):
    import sqlite3
    from dataclasses import replace

    from yinshi.api.deps import get_db_for_request
    from yinshi.tenant import TenantContext

    seed_parent_stack(db, git_repo)
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'key', 'running')",
        ("1" * 32,),
    )
    db.execute(
        "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, title, task, requested_model, status) VALUES (?, 'parent-session', 'child-key', 'user', 'Child', 'Inspect', 'model', 'provisioning')",
        ("2" * 32,),
    )
    db.commit()
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    requests = []
    for name in ("first", "second"):
        directory = tmp_path / name
        directory.mkdir(mode=0o700)
        path = directory / "yinshi.db"
        with sqlite3.connect(path) as destination:
            db.backup(destination)
        request = _orchestration_request()
        request.state.tenant = TenantContext(
            "same-tenant", "same@example.com", str(directory), str(path)
        )
        requests.append(request)
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id="same-tenant",
        runtime_id="parent-runtime",
        tool_call_id="call",
        expires_at=time.monotonic() + 60,
        database_path=str(tmp_path / "first" / "yinshi.db"),
    )
    service = ThreadOrchestrationService()
    with get_db_for_request(requests[0]) as first:
        assert service.authorize_descendant(first, requests[0], caller, "2" * 32)["id"] == "2" * 32
        with pytest.raises(ThreadNotFoundError):
            service.authorize_caller(first, requests[0], replace(caller, database_path=None))
    with get_db_for_request(requests[1]) as second, pytest.raises(ThreadNotFoundError):
        service.authorize_descendant(second, requests[1], caller, "2" * 32)


def test_disabled_agent_tools_reject_a_live_caller(db, git_repo, monkeypatch):
    from yinshi.services.thread_orchestration import ThreadHierarchyDisabledError

    seed_parent_stack(db, git_repo)
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'key', 'running')",
        ("1" * 32,),
    )
    db.commit()
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "false")
    get_settings.cache_clear()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    with pytest.raises(ThreadHierarchyDisabledError):
        ThreadOrchestrationService.authorize_caller(db, _orchestration_request(), caller)


def test_caller_from_another_database_cannot_authorize(db, git_repo, monkeypatch):
    seed_parent_stack(db, git_repo)
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'key', 'running')",
        ("1" * 32,),
    )
    db.commit()
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id="foreign-tenant",
        runtime_id=None,
        tool_call_id="call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    with pytest.raises(ThreadNotFoundError):
        ThreadOrchestrationService.authorize_caller(db, _orchestration_request(), caller)


def test_descendant_authorization_includes_placeholders_but_rejects_siblings(
    db, git_repo, monkeypatch
):
    seed_parent_stack(db, git_repo)
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'key', 'running')",
        ("1" * 32,),
    )
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('sibling', 'parent-ws')")
    for identifier, parent in (("2" * 32, "parent-session"), ("3" * 32, "sibling")):
        db.execute(
            "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, title, task, requested_model, status) VALUES (?, ?, ?, 'user', 'Child', 'Inspect', 'model', 'provisioning')",
            (identifier, parent, identifier),
        )
    db.commit()
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="call",
        expires_at=time.monotonic() + 60,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    assert service.authorize_descendant(db, request, caller, "2" * 32)["id"] == "2" * 32
    with pytest.raises(ThreadNotFoundError):
        service.authorize_descendant(db, request, caller, "3" * 32)


@pytest.mark.parametrize("expired", [True, False])
async def test_spawn_does_not_reconcile_outside_authorized_parent(
    db, git_repo, monkeypatch, expired
):
    import uuid

    from yinshi.models import ThreadChildCreate

    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'key', 'running')",
        ("1" * 32,),
    )
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('other-root', 'parent-ws')")
    placeholder_id = "2" * 32
    parent_id = "parent-session" if expired else "other-root"
    db.execute(
        "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, title, task, requested_model, status, created_at, updated_at) "
        "VALUES (?, ?, 'stale-key', 'user', 'Pending', 'Inspect', 'model', 'provisioning', '2000-01-01 00:00:00', '2000-01-01 00:00:00')",
        (placeholder_id, parent_id),
    )
    db.commit()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="spawn-call",
        expires_at=time.monotonic() + (-1 if expired else 60),
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    body = ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()), title="Child", task="Inspect", start_immediately=False
    )
    if expired:
        with pytest.raises(ThreadNotFoundError):
            await service.spawn_child(
                request, parent_session_id="parent-session", body=body, caller=caller
            )
    else:
        await service.spawn_child(
            request, parent_session_id="parent-session", body=body, caller=caller
        )
    stored = db.execute(
        "SELECT status FROM thread_delegations WHERE id = ?", (placeholder_id,)
    ).fetchone()
    assert stored["status"] == "provisioning"


def test_expired_caller_cannot_authorize_a_live_run(db, git_repo, monkeypatch):
    seed_parent_stack(db, git_repo)
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'key', 'running')",
        ("1" * 32,),
    )
    db.commit()
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    caller = VerifiedThreadCaller(
        session_id="parent-session",
        run_id="1" * 32,
        tenant_id=None,
        runtime_id=None,
        tool_call_id="call",
        expires_at=time.monotonic() - 1,
        database_path=db.execute("PRAGMA database_list").fetchone()[2],
    )
    with pytest.raises(ThreadNotFoundError):
        ThreadOrchestrationService.authorize_caller(db, _orchestration_request(), caller)
