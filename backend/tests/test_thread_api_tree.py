"""Thread API tree endpoint tests."""

from __future__ import annotations

from tests.conftest import DEFAULT_TEST_HEADERS

SEED_TREE_SQL = """
INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'r', '/tmp/r');
INSERT INTO workspaces (id, repo_id, name, branch, path)
    VALUES ('ws1', 'repo1', 'w', 'branch', '/tmp/r/w');
INSERT INTO sessions (id, workspace_id, title)
    VALUES ('root1', 'ws1', 'Root task');
INSERT INTO sessions (id, workspace_id, title)
    VALUES ('child1', 'ws1', 'Child task');
INSERT INTO thread_delegations (
    id, parent_session_id, child_session_id, idempotency_key,
    initiator, title, task, requested_model, status, role
) VALUES (
    'del1', 'root1', 'child1', 'key1',
    'agent', 'Child task', 'do it', 'model', 'running', 'implementation'
);
INSERT INTO thread_delegations (
    id, parent_session_id, idempotency_key,
    initiator, title, task, requested_model, status
) VALUES (
    'del2', 'root1', 'key2',
    'user', 'Pending child', 'task', 'model', 'provisioning'
);
"""


def test_get_thread_tree_endpoint_returns_full_tree(noauth_client, db):
    """GET /api/threads/{id}/tree returns root, nodes, and placeholders."""
    db.executescript(SEED_TREE_SQL)
    db.commit()

    response = noauth_client.get("/api/threads/root1/tree", headers=DEFAULT_TEST_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["root"]["id"] == "root1"
    assert [node["id"] for node in body["nodes"]] == ["child1"]
    assert [p["delegation_id"] for p in body["placeholders"]] == ["del2"]
    # Reserved placeholder threads count toward tree totals.
    assert body["thread_count"] == 3
    assert body["active_descendant_count"] == 2
