"""POST /api/threads/{thread_id}/cancel API contract tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_thread_children_create_api import RecordingJournal, _post
from tests.test_thread_workspaces import seed_parent_stack


def _seed(noauth_client, db, git_repo, monkeypatch) -> None:
    """Seed one parent stack and install a recording journal on the app."""
    from yinshi.main import app

    seed_parent_stack(db, git_repo)
    monkeypatch.setattr(app.state, "prompt_journal", RecordingJournal())


def _spawn_child(client: TestClient) -> dict[str, object]:
    """Spawn one queued child through the public spawn API."""
    response = _post(client, "parent-session")
    assert response.status_code == 201, response.text
    return dict(response.json())


def test_cancel_attached_child_returns_stable_body(noauth_client, db, git_repo, monkeypatch):
    """Cancelling by child session ID returns the cancelled outcome twice."""
    _seed(noauth_client, db, git_repo, monkeypatch)
    spawned = _spawn_child(noauth_client)
    child_session_id = str(spawned["child_session_id"])

    first = noauth_client.post(
        f"/api/threads/{child_session_id}/cancel",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    second = noauth_client.post(
        f"/api/threads/{child_session_id}/cancel",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200
    assert second.json() == first.json()
    body = first.json()
    assert body["delegation_id"] == spawned["delegation_id"]
    assert body["status"] == "cancelled"
    assert body["child_session_id"] == child_session_id
    assert body["error_code"] is None
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned["delegation_id"],),
    ).fetchone()
    assert delegation["status"] == "cancelled"
    assert delegation["completed_at"] is not None
    assert delegation["child_workspace_id"] is not None


def test_cancel_provisioning_delegation_cleans_artifacts(noauth_client, db, git_repo, monkeypatch):
    """Cancelling by delegation ID resolves the pre-attach reservation."""
    from pathlib import Path

    _seed(noauth_client, db, git_repo, monkeypatch)
    spawned = _spawn_child(noauth_client)
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned["delegation_id"],),
    ).fetchone()
    workspace = db.execute(
        "SELECT * FROM workspaces WHERE id = ?",
        (delegation["child_workspace_id"],),
    ).fetchone()
    worktree_path = str(workspace["path"])
    db.execute(
        """UPDATE thread_delegations
           SET status = 'provisioning', child_session_id = NULL,
               child_workspace_id = NULL, base_kind = NULL, base_commit = NULL,
               snapshot_ref = NULL, started_at = NULL, completed_at = NULL
           WHERE id = ?""",
        (spawned["delegation_id"],),
    )
    db.execute("DELETE FROM sessions WHERE id = ?", (spawned["child_session_id"],))
    db.execute("DELETE FROM workspaces WHERE id = ?", (delegation["child_workspace_id"],))
    db.commit()

    response = noauth_client.post(
        f"/api/threads/{spawned['delegation_id']}/cancel",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["child_session_id"] is None
    assert not Path(worktree_path).exists()
    assert (
        db.execute("SELECT COUNT(*) AS n FROM workspaces WHERE kind = 'delegated'").fetchone()["n"]
        == 0
    )


def test_cancel_unknown_thread_returns_404(noauth_client, db, git_repo, monkeypatch):
    """Unknown threads and non-child sessions return the hidden 404 body."""
    _seed(noauth_client, db, git_repo, monkeypatch)

    missing = noauth_client.post(
        "/api/threads/missing-thread/cancel",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    root = noauth_client.post(
        "/api/threads/parent-session/cancel",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert missing.status_code == 404
    assert missing.json()["detail"] == "Session not found"
    assert root.status_code == 404
    assert root.json()["detail"] == "Session not found"


def test_cancel_hierarchy_disabled_returns_404(noauth_client, monkeypatch):
    """A disabled hierarchy flag maps the cancel route to 404."""
    from yinshi.config import get_settings

    monkeypatch.setenv("THREAD_HIERARCHY_ENABLED", "false")
    get_settings.cache_clear()
    try:
        response = noauth_client.post(
            "/api/threads/missing-thread/cancel",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Thread hierarchy is disabled"
    finally:
        get_settings.cache_clear()


def test_cancel_is_tenant_isolated(auth_client_factory, git_repo, monkeypatch):
    """One tenant cannot cancel another tenant's child thread."""
    import uuid as uuid_module

    from tests.factories import create_full_stack
    from yinshi.main import app

    monkeypatch.setattr(app.state, "prompt_journal", RecordingJournal())
    owner = auth_client_factory(email="cancel-owner@example.com")
    other = auth_client_factory(email="cancel-other@example.com")
    stack = create_full_stack(owner, git_repo, name="cancel-tenant")
    parent_session_id = stack["session"]["id"]

    spawned = owner.post(
        f"/api/threads/{parent_session_id}/children",
        json={
            "idempotency_key": str(uuid_module.uuid4()),
            "title": "Tenant child",
            "task": "Tenant task",
            "start_immediately": False,
        },
    )
    assert spawned.status_code == 201, spawned.text
    child_session_id = str(spawned.json()["child_session_id"])

    foreign = other.post(
        f"/api/threads/{child_session_id}/cancel",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert foreign.status_code == 404
    assert foreign.json()["detail"] == "Session not found"
