"""Thread API endpoint tests."""

from __future__ import annotations

from tests.conftest import DEFAULT_TEST_HEADERS

SEED_REPO_SQL = """
INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'r', '/tmp/r');
INSERT INTO workspaces (id, repo_id, name, branch, path)
    VALUES ('ws1', 'repo1', 'w', 'branch', '/tmp/r/w');
INSERT INTO sessions (id, workspace_id, title)
    VALUES ('root1', 'ws1', 'Root task');
INSERT INTO sessions (id, workspace_id, title)
    VALUES ('child1', 'ws1', 'Child task');
"""


def test_get_thread_endpoint_returns_root_projection(noauth_client, db):
    """GET /api/threads/{id} projects an existing session as a root thread."""
    db.executescript(SEED_REPO_SQL)
    db.commit()

    response = noauth_client.get("/api/threads/root1", headers=DEFAULT_TEST_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == "root1"
    assert body["parent_id"] is None
    assert body["root_id"] == "root1"
    assert body["depth"] == 0
    assert body["origin"] == "user"
    assert body["title"] == "Root task"


def test_get_thread_children_endpoint(noauth_client, db):
    """GET /api/threads/{id}/children returns direct child projections."""
    db.executescript(SEED_REPO_SQL)
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status, role
           ) VALUES (
               'del1', 'root1', 'child1', 'key1',
               'agent', 'Child task', 'do it', 'model', 'running', 'implementation'
           )""")
    db.commit()

    response = noauth_client.get("/api/threads/root1/children", headers=DEFAULT_TEST_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [child["id"] for child in body] == ["child1"]
    assert body[0]["parent_id"] == "root1"
    assert body[0]["state"] == "running"
    assert body[0]["delegation_id"] == "del1"


def test_get_thread_result_endpoint(noauth_client, db):
    """GET /api/threads/{id}/result returns the stored child result."""
    db.executescript(SEED_REPO_SQL)
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status, role
           ) VALUES (
               'del1', 'root1', 'child1', 'key1',
               'agent', 'Child task', 'do it', 'model', 'completed', 'implementation'
           )""")
    db.execute(
        "INSERT INTO thread_results (delegation_id, source, summary, sealed) "
        "VALUES ('del1', 'reported', 'Done', 1)"
    )
    db.commit()

    response = noauth_client.get("/api/threads/child1/result", headers=DEFAULT_TEST_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delegation_id"] == "del1"
    assert body["source"] == "reported"
    assert body["sealed"] is True
    assert body["summary"] == "Done"
    assert body["tests"] == []
    assert body["warnings"] == []
    assert body["changed_files"] == []


def test_get_thread_result_hides_unsealed(noauth_client, db):
    """GET /api/threads/{id}/result returns 404 while the result is unsealed."""
    db.executescript(SEED_REPO_SQL)
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'del1', 'root1', 'child1', 'key1',
               'agent', 'Child task', 'do it', 'model', 'running'
           )""")
    db.execute(
        "INSERT INTO thread_results (delegation_id, source, summary, sealed) "
        "VALUES ('del1', 'reported', 'Draft', 0)"
    )
    db.commit()

    response = noauth_client.get("/api/threads/child1/result", headers=DEFAULT_TEST_HEADERS)
    assert response.status_code == 404, response.text


def test_get_thread_limits_endpoint(noauth_client, db):
    """GET /api/threads/{id}/limits returns bounds, usage, and allowance."""
    db.executescript(SEED_REPO_SQL)
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status, role
           ) VALUES (
               'del1', 'root1', 'child1', 'key1',
               'agent', 'Child task', 'do it', 'model', 'running', 'implementation'
           )""")
    db.commit()

    response = noauth_client.get("/api/threads/root1/limits", headers=DEFAULT_TEST_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["max_depth"] == 1
    assert body["max_direct_children"] == 4
    assert body["max_active_descendants"] == 4
    assert body["max_total_threads"] == 20
    assert body["direct_children"] == 1
    assert body["active_descendants"] == 1
    assert body["total_threads"] == 2
    assert body["can_spawn_child"] is True


def test_session_apis_expose_additive_title(noauth_client, db):
    """Existing session APIs accept and return titles without breaking shape."""
    db.executescript(SEED_REPO_SQL)
    db.commit()

    created = noauth_client.post(
        "/api/workspaces/ws1/sessions",
        json={"model": "default", "title": "Fresh session"},
        headers=DEFAULT_TEST_HEADERS,
    )
    assert created.status_code == 201, created.text
    assert created.json()["title"] == "Fresh session"

    patched = noauth_client.patch(
        "/api/sessions/root1",
        json={"title": "Renamed root"},
        headers=DEFAULT_TEST_HEADERS,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "Renamed root"

    fetched = noauth_client.get("/api/sessions/root1", headers=DEFAULT_TEST_HEADERS)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["title"] == "Renamed root"
