"""Manual reads retry selected recovery without exposing drafts or foreign state."""

import asyncio
import uuid

import pytest

from tests.test_thread_orchestration import seed_parent_stack
from yinshi.services.thread_orchestration import initial_run_idempotency_key
from yinshi.services.thread_workspaces import ThreadWorkspaceService


@pytest.mark.parametrize("route", ["result", "tree"])
@pytest.mark.parametrize("auto_start", [False, True])
def test_read_preserves_subtree_cancellation_barrier(client, db, git_repo, route, auto_start):
    seed_parent_stack(db, git_repo)
    response = client.post(
        "/api/threads/parent-session/children",
        json={
            "idempotency_key": str(uuid.uuid4()),
            "title": "Child",
            "task": "Inspect",
            "start_immediately": False,
        },
    )
    assert response.status_code == 201
    child = response.json()
    db.execute(
        "UPDATE thread_delegations SET cancel_scope = 'subtree', auto_start = ? WHERE id = ?",
        (auto_start, child["delegation_id"]),
    )
    db.execute(
        "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, "
        "title, task, requested_model, status) VALUES "
        "(?, ?, 'nested', 'user', 'Nested', 'Inspect', 'model', 'provisioning')",
        ("2" * 32, child["child_session_id"]),
    )
    db.commit()
    before = [tuple(row) for row in db.execute("SELECT * FROM thread_delegations ORDER BY id")]
    response = client.get(f"/api/threads/{child['child_session_id']}/{route}")
    assert response.status_code == (404 if route == "result" else 200)
    assert [
        tuple(row) for row in db.execute("SELECT * FROM thread_delegations ORDER BY id")
    ] == before


async def test_recovered_admission_cannot_promote_after_owner_revocation(db, git_repo, monkeypatch):
    from tests.test_thread_orchestration import _orchestration_request
    from yinshi.models import ThreadChildCreate
    from yinshi.services import thread_orchestration as orchestration
    from yinshi.services.prompt_journal import PromptJournal

    seed_parent_stack(db, git_repo)
    service = orchestration.ThreadOrchestrationService()
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
    release = asyncio.Event()

    async def executor(request, session_id, body):
        await release.wait()
        yield {"type": "result"}

    class RevokingJournal(PromptJournal):
        async def start(self, **kwargs):
            run = await super().start(**kwargs)
            db.execute("UPDATE repos SET owner_email = 'foreign@example.com'")
            db.commit()
            return run

    journal = RevokingJournal(executor=executor)
    request.app.state.prompt_journal = journal
    try:
        await journal.recover(request)
        monkeypatch.setattr(orchestration, "get_user_email", lambda request: "owner@example.com")
        db.execute(
            "UPDATE thread_delegations SET auto_start = 1 WHERE id = ?", (child.delegation_id,)
        )
        db.commit()
        with pytest.raises(orchestration.ThreadParentNotAuthorizedError):
            await service.get_manual_result(request, session_id=child.child_session_id)
        assert (
            db.execute(
                "SELECT status FROM thread_delegations WHERE id = ?", (child.delegation_id,)
            ).fetchone()[0]
            == "queued"
        )
    finally:
        release.set()
        await journal.close()


@pytest.mark.parametrize("route", ["result", "tree"])
def test_denied_manual_read_leaves_foreign_stale_rows_untouched(
    client, db, git_repo, monkeypatch, route
):
    from yinshi.services import thread_orchestration as orchestration

    seed_parent_stack(db, git_repo)
    db.execute("UPDATE repos SET owner_email = 'foreign@example.com'")
    db.execute(
        "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, "
        "title, task, requested_model, status, updated_at) VALUES "
        "(?, 'parent-session', 'stale', 'user', 'Stale', 'Inspect', 'model', 'provisioning', '2000-01-01 00:00:00')",
        ("2" * 32,),
    )
    db.commit()
    before = tuple(db.execute("SELECT * FROM thread_delegations").fetchone())
    monkeypatch.setattr(orchestration, "get_user_email", lambda request: "owner@example.com")
    response = client.get(f"/api/threads/parent-session/{route}")
    assert response.status_code == 404
    assert tuple(db.execute("SELECT * FROM thread_delegations").fetchone()) == before


@pytest.mark.parametrize("route", ["result", "tree"])
@pytest.mark.parametrize("cancel_read", [False, True])
async def test_slow_manual_refresh_drains_only_its_owned_task(
    db, git_repo, monkeypatch, route, cancel_read
):
    from tests.test_thread_orchestration import _orchestration_request
    from yinshi.models import ThreadChildCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

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
        "UPDATE thread_delegations SET status = 'completed' WHERE id = ?", (child.delegation_id,)
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
    started, drained, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_git(*args, **kwargs):
        started.set()
        try:
            await release.wait()
        finally:
            drained.set()

    monkeypatch.setattr(ThreadWorkspaceService, "finalize_child_context", slow_git)
    unrelated = asyncio.create_task(release.wait())
    method = service.get_manual_result if route == "result" else service.get_manual_tree
    read = asyncio.create_task(method(request, session_id=child.child_session_id))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        if cancel_read:
            read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(read, timeout=1)
        else:
            result = await asyncio.wait_for(read, timeout=3)
            if route == "result":
                assert result is None
            else:
                assert [(node["id"], node["state"]) for node in result["nodes"]] == [
                    (child.child_session_id, "completed")
                ]
        assert drained.is_set()
        assert not unrelated.done()
        assert db.execute("SELECT COUNT(*) FROM thread_results WHERE sealed = 1").fetchone()[0] == 0
    finally:
        release.set()
        read.cancel()
        await asyncio.gather(read, unrelated, return_exceptions=True)


def test_tree_refresh_recovers_only_returned_tree(client, db, git_repo):
    seed_parent_stack(db, git_repo)
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('other-root', 'parent-ws')")
    for identifier, parent in (("2" * 32, "parent-session"), ("3" * 32, "other-root")):
        db.execute(
            "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, "
            "title, task, requested_model, status, updated_at) VALUES "
            "(?, ?, 'stale', 'user', 'Stale', 'Inspect', 'model', 'provisioning', '2000-01-01 00:00:00')",
            (identifier, parent),
        )
    db.commit()
    response = client.get("/api/threads/parent-session/tree")
    assert response.status_code == 200
    assert [(row["delegation_id"], row["status"]) for row in response.json()["placeholders"]] == [
        ("2" * 32, "interrupted")
    ]
    assert tuple(
        db.execute(
            "SELECT status, updated_at FROM thread_delegations WHERE id = ?", ("3" * 32,)
        ).fetchone()
    ) == ("provisioning", "2000-01-01 00:00:00")


def test_later_result_read_seals_only_the_requested_pending_result(
    client, db, git_repo, monkeypatch
):
    seed_parent_stack(db, git_repo)
    spawned = client.post(
        "/api/threads/parent-session/children",
        json={
            "idempotency_key": str(uuid.uuid4()),
            "title": "Child",
            "task": "Inspect",
            "start_immediately": False,
        },
    )
    assert spawned.status_code == 201
    child = spawned.json()
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) "
        "VALUES (?, ?, ?, 'completed')",
        (
            uuid.uuid4().hex,
            child["child_session_id"],
            initial_run_idempotency_key(child["delegation_id"]),
        ),
    )
    db.execute(
        "UPDATE thread_delegations SET status = 'running' WHERE id = ?", (child["delegation_id"],)
    )
    db.execute(
        "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, "
        "title, task, requested_model, status, updated_at) VALUES "
        "(?, 'parent-session', 'stale', 'user', 'Stale', 'Inspect', 'model', 'provisioning', '2000-01-01 00:00:00')",
        ("2" * 32,),
    )
    db.commit()
    finalize = ThreadWorkspaceService.finalize_child_context

    async def unavailable(*args, **kwargs):
        raise OSError("Git is temporarily unavailable")

    monkeypatch.setattr(ThreadWorkspaceService, "finalize_child_context", unavailable)
    path = f"/api/threads/{child['child_session_id']}/result"
    assert client.get(path).status_code == 404
    monkeypatch.setattr(ThreadWorkspaceService, "finalize_child_context", finalize)
    for _ in range(5):
        response = client.get(path)
        if response.status_code == 200:
            break
        assert response.status_code == 404
    assert response.status_code == 200
    assert response.json()["sealed"] is True
    assert response.json()["delegation_id"] == child["delegation_id"]
    assert tuple(
        db.execute(
            "SELECT status, updated_at FROM thread_delegations WHERE id = ?", ("2" * 32,)
        ).fetchone()
    ) == ("provisioning", "2000-01-01 00:00:00")
