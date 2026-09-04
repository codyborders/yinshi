"""Model data in read-only thread projections."""

from __future__ import annotations

from tests.conftest import DEFAULT_TEST_HEADERS


def test_thread_projection_includes_session_model(noauth_client, db):
    """Thread metadata returns the selected session model."""
    db.executescript("""
        INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'r', '/tmp/r');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('ws1', 'repo1', 'w', 'branch', '/tmp/r/w');
        INSERT INTO sessions (id, workspace_id, model)
            VALUES ('root1', 'ws1', 'provider/example-model');
    """)
    db.commit()

    response = noauth_client.get("/api/threads/root1", headers=DEFAULT_TEST_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "provider/example-model"
