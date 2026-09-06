"""Manual mutation routes must publish through the application-owned service."""

import asyncio
import time
import uuid

import httpx
import pytest
from starlette.requests import Request

from tests.test_thread_orchestration import seed_parent_stack
from yinshi.services.thread_orchestration import ThreadOrchestrationService


def test_manual_spawn_uses_the_application_service(client, db, git_repo, monkeypatch):
    from yinshi.main import app

    seed_parent_stack(db, git_repo)
    calls = []

    class ObservedService(ThreadOrchestrationService):
        async def spawn_child(self, request, **kwargs):
            calls.append(request)
            return await super().spawn_child(request, **kwargs)

    service = ObservedService()
    monkeypatch.setattr(app.state, "thread_orchestration", service)
    response = client.post(
        "/api/threads/parent-session/children",
        json={
            "idempotency_key": str(uuid.uuid4()),
            "title": "Manual",
            "task": "Inspect",
            "start_immediately": False,
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "queued"
    assert len(calls) == 1
    assert calls[0].app.state.thread_orchestration is service
    assert (
        db.execute("SELECT child_session_id FROM thread_delegations").fetchone()[0]
        == response.json()["child_session_id"]
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_missing_application_owner_never_creates_a_fallback(
    client, db, git_repo, monkeypatch, enabled
):
    from yinshi.config import get_settings
    from yinshi.main import app

    seed_parent_stack(db, git_repo)
    monkeypatch.delattr(app.state, "thread_orchestration")
    monkeypatch.setenv("THREAD_HIERARCHY_ENABLED", str(enabled).lower())
    get_settings.cache_clear()
    response = client.post(
        "/api/threads/parent-session/children",
        json={
            "idempotency_key": str(uuid.uuid4()),
            "title": "Manual",
            "task": "Inspect",
            "start_immediately": False,
        },
    )
    assert response.status_code == (503 if enabled else 404)
    assert db.execute("SELECT COUNT(*) FROM thread_delegations").fetchone()[0] == 0


async def test_public_wait_converges_after_manual_cancellation_on_the_shared_service(
    db, git_repo, monkeypatch
):
    from yinshi.config import get_settings
    from yinshi.main import app
    from yinshi.services.orchestration_bridge import VerifiedThreadCaller
    from yinshi.services.thread_tool_handlers import build_thread_handlers

    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()
    started = asyncio.Event()

    class ObservedService(ThreadOrchestrationService):
        async def wait_for_threads(self, request, **kwargs):
            started.set()
            return await super().wait_for_threads(request, **kwargs)

    service = ObservedService()
    monkeypatch.setattr(app.state, "thread_orchestration", service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/api/repos")).status_code == 200
        response = await client.post(
            "/api/threads/parent-session/children",
            json={
                "idempotency_key": str(uuid.uuid4()),
                "title": "Manual",
                "task": "Inspect",
                "start_immediately": False,
            },
        )
        assert response.status_code == 201
        child_id = response.json()["child_session_id"]
        db.execute(
            "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, 'parent-session', 'root-run', 'running')",
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
        handler = build_thread_handlers(Request({"type": "http", "app": app}), service)[
            "wait_for_threads"
        ]
        waiter = asyncio.create_task(
            handler({"thread_ids": [child_id], "timeout_seconds": 3}, caller=caller)
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
            assert not waiter.done()
            cancelled = await client.post(f"/api/threads/{child_id}/cancel")
            assert cancelled.status_code == 200
            result = await asyncio.wait_for(waiter, 4)
            assert result["complete"] is True
            assert result["timed_out"] is False
            assert [(row["thread_id"], row["state"]) for row in result["threads"]] == [
                (child_id, "cancelled")
            ]
            assert (
                db.execute(
                    "SELECT cancel_scope FROM thread_delegations WHERE child_session_id = ?",
                    (child_id,),
                ).fetchone()[0]
                is None
            )
            assert (
                db.execute("SELECT status FROM prompt_runs WHERE id = ?", ("1" * 32,)).fetchone()[0]
                == "running"
            )
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


def test_manual_dependency_override_keeps_the_real_spawn_contract(
    client, db, git_repo, monkeypatch
):
    from yinshi.api.threads import get_thread_orchestration
    from yinshi.main import app

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    monkeypatch.delattr(app.state, "thread_orchestration")
    monkeypatch.setitem(app.dependency_overrides, get_thread_orchestration, lambda: service)
    response = client.post(
        "/api/threads/parent-session/children",
        json={
            "idempotency_key": str(uuid.uuid4()),
            "title": "Manual",
            "task": "Inspect",
            "start_immediately": False,
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "queued"
    assert (
        db.execute(
            "SELECT COUNT(*) FROM thread_delegations WHERE git_artifacts_claimed = 1"
        ).fetchone()[0]
        == 1
    )
