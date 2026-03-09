"""Tests for REST API endpoints including SSE streaming."""

import json
from collections import namedtuple
from collections.abc import Iterator
from typing import Any, Callable
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

Entities = namedtuple("Entities", ["repo_id", "workspace_id", "session_id"])


@pytest.fixture
def client(db_path: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Create a test client with initialized DB."""
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db

    init_db()

    from yinshi.main import app

    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()


@pytest.fixture
def test_entities(client: TestClient, git_repo: str) -> Entities:
    """Create a repo -> workspace -> session and return all IDs."""
    repo = client.post(
        "/api/repos", json={"name": "test-repo", "local_path": git_repo}
    ).json()
    ws = client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json()
    sess = client.post(f"/api/workspaces/{ws['id']}/sessions", json={}).json()
    return Entities(repo["id"], ws["id"], sess["id"])


@pytest.fixture
def session_id(test_entities: Entities) -> str:
    """Create a repo -> workspace -> session and return the session ID."""
    return test_entities.session_id


def test_health_endpoint(client: TestClient) -> None:
    """GET /health should return ok."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_list_repos_empty(client: TestClient) -> None:
    """GET /api/repos should return empty list initially."""
    resp = client.get("/api/repos")
    assert resp.status_code == 200
    assert resp.json() == []


def test_import_local_repo(client: TestClient, git_repo: str) -> None:
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


def test_import_repo_invalid_path(client: TestClient, tmp_path) -> None:
    """POST /api/repos with invalid path should fail."""
    resp = client.post(
        "/api/repos",
        json={"name": "bad-repo", "local_path": str(tmp_path / "nonexistent")},
    )
    assert resp.status_code == 400


def test_get_repo(client: TestClient, git_repo: str) -> None:
    """GET /api/repos/:id should return the repo."""
    create_resp = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    )
    repo_id = create_resp.json()["id"]

    resp = client.get(f"/api/repos/{repo_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == repo_id


def test_get_repo_not_found(client: TestClient) -> None:
    """GET /api/repos/:id with bad ID should 404."""
    resp = client.get("/api/repos/nonexistent")
    assert resp.status_code == 404


def test_delete_repo(client: TestClient, git_repo: str) -> None:
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


def test_create_workspace(client: TestClient, git_repo: str) -> None:
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


def test_list_workspaces(client: TestClient, git_repo: str) -> None:
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


def test_create_session(client: TestClient, git_repo: str) -> None:
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


def test_list_sessions(client: TestClient, git_repo: str) -> None:
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


def test_get_session_messages(client: TestClient, git_repo: str) -> None:
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


# --- SSE prompt endpoint tests ---


def _parse_sse_events(response_text: str) -> list[dict[str, Any]]:
    """Parse SSE text/event-stream body into list of JSON objects."""
    events = []
    for line in response_text.strip().split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def test_prompt_session_not_found(client: TestClient) -> None:
    """POST /api/sessions/:id/prompt with bad session should 404."""
    resp = client.post(
        "/api/sessions/nonexistent/prompt",
        json={"prompt": "hello"},
    )
    assert resp.status_code == 404


def _make_mock_sidecar(query_fn: Callable[..., Any]) -> AsyncMock:
    """Build a mock SidecarClient with the given query async generator."""
    mock = AsyncMock()
    mock.query = query_fn
    mock.warmup = AsyncMock()
    mock.disconnect = AsyncMock()
    return mock


def test_prompt_streams_sidecar_events(client: TestClient, session_id: str) -> None:
    """POST /api/sessions/:id/prompt should stream SSE events and persist messages."""

    async def fake_query(sid, prompt, model, cwd):
        yield {
            "type": "message",
            "data": {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello world"}]},
            },
        }
        yield {
            "type": "message",
            "data": {"type": "result", "usage": {}},
        }

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=_make_mock_sidecar(fake_query),
    ):
        resp = client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "say hello"},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse_events(resp.text)
    types = [e.get("type") for e in events]
    assert "assistant" in types
    assert "result" in types

    # Verify user + assistant messages persisted
    msgs = client.get(f"/api/sessions/{session_id}/messages").json()
    roles = [m["role"] for m in msgs]
    assert "user" in roles
    assert "assistant" in roles


def test_prompt_saves_partial_on_sidecar_error(client: TestClient, session_id: str) -> None:
    """If the sidecar errors mid-stream, partial content is still saved."""

    async def failing_query(sid, prompt, model, cwd):
        yield {
            "type": "message",
            "data": {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "partial "}]},
            },
        }
        raise ConnectionError("sidecar died")

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=_make_mock_sidecar(failing_query),
    ):
        resp = client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "do stuff"},
        )

    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)
    # Should have an error event
    assert any(e.get("type") == "error" for e in events)

    # Partial assistant content should be saved
    msgs = client.get(f"/api/sessions/{session_id}/messages").json()
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert "partial" in assistant_msgs[0]["content"]


def test_cancel_session_not_found(client: TestClient) -> None:
    """POST /api/sessions/:id/cancel with no active session returns 404."""
    resp = client.post("/api/sessions/nonexistent/cancel")
    assert resp.status_code == 404


def test_cancel_no_active_stream(client: TestClient, session_id: str) -> None:
    """POST /api/sessions/:id/cancel with no active stream returns 409."""
    resp = client.post(f"/api/sessions/{session_id}/cancel")
    assert resp.status_code == 409


def test_first_prompt_updates_workspace_name(client: TestClient, git_repo: str) -> None:
    """The first prompt should update the workspace name to a summary of the prompt."""
    repo = client.post(
        "/api/repos", json={"name": "test-repo", "local_path": git_repo}
    ).json()
    ws = client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json()
    sess = client.post(f"/api/workspaces/{ws['id']}/sessions", json={}).json()

    # Workspace name should equal branch initially
    assert ws["name"] == ws["branch"]

    async def fake_query(sid, prompt, model, cwd):
        yield {
            "type": "message",
            "data": {"type": "result", "usage": {}},
        }

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=_make_mock_sidecar(fake_query),
    ):
        client.post(
            f"/api/sessions/{sess['id']}/prompt",
            json={"prompt": "Fix the login page authentication bug"},
        )

    # Workspace name should now be updated
    updated_ws = client.get(f"/api/repos/{repo['id']}/workspaces").json()
    target = [w for w in updated_ws if w["id"] == ws["id"]][0]
    assert target["name"] != target["branch"]
    assert "login" in target["name"].lower() or "auth" in target["name"].lower() or "fix" in target["name"].lower()


def test_second_prompt_does_not_update_workspace_name(client: TestClient, git_repo: str) -> None:
    """Only the first prompt should update the workspace name."""
    repo = client.post(
        "/api/repos", json={"name": "test-repo", "local_path": git_repo}
    ).json()
    ws = client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json()
    sess = client.post(f"/api/workspaces/{ws['id']}/sessions", json={}).json()

    async def fake_query(sid, prompt, model, cwd):
        yield {
            "type": "message",
            "data": {"type": "result", "usage": {}},
        }

    mock_sidecar = _make_mock_sidecar(fake_query)

    # First prompt
    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=mock_sidecar,
    ):
        client.post(
            f"/api/sessions/{sess['id']}/prompt",
            json={"prompt": "Fix the login page"},
        )

    updated_ws = client.get(f"/api/repos/{repo['id']}/workspaces").json()
    target = [w for w in updated_ws if w["id"] == ws["id"]][0]
    name_after_first = target["name"]

    # Second prompt -- name should NOT change
    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=_make_mock_sidecar(fake_query),
    ):
        client.post(
            f"/api/sessions/{sess['id']}/prompt",
            json={"prompt": "Now add unit tests for everything"},
        )

    updated_ws = client.get(f"/api/repos/{repo['id']}/workspaces").json()
    target = [w for w in updated_ws if w["id"] == ws["id"]][0]
    assert target["name"] == name_after_first


def test_turn_id_index_exists(db_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The messages table should have an index on turn_id."""
    import sqlite3

    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    from yinshi.config import get_settings

    get_settings.cache_clear()
    from yinshi.db import init_db

    init_db()

    conn = sqlite3.connect(db_path)
    indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages'"
    ).fetchall()
    index_names = [row[0] for row in indexes]
    conn.close()
    assert "idx_messages_turn_id" in index_names
    get_settings.cache_clear()


def test_prompt_concurrent_rejects(client: TestClient, session_id: str) -> None:
    """POST /api/sessions/:id/prompt should reject if session is already running."""
    # Manually set session to running
    from yinshi.db import get_db

    with get_db() as db:
        db.execute("UPDATE sessions SET status = 'running' WHERE id = ?", (session_id,))
        db.commit()

    resp = client.post(
        f"/api/sessions/{session_id}/prompt",
        json={"prompt": "hello"},
    )
    assert resp.status_code == 409

    # Reset status so cleanup doesn't fail
    with get_db() as db:
        db.execute("UPDATE sessions SET status = 'idle' WHERE id = ?", (session_id,))
        db.commit()


def test_git_url_validation(client: TestClient) -> None:
    """Dangerous git URL schemes should be rejected."""
    # ext:: scheme
    resp = client.post(
        "/api/repos",
        json={"name": "evil-repo", "remote_url": "ext::sh -c evil"},
    )
    assert resp.status_code == 400

    # file:// scheme
    resp = client.post(
        "/api/repos",
        json={"name": "evil-repo", "remote_url": "file:///etc/passwd"},
    )
    assert resp.status_code == 400

    # Argument injection
    resp = client.post(
        "/api/repos",
        json={"name": "evil-repo", "remote_url": "--upload-pack=evil"},
    )
    assert resp.status_code == 400


# --- _summarize_prompt unit tests ---


def test_summarize_prompt_basic() -> None:
    from yinshi.api.stream import _summarize_prompt

    assert _summarize_prompt("Fix the login page") == "Fix the login page"


def test_summarize_prompt_long() -> None:
    from yinshi.api.stream import _summarize_prompt

    result = _summarize_prompt("A" * 100)
    assert len(result) <= 50


def test_summarize_prompt_punctuation_only() -> None:
    from yinshi.api.stream import _summarize_prompt

    result = _summarize_prompt("...")
    assert result == "..."  # falls back to prompt[:max_len]


def test_summarize_prompt_empty() -> None:
    from yinshi.api.stream import _summarize_prompt

    result = _summarize_prompt("")
    assert result == ""
