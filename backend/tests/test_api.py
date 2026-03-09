"""Tests for REST API endpoints."""

import os

import pytest


@pytest.fixture
def client(db_path, monkeypatch):
    """Create a test client with initialized DB."""
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db

    init_db()

    from yinshi.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()


def test_health_endpoint(client):
    """GET /health should return ok."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_list_repos_empty(client):
    """GET /api/repos should return empty list initially."""
    resp = client.get("/api/repos")
    assert resp.status_code == 200
    assert resp.json() == []


def test_import_local_repo(client, git_repo):
    """POST /api/repos should import a local repo."""
    resp = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "test-repo"
    assert data["root_path"] == git_repo
    assert data["id"]


def test_import_repo_invalid_path(client, tmp_path):
    """POST /api/repos with invalid path should fail."""
    resp = client.post(
        "/api/repos",
        json={"name": "bad-repo", "local_path": str(tmp_path / "nonexistent")},
    )
    assert resp.status_code == 400


def test_get_repo(client, git_repo):
    """GET /api/repos/:id should return the repo."""
    create_resp = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    )
    repo_id = create_resp.json()["id"]

    resp = client.get(f"/api/repos/{repo_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == repo_id


def test_get_repo_not_found(client):
    """GET /api/repos/:id with bad ID should 404."""
    resp = client.get("/api/repos/nonexistent")
    assert resp.status_code == 404


def test_delete_repo(client, git_repo):
    """DELETE /api/repos/:id should remove the repo."""
    create_resp = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    )
    repo_id = create_resp.json()["id"]

    resp = client.delete(f"/api/repos/{repo_id}")
    assert resp.status_code == 204

    resp = client.get(f"/api/repos/{repo_id}")
    assert resp.status_code == 404


def test_create_workspace(client, git_repo):
    """POST /api/repos/:id/workspaces should create a worktree."""
    create_resp = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    )
    repo_id = create_resp.json()["id"]

    resp = client.post(f"/api/repos/{repo_id}/workspaces", json={})
    assert resp.status_code == 201
    data = resp.json()
    assert data["repo_id"] == repo_id
    assert data["branch"]
    assert data["state"] == "ready"


def test_list_workspaces(client, git_repo):
    """GET /api/repos/:id/workspaces should list workspaces."""
    create_resp = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    )
    repo_id = create_resp.json()["id"]

    client.post(f"/api/repos/{repo_id}/workspaces", json={})
    client.post(f"/api/repos/{repo_id}/workspaces", json={})

    resp = client.get(f"/api/repos/{repo_id}/workspaces")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_create_session(client, git_repo):
    """POST /api/workspaces/:id/sessions should create a session."""
    repo_resp = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    )
    repo_id = repo_resp.json()["id"]

    ws_resp = client.post(f"/api/repos/{repo_id}/workspaces", json={})
    ws_id = ws_resp.json()["id"]

    resp = client.post(f"/api/workspaces/{ws_id}/sessions", json={"model": "sonnet"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["workspace_id"] == ws_id
    assert data["model"] == "sonnet"
    assert data["status"] == "idle"


def test_list_sessions(client, git_repo):
    """GET /api/workspaces/:id/sessions should list sessions."""
    repo_resp = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    )
    repo_id = repo_resp.json()["id"]

    ws_resp = client.post(f"/api/repos/{repo_id}/workspaces", json={})
    ws_id = ws_resp.json()["id"]

    client.post(f"/api/workspaces/{ws_id}/sessions", json={})

    resp = client.get(f"/api/workspaces/{ws_id}/sessions")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_get_session_messages(client, git_repo):
    """GET /api/sessions/:id/messages should return messages."""
    repo_resp = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    )
    repo_id = repo_resp.json()["id"]

    ws_resp = client.post(f"/api/repos/{repo_id}/workspaces", json={})
    ws_id = ws_resp.json()["id"]

    sess_resp = client.post(f"/api/workspaces/{ws_id}/sessions", json={})
    sess_id = sess_resp.json()["id"]

    resp = client.get(f"/api/sessions/{sess_id}/messages")
    assert resp.status_code == 200
    assert resp.json() == []
