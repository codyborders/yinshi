"""POST /api/threads/{parent_session_id}/children API contract tests."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient  # noqa: F401

from yinshi.services.prompt_journal import PromptJournal, PromptRun


class RecordingJournal(PromptJournal):
    """PromptJournal test double that records start calls."""

    def __init__(self, *, error: Exception | None = None) -> None:
        async def dead_executor(request, session_id, body):  # pragma: no cover
            raise AssertionError("prompt executor must not run")
            yield

        super().__init__(executor=dead_executor)
        self.starts: list[dict[str, object]] = []
        self.error = error

    async def start(self, **kwargs) -> PromptRun:
        self.starts.append(kwargs)
        if self.error is not None:
            raise self.error
        return PromptRun(
            id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            session_id=str(kwargs["session_id"]),
            status="starting",
        )


def _post(client: TestClient, parent_session_id: str, **overrides: object):
    payload: dict[str, object] = {
        "idempotency_key": str(uuid.uuid4()),
        "title": "Child title",
        "task": "Child task",
        "start_immediately": False,
    }
    payload.update(overrides)
    return client.post(
        f"/api/threads/{parent_session_id}/children",
        json=payload,
        headers={"X-Requested-With": "XMLHttpRequest"},
    )


def _seed(noauth_client, db, git_repo, monkeypatch) -> dict[str, object]:
    """Seed one parent stack and install a recording journal on the app."""
    from tests.test_thread_workspaces import seed_parent_stack
    from yinshi.main import app

    seed_parent_stack(db, git_repo)
    journal = RecordingJournal()
    monkeypatch.setattr(app.state, "prompt_journal", journal)

    def delegation_status() -> str | None:
        row = db.execute(
            "SELECT status FROM thread_delegations " "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row["status"])

    return {"journal": journal, "delegation_status": delegation_status}


def test_spawn_children_tenant_runtime_uses_request_tenant(
    auth_client_factory, git_repo, monkeypatch
):
    """In tenant mode the journal sees the tenant request runtime of the caller."""
    from tests.factories import create_full_stack
    from yinshi.main import app

    client = auth_client_factory(email="tenant-spawn@example.com")
    stack = create_full_stack(client, git_repo, name="spawn-tenant")
    parent_session_id = stack["session"]["id"]
    journal = RecordingJournal()
    monkeypatch.setattr(app.state, "prompt_journal", journal)

    response = client.post(
        f"/api/threads/{parent_session_id}/children",
        json={
            "idempotency_key": str(uuid.uuid4()),
            "title": "Child title",
            "task": "Child task",
            "start_immediately": True,
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "running"
    assert journal.starts[0]["request"].state.tenant is not None
    assert journal.starts[0]["request"].state.tenant.email == "tenant-spawn@example.com"


def test_spawn_children_start_failure_returns_500_and_keeps_workspace(
    noauth_client, db, git_repo, monkeypatch
):
    """A rejected prompt start maps to a safe 500 and preserves the child."""
    from tests.test_thread_workspaces import seed_parent_stack
    from yinshi.main import app

    seed_parent_stack(db, git_repo)
    journal = RecordingJournal(error=RuntimeError("boom"))
    monkeypatch.setattr(app.state, "prompt_journal", journal)

    response = _post(noauth_client, "parent-session", start_immediately=True)

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["code"] == "start_failed"
    assert "boom" not in response.text
    delegation = db.execute("SELECT * FROM thread_delegations WHERE status = 'failed'").fetchone()
    assert delegation is not None
    assert delegation["error_code"] == "start_failed"
    assert delegation["child_workspace_id"] is not None
    assert (
        db.execute("SELECT COUNT(*) AS n FROM workspaces WHERE kind = 'delegated'").fetchone()["n"]
        == 1
    )


def test_spawn_children_foreign_owner_returns_404(noauth_client, db, git_repo, monkeypatch):
    """A legacy parent owned by another account maps to 404 like a miss."""
    import yinshi.services.thread_orchestration as orchestration_module

    _seed(noauth_client, db, git_repo, monkeypatch)
    db.execute("UPDATE repos SET owner_email = 'owner@example.com' WHERE id = 'repo1'")
    db.commit()
    monkeypatch.setattr(orchestration_module, "get_user_email", lambda request: "other@example.com")

    response = _post(noauth_client, "parent-session")

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"


def test_spawn_children_missing_parent_returns_404(noauth_client, db, git_repo, monkeypatch):
    """An unknown parent session maps to 404 without leaking detail."""
    _seed(noauth_client, db, git_repo, monkeypatch)
    response = _post(noauth_client, "no-such-session")
    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"


def test_spawn_children_hierarchy_disabled_returns_404(noauth_client, db, monkeypatch):
    """A disabled hierarchy flag maps the spawn route to 404."""
    from yinshi.config import get_settings

    monkeypatch.setenv("THREAD_HIERARCHY_ENABLED", "false")
    get_settings.cache_clear()
    try:
        response = _post(noauth_client, "missing-session")
        assert response.status_code == 404
        assert response.json()["detail"] == "Thread hierarchy is disabled"
    finally:
        get_settings.cache_clear()


def test_spawn_children_replay_returns_stable_body(noauth_client, db, git_repo, monkeypatch):
    """One replayed POST returns the identical spawn body and schedules once."""
    seed = _seed(noauth_client, db, git_repo, monkeypatch)
    journal = seed["journal"]
    key = str(uuid.uuid4())

    first = _post(noauth_client, "parent-session", idempotency_key=key, start_immediately=True)
    second = _post(noauth_client, "parent-session", idempotency_key=key, start_immediately=True)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    assert len(journal.starts) == 1


def test_spawn_children_start_immediately_returns_running(noauth_client, db, git_repo, monkeypatch):
    """A started spawn returns running and uses the deterministic run key."""
    from yinshi.services.thread_lifecycle import initial_run_idempotency_key

    seed = _seed(noauth_client, db, git_repo, monkeypatch)
    journal = seed["journal"]

    key = str(uuid.uuid4())
    response = _post(
        noauth_client,
        "parent-session",
        idempotency_key=key,
        start_immediately=True,
        model="model-x",
        thinking="high",
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "running"
    assert body["child_session_id"] == journal.starts[0]["session_id"]
    assert journal.starts[0]["idempotency_key"] == initial_run_idempotency_key(
        body["delegation_id"]
    )
    assert seed["delegation_status"]() == "running"


def test_spawn_children_returns_queued_identity(noauth_client, db, git_repo, monkeypatch):
    """A no-start spawn returns the queued child identity without a prompt run."""
    from tests.test_thread_workspaces import seed_parent_stack
    from yinshi.main import app

    seed_parent_stack(db, git_repo)
    journal = RecordingJournal()
    monkeypatch.setattr(app.state, "prompt_journal", journal)

    response = _post(noauth_client, "parent-session")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["child_session_id"]
    assert body["delegation_id"]
    assert body["error_code"] is None
    assert journal.starts == []
    row = db.execute("SELECT id FROM sessions WHERE id = ?", (body["child_session_id"],)).fetchone()
    assert row is not None
