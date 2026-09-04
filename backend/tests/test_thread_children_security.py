"""Ownership checks for direct child thread reads."""

from __future__ import annotations

from tests.conftest import DEFAULT_TEST_HEADERS


def test_legacy_children_route_rejects_cross_owner_parentage(noauth_client, db, monkeypatch):
    """A child owner cannot traverse from a foreign parent relationship."""
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
        INSERT INTO sessions (id, workspace_id) VALUES ('sessA', 'wsA');
        INSERT INTO sessions (id, workspace_id) VALUES ('sessB', 'wsB');
        INSERT INTO thread_delegations (
            id, parent_session_id, child_session_id, idempotency_key,
            initiator, title, task, requested_model, status
        ) VALUES (
            'del-x', 'sessA', 'sessB', 'kx',
            'agent', 'Child', 'task', 'm', 'running'
        );
    """)
    db.commit()

    monkeypatch.setattr(threads_module, "get_user_email", lambda request: "b@example.com")

    response = noauth_client.get(
        "/api/threads/sessB/children",
        headers=DEFAULT_TEST_HEADERS,
    )
    assert response.status_code == 404
    assert "sessA" not in response.text
