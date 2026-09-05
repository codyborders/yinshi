"""Workspace projection API tests."""

from fastapi.testclient import TestClient


def test_list_workspaces_projects_delegation_metadata_per_repo(
    client: TestClient, git_repo: str
) -> None:
    """Workspace listing projects delegated metadata without crossing repositories."""
    from yinshi.db import get_db

    repo_one = client.post("/api/repos", json={"name": "repo-one", "local_path": git_repo}).json()
    repo_two = client.post("/api/repos", json={"name": "repo-two", "local_path": git_repo}).json()
    primary = client.post(f"/api/repos/{repo_one['id']}/workspaces", json={}).json()
    delegated = client.post(f"/api/repos/{repo_one['id']}/workspaces", json={}).json()
    other = client.post(f"/api/repos/{repo_two['id']}/workspaces", json={}).json()
    parent_session = client.post(f"/api/workspaces/{primary['id']}/sessions", json={}).json()

    with get_db() as db:
        db.execute(
            """UPDATE workspaces
               SET kind = 'delegated', parent_workspace_id = ?
               WHERE id = ?""",
            (primary["id"], delegated["id"]),
        )
        db.execute(
            """INSERT INTO thread_delegations (
                   id, parent_session_id, child_workspace_id, idempotency_key,
                   initiator, title, task, requested_model, status
               ) VALUES (?, ?, ?, ?, 'agent', ?, ?, ?, ?)""",
            (
                "delegation-one",
                parent_session["id"],
                delegated["id"],
                "delegation-key",
                "Child",
                "Task",
                "model",
                "running",
            ),
        )
        db.commit()

    repo_one_workspaces = client.get(f"/api/repos/{repo_one['id']}/workspaces").json()
    primary_projection = next(ws for ws in repo_one_workspaces if ws["id"] == primary["id"])
    assert primary_projection["kind"] == "primary"
    assert primary_projection["parent_workspace_id"] is None
    assert primary_projection["delegation_id"] is None
    assert primary_projection["delegation_status"] is None
    delegated_projection = next(ws for ws in repo_one_workspaces if ws["id"] == delegated["id"])
    assert delegated_projection["kind"] == "delegated"
    assert delegated_projection["parent_workspace_id"] == primary["id"]
    assert delegated_projection["delegation_id"] == "delegation-one"
    assert delegated_projection["delegation_status"] == "running"

    repo_two_workspaces = client.get(f"/api/repos/{repo_two['id']}/workspaces").json()
    assert [ws["id"] for ws in repo_two_workspaces] == [other["id"]]
    assert repo_two_workspaces[0]["delegation_id"] is None
    assert repo_two_workspaces[0]["delegation_status"] is None
