"""POST /api/threads/{child_session_id}/retry API contract tests."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.test_thread_workspaces import seed_parent_stack
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
            id=uuid.uuid4().hex,
            session_id=str(kwargs["session_id"]),
            status="starting",
        )


def _seed_failed_child(db, git_repo) -> str:
    """Seed one failed delegated child and return its session ID."""
    seed_parent_stack(db, git_repo)
    db.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path, state, kind,
                                   parent_workspace_id)
           VALUES ('orig-ws', 'repo1', 'orig', 'orig-branch', '/tmp/orig-ws',
                   'ready', 'delegated', 'parent-ws')""",
    )
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('orig-child', 'orig-ws')")
    db.execute(
        """INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, child_workspace_id,
               idempotency_key, initiator, title, task, context, role,
               requested_model, requested_thinking, status,
               error_code, error_detail_safe
           ) VALUES (
               'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'parent-session', 'orig-child',
               'orig-ws', 'orig-key', 'user', 'Original child',
               'Original task text.', 'Original context.', 'implementation',
               'model-x', 'high', 'failed', 'start_failed',
               'initial prompt run failed to start'
           )""",
    )
    db.commit()
    return "orig-child"


def _post(client: TestClient, child_session_id: str, **overrides: object):
    payload: dict[str, object] = {"idempotency_key": str(uuid.uuid4())}
    payload.update(overrides)
    return client.post(
        f"/api/threads/{child_session_id}/retry",
        json=payload,
        headers={"X-Requested-With": "XMLHttpRequest"},
    )


def test_retry_api_returns_running_lineage_child(noauth_client, db, git_repo, monkeypatch):
    """One retry POST spawns a running child linked to the failed original."""
    from yinshi.main import app

    child_session_id = _seed_failed_child(db, git_repo)
    journal = RecordingJournal()
    monkeypatch.setattr(app.state, "prompt_journal", journal)

    response = _post(noauth_client, child_session_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "running"
    assert body["child_session_id"]
    delegation = db.execute(
        "SELECT retry_of_delegation_id, title FROM thread_delegations WHERE id = ?",
        (body["delegation_id"],),
    ).fetchone()
    assert delegation["retry_of_delegation_id"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert delegation["title"] == "Original child"


def test_retry_api_replay_returns_same_body(noauth_client, db, git_repo, monkeypatch):
    """One replayed retry POST returns the identical body and starts once."""
    from yinshi.main import app

    child_session_id = _seed_failed_child(db, git_repo)
    journal = RecordingJournal()
    monkeypatch.setattr(app.state, "prompt_journal", journal)
    key = str(uuid.uuid4())

    first = _post(noauth_client, child_session_id, idempotency_key=key)
    second = _post(noauth_client, child_session_id, idempotency_key=key)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    assert len(journal.starts) == 1


def test_retry_api_invalid_status_returns_409(noauth_client, db, git_repo, monkeypatch):
    """A non-terminal child maps to 409 with the safe retry code."""
    from yinshi.main import app

    child_session_id = _seed_failed_child(db, git_repo)
    monkeypatch.setattr(app.state, "prompt_journal", RecordingJournal())
    db.execute(
        "UPDATE thread_delegations SET status = 'queued' WHERE child_session_id = ?",
        (child_session_id,),
    )
    db.commit()

    response = _post(noauth_client, child_session_id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "retry_not_allowed"
