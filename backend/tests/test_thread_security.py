"""Thread security, ownership, and deletion guard tests."""

from __future__ import annotations

from tests.conftest import DEFAULT_TEST_HEADERS


def test_workspace_delete_rejected_when_session_is_delegated_parent(noauth_client, db):
    """Deleting a workspace whose session parents children fails with 409."""
    db.executescript("""
        INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'r', '/tmp/r');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('ws1', 'repo1', 'w', 'branch', '/tmp/r/w');
        INSERT INTO sessions (id, workspace_id) VALUES ('root1', 'ws1');
        INSERT INTO sessions (id, workspace_id) VALUES ('child1', 'ws1');
        INSERT INTO thread_delegations (
            id, parent_session_id, child_session_id, idempotency_key,
            initiator, title, task, requested_model, status
        ) VALUES (
            'del1', 'root1', 'child1', 'key1',
            'agent', 'Child', 'task', 'model', 'running'
        );
    """)
    db.commit()

    response = noauth_client.delete("/api/workspaces/ws1", headers=DEFAULT_TEST_HEADERS)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "thread_children_present"

    remaining = db.execute("SELECT count(*) FROM workspaces").fetchone()
    assert remaining is not None and remaining[0] == 1


def test_workspace_delete_guard_runs_before_any_teardown(noauth_client, db, monkeypatch):
    """A 409 deletion guard fires before cancellation or any teardown work."""
    import yinshi.api.workspaces as workspaces_module

    db.executescript("""
        INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'r', '/tmp/r');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('ws1', 'repo1', 'w', 'branch', '/tmp/r/w');
        INSERT INTO sessions (id, workspace_id) VALUES ('root1', 'ws1');
        INSERT INTO sessions (id, workspace_id) VALUES ('child1', 'ws1');
        INSERT INTO thread_delegations (
            id, parent_session_id, child_session_id, idempotency_key,
            initiator, title, task, requested_model, status
        ) VALUES (
            'del1', 'root1', 'child1', 'key1',
            'agent', 'Child', 'task', 'model', 'running'
        );
    """)
    db.commit()

    class ForbiddenCoordinator:
        def request_cancel(self, session_id):
            raise AssertionError("request_cancel must not run after a 409 guard")

    monkeypatch.setattr(
        workspaces_module,
        "get_run_coordinator",
        lambda: ForbiddenCoordinator(),
    )

    def forbidden_release(*args, **kwargs):
        raise AssertionError("release_sessions must not run after a 409 guard")

    monkeypatch.setattr(workspaces_module, "release_sessions", forbidden_release)

    response = noauth_client.delete("/api/workspaces/ws1", headers=DEFAULT_TEST_HEADERS)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "thread_children_present"


def test_legacy_thread_route_filters_cross_owner_descendants(noauth_client, db, monkeypatch):
    """The legacy route passes the caller email so foreign children vanish."""
    import yinshi.api.threads as threads_module

    db.executescript("""
        INSERT INTO repos (id, name, root_path, owner_email)
            VALUES ('repoA', 'a', '/tmp/rA', 'a@example.com');
        INSERT INTO repos (id, name, root_path, owner_email)
            VALUES ('repoB', 'b', '/tmp/rB', 'b@example.com');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('wsA', 'repoA', 'w', 'b', '/tmp/rA/w');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('wsB', 'repoB', 'w', 'b', '/tmp/rB/w');
        INSERT INTO sessions (id, workspace_id, title)
            VALUES ('sessA', 'wsA', 'Owned root');
        INSERT INTO sessions (id, workspace_id, title)
            VALUES ('sessB', 'wsB', 'Foreign child');
        INSERT INTO thread_delegations (
            id, parent_session_id, child_session_id, idempotency_key,
            initiator, title, task, requested_model, status
        ) VALUES (
            'del-x', 'sessA', 'sessB', 'kx',
            'agent', 'Smuggled', 'task', 'm', 'running'
        );
    """)
    db.commit()

    monkeypatch.setattr(threads_module, "get_user_email", lambda request: "a@example.com")

    response = noauth_client.get("/api/threads/sessA/children", headers=DEFAULT_TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_legacy_thread_route_rejects_cross_owner_parentage(noauth_client, db, monkeypatch):
    """A child owner cannot read a thread linked to another owner's parent."""
    import yinshi.api.threads as threads_module

    db.executescript("""
        INSERT INTO repos (id, name, root_path, owner_email)
            VALUES ('repoA', 'a', '/tmp/rA', 'a@example.com');
        INSERT INTO repos (id, name, root_path, owner_email)
            VALUES ('repoB', 'b', '/tmp/rB', 'b@example.com');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('wsA', 'repoA', 'w', 'b', '/tmp/rA/w');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('wsB', 'repoB', 'w', 'b', '/tmp/rB/w');
        INSERT INTO sessions (id, workspace_id, title)
            VALUES ('sessA', 'wsA', 'Foreign parent');
        INSERT INTO sessions (id, workspace_id, title)
            VALUES ('sessB', 'wsB', 'Owned child');
        INSERT INTO thread_delegations (
            id, parent_session_id, child_session_id, idempotency_key,
            initiator, title, task, requested_model, status
        ) VALUES (
            'del-x', 'sessA', 'sessB', 'kx',
            'agent', 'Owned child', 'task', 'm', 'running'
        );
    """)
    db.commit()

    monkeypatch.setattr(threads_module, "get_user_email", lambda request: "b@example.com")

    response = noauth_client.get("/api/threads/sessB", headers=DEFAULT_TEST_HEADERS)
    assert response.status_code == 404
    assert "sessA" not in response.text


def test_thread_routes_are_tenant_isolated(auth_client_factory):
    """One tenant cannot read another tenant's thread projections."""
    from yinshi.tenant import get_user_db

    owner = auth_client_factory(email="owner@example.com")
    other = auth_client_factory(email="other@example.com")

    owner_tenant = owner.yinshi_tenant
    with get_user_db(owner_tenant) as db:
        db.executescript("""
            INSERT INTO repos (id, name, root_path)
                VALUES ('repoA', 'r', '/tmp/rA');
            INSERT INTO workspaces (id, repo_id, name, branch, path)
                VALUES ('wsA', 'repoA', 'w', 'b', '/tmp/rA/w');
            INSERT INTO sessions (id, workspace_id, title)
                VALUES ('sessA', 'wsA', 'Secret root');
        """)
        db.commit()

    forbidden = other.get("/api/threads/sessA", headers=DEFAULT_TEST_HEADERS)
    assert forbidden.status_code == 404

    allowed = owner.get("/api/threads/sessA", headers=DEFAULT_TEST_HEADERS)
    assert allowed.status_code == 200
    assert allowed.json()["title"] == "Secret root"
    assert allowed.json()["can_spawn_child"] is True


def test_thread_routes_fail_closed_when_hierarchy_disabled(noauth_client, db, monkeypatch):
    """Disabling THREAD_HIERARCHY_ENABLED hides thread endpoints."""
    monkeypatch.setenv("THREAD_HIERARCHY_ENABLED", "false")
    from yinshi.config import get_settings

    get_settings.cache_clear()
    db.executescript("""
        INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'r', '/tmp/r');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('ws1', 'repo1', 'w', 'branch', '/tmp/r/w');
        INSERT INTO sessions (id, workspace_id) VALUES ('root1', 'ws1');
    """)
    db.commit()

    response = noauth_client.get("/api/threads/root1", headers=DEFAULT_TEST_HEADERS)
    assert response.status_code == 404
    get_settings.cache_clear()
