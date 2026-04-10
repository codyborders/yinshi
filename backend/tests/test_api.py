"""Tests for REST API endpoints including SSE streaming."""

import sqlite3
import subprocess
from collections import namedtuple
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.conftest import reset_rate_limiter
from tests.factories import create_full_stack, make_mock_sidecar, parse_sse_events

Entities = namedtuple("Entities", ["repo_id", "workspace_id", "session_id"])


def _seed_legacy_repo(
    legacy_db_path: str,
    *,
    email: str,
    repo_id: str,
    repo_name: str,
    repo_path: str,
) -> None:
    """Insert a legacy repo row that will be migrated on first tenant login."""
    legacy = sqlite3.connect(legacy_db_path)
    legacy.execute("PRAGMA foreign_keys = ON")
    legacy.execute(
        "INSERT INTO repos (id, name, remote_url, root_path, owner_email) "
        "VALUES (?, ?, ?, ?, ?)",
        (repo_id, repo_name, "https://github.com/example/project", repo_path, email),
    )
    legacy.commit()
    legacy.close()


def _seed_legacy_workspace_stack(
    legacy_db_path: str,
    *,
    repo_id: str,
    workspace_id: str,
    session_id: str,
    branch: str,
    workspace_path: str,
) -> None:
    """Insert a legacy workspace and session for prompt repair tests."""
    legacy = sqlite3.connect(legacy_db_path)
    legacy.execute("PRAGMA foreign_keys = ON")
    legacy.execute(
        "INSERT INTO workspaces (id, repo_id, name, branch, path, state) "
        "VALUES (?, ?, ?, ?, ?, 'ready')",
        (workspace_id, repo_id, branch, branch, workspace_path),
    )
    legacy.execute(
        "INSERT INTO sessions (id, workspace_id, status, model) VALUES (?, ?, 'idle', 'minimax')",
        (session_id, workspace_id),
    )
    legacy.commit()
    legacy.close()


@pytest.fixture
def test_entities(client: TestClient, git_repo: str) -> Entities:
    """Create a repo -> workspace -> session and return all IDs."""
    stack = create_full_stack(client, git_repo, name="test-repo")
    return Entities(
        stack["repo"]["id"],
        stack["workspace"]["id"],
        stack["session"]["id"],
    )


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


def test_tenant_local_import_clones_into_tenant_storage(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Tenant local imports should be copied into tenant storage when containers are on."""
    from yinshi.config import get_settings

    tenant = getattr(auth_client, "yinshi_tenant")
    settings = get_settings()
    original_container_enabled = settings.container_enabled
    settings.container_enabled = True

    try:
        response = auth_client.post(
            "/api/repos",
            json={"name": "tenant-repo", "local_path": git_repo},
        )
    finally:
        settings.container_enabled = original_container_enabled

    assert response.status_code == 201
    payload = response.json()
    assert payload["root_path"] == str(Path(tenant.data_dir) / "repos" / payload["id"])
    assert payload["root_path"] != git_repo
    assert Path(payload["root_path"]).is_dir()


def test_repo_response_excludes_owner_email(client: TestClient, git_repo: str) -> None:
    """Repo API responses should not leak owner_email."""
    resp = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "owner_email" not in data

    repo_id = data["id"]
    get_resp = client.get(f"/api/repos/{repo_id}")
    assert get_resp.status_code == 200
    assert "owner_email" not in get_resp.json()


def test_list_repos_includes_null_owner(
    db_path: str,
    git_repo: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repos with NULL owner_email should still appear when user is authenticated."""
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import get_db, init_control_db, init_db

    init_db()
    init_control_db()

    # Insert a repo with NULL owner_email (simulating pre-migration data)
    with get_db() as db:
        db.execute(
            "INSERT INTO repos (name, root_path, owner_email) VALUES (?, ?, NULL)",
            ("legacy-repo", git_repo),
        )
        db.commit()

    # Create a user in the control DB so the session token resolves
    from yinshi.services.accounts import resolve_or_create_user

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="google-test",
        email="user@example.com",
        display_name="Test",
    )

    from fastapi.testclient import TestClient

    from yinshi.auth import create_session_token
    from yinshi.main import app

    token = create_session_token(tenant.user_id)

    with TestClient(app) as client:
        client.cookies.set("yinshi_session", token)
        resp = client.get("/api/repos")
        assert resp.status_code == 200
        # In tenant mode, user gets their own empty DB -- repos are in user DB
        # Legacy repos in main DB are not visible in tenant mode
        # This is expected: tenant mode provides isolation

    get_settings.cache_clear()


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


def test_update_repo(client: TestClient, git_repo: str) -> None:
    """PATCH /api/repos/:id should update allowed fields."""
    create_resp = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    )
    repo_id = create_resp.json()["id"]

    resp = client.patch(
        f"/api/repos/{repo_id}",
        json={"name": "updated-name"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "updated-name"

    resp = client.patch(
        f"/api/repos/{repo_id}",
        json={"custom_prompt": "Be concise"},
    )
    assert resp.status_code == 200
    assert resp.json()["custom_prompt"] == "Be concise"


def test_update_repo_no_changes(client: TestClient, git_repo: str) -> None:
    """PATCH /api/repos/:id with empty body should return repo unchanged."""
    create_resp = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    )
    repo_id = create_resp.json()["id"]

    resp = client.patch(f"/api/repos/{repo_id}", json={})
    assert resp.status_code == 200
    assert resp.json()["name"] == "test-repo"


def test_update_repo_filters_to_updatable_columns(client: TestClient, git_repo: str) -> None:
    """PATCH /api/repos/:id filters to _UPDATABLE_COLUMNS before building SQL.

    The dict comprehension in update_repo already filters keys to
    _UPDATABLE_COLUMNS, so no secondary check is needed.
    """
    create_resp = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    )
    repo_id = create_resp.json()["id"]
    original = create_resp.json()

    resp = client.patch(
        f"/api/repos/{repo_id}",
        json={"name": "new-name", "custom_prompt": "be brief"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "new-name"
    assert data["custom_prompt"] == "be brief"
    assert data["root_path"] == original["root_path"]


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


def test_delete_repo_continues_on_workspace_failure(
    client: TestClient,
    git_repo: str,
) -> None:
    """Repo deletion should continue even if one workspace cleanup fails."""
    repo = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    ).json()
    workspaces = [
        client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json() for _ in range(3)
    ]
    attempted_workspace_ids: list[str] = []
    failure_workspace_id = workspaces[1]["id"]

    from yinshi.api import repos as repos_api

    original_delete_workspace = repos_api.delete_workspace

    async def flaky_delete_workspace(db, workspace_id: str) -> None:
        attempted_workspace_ids.append(workspace_id)
        if workspace_id == failure_workspace_id:
            raise RuntimeError("delete failed")
        await original_delete_workspace(db, workspace_id)

    with patch(
        "yinshi.api.repos.delete_workspace",
        side_effect=flaky_delete_workspace,
    ):
        resp = client.delete(f"/api/repos/{repo['id']}")

    assert resp.status_code == 204
    assert set(attempted_workspace_ids) == {ws["id"] for ws in workspaces}
    assert client.get(f"/api/repos/{repo['id']}").status_code == 404


def test_import_repo_rate_limit_returns_429(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Repo imports should be limited per authenticated user."""
    reset_rate_limiter()
    for index in range(10):
        response = auth_client.post(
            "/api/repos",
            json={"name": f"test-repo-{index}", "local_path": git_repo},
        )
        assert response.status_code == 201

    limited_response = auth_client.post(
        "/api/repos",
        json={"name": "test-repo-10", "local_path": git_repo},
    )

    assert limited_response.status_code == 429
    reset_rate_limiter()


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


def test_create_workspace_repairs_migrated_repo_paths(
    auth_client_factory,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant workspace creation should repair migrated legacy repo paths first."""
    from yinshi.config import get_settings

    repo_id = "repo-repair-1"
    email = "repair@example.com"
    monkeypatch.setenv("ALLOWED_REPO_BASE", "")
    get_settings.cache_clear()
    settings = get_settings()
    _seed_legacy_repo(
        settings.db_path,
        email=email,
        repo_id=repo_id,
        repo_name="legacy-repo",
        repo_path=git_repo,
    )

    auth_client = auth_client_factory(email=email, provider_user_id="repair-google")
    tenant = getattr(auth_client, "yinshi_tenant")

    resp = auth_client.post(f"/api/repos/{repo_id}/workspaces", json={})
    assert resp.status_code == 201
    workspace = resp.json()
    repaired_repo_path = str(Path(tenant.data_dir) / "repos" / repo_id)
    assert workspace["path"].startswith(repaired_repo_path)
    assert Path(workspace["path"]).is_dir()

    with sqlite3.connect(tenant.db_path) as user_db:
        row = user_db.execute(
            "SELECT root_path FROM repos WHERE id = ?",
            (repo_id,),
        ).fetchone()
    assert row == (repaired_repo_path,)


def test_create_workspace_repairs_from_local_checkout_when_github_auth_fails(
    auth_client_factory,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant repair should preserve local work even if GitHub auth is broken."""
    from yinshi.config import get_settings
    from yinshi.exceptions import GitHubInstallationUnusableError

    repo_id = "repo-repair-local-fallback"
    email = "repair-local@example.com"
    monkeypatch.setenv("ALLOWED_REPO_BASE", "")
    get_settings.cache_clear()
    settings = get_settings()
    _seed_legacy_repo(
        settings.db_path,
        email=email,
        repo_id=repo_id,
        repo_name="legacy-repo",
        repo_path=git_repo,
    )

    auth_client = auth_client_factory(email=email, provider_user_id="repair-local-google")
    tenant = getattr(auth_client, "yinshi_tenant")

    with patch(
        "yinshi.services.workspace.resolve_github_clone_access",
        new=AsyncMock(
            side_effect=GitHubInstallationUnusableError(
                "The connected GitHub installation is no longer usable."
            )
        ),
    ):
        resp = auth_client.post(f"/api/repos/{repo_id}/workspaces", json={})

    assert resp.status_code == 201
    repaired_repo_path = str(Path(tenant.data_dir) / "repos" / repo_id)
    assert resp.json()["path"].startswith(repaired_repo_path)
    assert Path(repaired_repo_path).is_dir()


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


def test_list_workspaces_nonexistent_repo_returns_404(client: TestClient) -> None:
    """GET /api/repos/:id/workspaces should 404 when the repo is missing."""
    resp = client.get("/api/repos/nonexistent/workspaces")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Repo not found"


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
    assert data["model"] == "anthropic/claude-sonnet-4-20250514"
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


def test_prompt_session_not_found(client: TestClient) -> None:
    """POST /api/sessions/:id/prompt with bad session should 404."""
    resp = client.post(
        "/api/sessions/nonexistent/prompt",
        json={"prompt": "hello"},
    )
    assert resp.status_code == 404


def test_prompt_rate_limit_returns_429(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Prompt submission should be limited per authenticated user."""
    from yinshi.api.stream import ExecutionContext

    stack = create_full_stack(auth_client, git_repo, name="prompt-rate-limit")
    session_id = stack["session"]["id"]

    async def fake_query(
        sid: str,
        prompt: str,
        model: str | None = None,
        cwd: str | None = None,
        api_key: str | None = None,
        agent_dir: str | None = None,
        settings_payload: dict[str, object] | None = None,
    ):
        yield {"type": "message", "data": {"type": "result", "usage": {}}}

    mock_sidecar = make_mock_sidecar(fake_query)

    reset_rate_limiter()
    with (
        patch(
            "yinshi.api.stream.create_sidecar_connection",
            return_value=mock_sidecar,
        ),
        patch(
            "yinshi.api.stream._resolve_execution_context",
            new=AsyncMock(
                return_value=ExecutionContext(
                    sidecar_socket=None,
                    effective_cwd="/tmp",
                    key_source="platform",
                    provider="test-provider",
                    provider_auth=None,
                    provider_config=None,
                    model_ref="minimax/MiniMax-M2.7",
                )
            ),
        ),
    ):
        for _ in range(120):
            response = auth_client.post(
                f"/api/sessions/{session_id}/prompt",
                json={"prompt": "rate limit test"},
            )
            assert response.status_code == 200
        limited_response = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "rate limit test"},
        )

    assert limited_response.status_code == 429
    reset_rate_limiter()


def test_prompt_rejects_none_provider(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Prompt requests should fail fast when model resolution returns no provider."""
    stack = create_full_stack(auth_client, git_repo, name="test-repo")

    async def unexpected_query(*args, **kwargs):
        if False:
            yield {}
        raise AssertionError("query should not be called")

    mock_sidecar = make_mock_sidecar(unexpected_query)
    mock_sidecar.resolve_model.return_value = {
        "provider": None,
        "model": "MiniMax-M2.7",
    }

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=mock_sidecar,
    ):
        resp = auth_client.post(
            f"/api/sessions/{stack['session']['id']}/prompt",
            json={"prompt": "say hello"},
        )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Could not determine provider for model"
    mock_sidecar.disconnect.assert_awaited_once()


def test_prompt_repairs_migrated_workspace_paths(
    auth_client_factory,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompting a migrated legacy session should repair repo and worktree paths."""
    from yinshi.config import get_settings

    repo_id = "repo-repair-2"
    workspace_id = "ws-repair-1"
    session_id = "sess-repair-1"
    branch = "legacy-feature"
    legacy_worktree_path = Path(git_repo) / ".worktrees" / branch
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(legacy_worktree_path)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    email = "prompt-repair@example.com"
    monkeypatch.setenv("ALLOWED_REPO_BASE", "")
    get_settings.cache_clear()
    settings = get_settings()
    _seed_legacy_repo(
        settings.db_path,
        email=email,
        repo_id=repo_id,
        repo_name="legacy-repo",
        repo_path=git_repo,
    )
    _seed_legacy_workspace_stack(
        settings.db_path,
        repo_id=repo_id,
        workspace_id=workspace_id,
        session_id=session_id,
        branch=branch,
        workspace_path=str(legacy_worktree_path),
    )

    auth_client = auth_client_factory(email=email, provider_user_id="prompt-repair-google")
    tenant = getattr(auth_client, "yinshi_tenant")
    from yinshi.db import get_control_db
    from yinshi.services.crypto import encrypt_api_key
    from yinshi.services.keys import get_user_dek

    dek = get_user_dek(tenant.user_id)
    encrypted_key = encrypt_api_key("sk-prompt-repair-minimax", dek)
    with get_control_db() as db:
        db.execute(
            "INSERT INTO api_keys (user_id, provider, encrypted_key) VALUES (?, ?, ?)",
            (tenant.user_id, "minimax", encrypted_key),
        )
        db.commit()

    async def fake_query(
        sid,
        prompt,
        model=None,
        cwd=None,
        provider_auth=None,
        provider_config=None,
        agent_dir=None,
        settings_payload=None,
    ):
        yield {
            "type": "message",
            "data": {"type": "result", "usage": {}},
        }

    mock_sidecar = make_mock_sidecar(fake_query)
    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=mock_sidecar,
    ):
        resp = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "repair the migrated workspace"},
        )

    assert resp.status_code == 200
    repaired_repo_path = str(Path(tenant.data_dir) / "repos" / repo_id)
    repaired_workspace_path = str(Path(repaired_repo_path) / ".worktrees" / branch)
    assert mock_sidecar.warmup.call_args.kwargs["cwd"] == repaired_workspace_path
    assert Path(repaired_workspace_path).is_dir()

    with sqlite3.connect(tenant.db_path) as user_db:
        repo_row = user_db.execute(
            "SELECT root_path FROM repos WHERE id = ?",
            (repo_id,),
        ).fetchone()
        workspace_row = user_db.execute(
            "SELECT path FROM workspaces WHERE id = ?",
            (workspace_id,),
        ).fetchone()

    assert repo_row == (repaired_repo_path,)
    assert workspace_row == (repaired_workspace_path,)


def test_prompt_streams_sidecar_events(client: TestClient, session_id: str) -> None:
    """POST /api/sessions/:id/prompt should stream SSE events and persist messages."""

    async def fake_query(
        sid,
        prompt,
        model=None,
        cwd=None,
        provider_auth=None,
        provider_config=None,
        agent_dir=None,
        settings_payload=None,
    ):
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
        return_value=make_mock_sidecar(fake_query),
    ):
        resp = client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "say hello"},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = parse_sse_events(resp.text)
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

    async def failing_query(
        sid,
        prompt,
        model=None,
        cwd=None,
        provider_auth=None,
        provider_config=None,
        agent_dir=None,
        settings_payload=None,
    ):
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
        return_value=make_mock_sidecar(failing_query),
    ):
        resp = client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "do stuff"},
        )

    assert resp.status_code == 200
    events = parse_sse_events(resp.text)
    # Should have an error event
    assert any(e.get("type") == "error" for e in events)

    # Partial assistant content should be saved
    msgs = client.get(f"/api/sessions/{session_id}/messages").json()
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert "partial" in assistant_msgs[0]["content"]


def test_prompt_persists_user_message_when_runtime_setup_fails(
    client: TestClient,
    session_id: str,
) -> None:
    """Prompt submission should survive runtime setup failures so history stays consistent."""
    with patch(
        "yinshi.api.stream._resolve_execution_context",
        side_effect=HTTPException(
            status_code=503,
            detail="Agent environment temporarily unavailable",
        ),
    ):
        response = client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "persist this prompt"},
        )

    assert response.status_code == 503

    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "persist this prompt"

    session = client.get(f"/api/sessions/{session_id}").json()
    assert session["status"] == "idle"


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
    repo = client.post("/api/repos", json={"name": "test-repo", "local_path": git_repo}).json()
    ws = client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json()
    sess = client.post(f"/api/workspaces/{ws['id']}/sessions", json={}).json()

    # Workspace name should equal branch initially
    assert ws["name"] == ws["branch"]

    async def fake_query(
        sid,
        prompt,
        model=None,
        cwd=None,
        provider_auth=None,
        provider_config=None,
        agent_dir=None,
        settings_payload=None,
    ):
        yield {
            "type": "message",
            "data": {"type": "result", "usage": {}},
        }

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=make_mock_sidecar(fake_query),
    ):
        client.post(
            f"/api/sessions/{sess['id']}/prompt",
            json={"prompt": "Fix the login page authentication bug"},
        )

    # Workspace name should now be updated
    updated_ws = client.get(f"/api/repos/{repo['id']}/workspaces").json()
    target = [w for w in updated_ws if w["id"] == ws["id"]][0]
    assert target["name"] != target["branch"]
    assert (
        "login" in target["name"].lower()
        or "auth" in target["name"].lower()
        or "fix" in target["name"].lower()
    )


def test_second_prompt_does_not_update_workspace_name(client: TestClient, git_repo: str) -> None:
    """Only the first prompt should update the workspace name."""
    repo = client.post("/api/repos", json={"name": "test-repo", "local_path": git_repo}).json()
    ws = client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json()
    sess = client.post(f"/api/workspaces/{ws['id']}/sessions", json={}).json()

    async def fake_query(
        sid,
        prompt,
        model=None,
        cwd=None,
        provider_auth=None,
        provider_config=None,
        agent_dir=None,
        settings_payload=None,
    ):
        yield {
            "type": "message",
            "data": {"type": "result", "usage": {}},
        }

    mock_sidecar = make_mock_sidecar(fake_query)

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
        return_value=make_mock_sidecar(fake_query),
    ):
        client.post(
            f"/api/sessions/{sess['id']}/prompt",
            json={"prompt": "Now add unit tests for everything"},
        )

    updated_ws = client.get(f"/api/repos/{repo['id']}/workspaces").json()
    target = [w for w in updated_ws if w["id"] == ws["id"]][0]
    assert target["name"] == name_after_first


def test_turn_id_index_exists(db_path: str, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The messages table should have an index on turn_id."""
    import sqlite3

    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
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


def test_import_github_repo_stores_installation_id(auth_client: TestClient) -> None:
    """GitHub imports should save the canonical URL and installation id."""
    from yinshi.services.github_app import GitHubCloneAccess
    from yinshi.tenant import get_user_db

    tenant = getattr(auth_client, "yinshi_tenant")
    with (
        patch(
            "yinshi.api.repos._resolve_clone_access",
            new=AsyncMock(
                return_value=GitHubCloneAccess(
                    clone_url="https://github.com/acme/private-repo.git",
                    repository_installation_id=12,
                    installation_id=12,
                    access_token="token-123",
                    manage_url="https://github.com/organizations/acme/settings/installations/12",
                )
            ),
        ),
        patch(
            "yinshi.api.repos.clone_repo",
            new=AsyncMock(return_value=str(Path(tenant.data_dir) / "repos" / "private-repo")),
        ),
    ):
        resp = auth_client.post(
            "/api/repos",
            json={
                "name": "private-repo",
                "remote_url": "git@github.com:acme/private-repo.git",
            },
        )

    assert resp.status_code == 201
    assert resp.json()["remote_url"] == "https://github.com/acme/private-repo.git"
    with get_user_db(tenant) as db:
        row = db.execute(
            "SELECT remote_url, installation_id FROM repos WHERE name = ?",
            ("private-repo",),
        ).fetchone()
    assert row is not None
    assert row["remote_url"] == "https://github.com/acme/private-repo.git"
    assert row["installation_id"] == 12


def test_import_public_github_repo_keeps_installation_id_null(auth_client: TestClient) -> None:
    """Public GitHub imports should stay anonymous even when the app is installed."""
    from yinshi.services.github_app import GitHubCloneAccess
    from yinshi.tenant import get_user_db

    tenant = getattr(auth_client, "yinshi_tenant")
    with (
        patch(
            "yinshi.api.repos._resolve_clone_access",
            new=AsyncMock(
                return_value=GitHubCloneAccess(
                    clone_url="https://github.com/acme/public-repo.git",
                    repository_installation_id=12,
                    installation_id=None,
                    access_token=None,
                    manage_url=None,
                )
            ),
        ),
        patch(
            "yinshi.api.repos.clone_repo",
            new=AsyncMock(return_value=str(Path(tenant.data_dir) / "repos" / "public-repo")),
        ),
    ):
        resp = auth_client.post(
            "/api/repos",
            json={
                "name": "public-repo",
                "remote_url": "https://github.com/acme/public-repo",
            },
        )

    assert resp.status_code == 201
    with get_user_db(tenant) as db:
        row = db.execute(
            "SELECT remote_url, installation_id FROM repos WHERE name = ?",
            ("public-repo",),
        ).fetchone()
    assert row is not None
    assert row["remote_url"] == "https://github.com/acme/public-repo.git"
    assert row["installation_id"] is None


def test_import_private_github_repo_returns_manage_error(auth_client: TestClient) -> None:
    """Private GitHub import failures should return structured manage guidance."""
    from yinshi.exceptions import GitError
    from yinshi.services.github_app import GitHubCloneAccess

    with (
        patch(
            "yinshi.api.repos._resolve_clone_access",
            new=AsyncMock(
                return_value=GitHubCloneAccess(
                    clone_url="https://github.com/acme/private-repo.git",
                    repository_installation_id=None,
                    installation_id=None,
                    access_token=None,
                    manage_url="https://github.com/organizations/acme/settings/installations/12",
                )
            ),
        ),
        patch(
            "yinshi.api.repos.clone_repo",
            new=AsyncMock(side_effect=GitError("git clone failed")),
        ),
    ):
        resp = auth_client.post(
            "/api/repos",
            json={
                "name": "private-repo",
                "remote_url": "https://github.com/acme/private-repo",
            },
        )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "github_access_not_granted"
    assert detail["manage_url"] == (
        "https://github.com/organizations/acme/settings/installations/12"
    )
    assert detail["connect_url"] is None


# --- _summarize_prompt unit tests ---


def test_summarize_prompt_basic() -> None:
    from yinshi.api.stream import _summarize_prompt

    assert _summarize_prompt("Fix the login page") == "fix-login-page"


def test_summarize_prompt_strips_filler() -> None:
    from yinshi.api.stream import _summarize_prompt

    assert _summarize_prompt("Can you fix the authentication bug") == "fix-authentication-bug"


def test_summarize_prompt_three_words_max() -> None:
    from yinshi.api.stream import _summarize_prompt

    result = _summarize_prompt("Refactor the database connection pool handling code")
    assert result == "refactor-database-connection"


def test_summarize_prompt_long() -> None:
    from yinshi.api.stream import _summarize_prompt

    result = _summarize_prompt("A" * 100)
    assert len(result) <= 50


def test_summarize_prompt_punctuation_only() -> None:
    from yinshi.api.stream import _summarize_prompt

    result = _summarize_prompt("...")
    assert result == "..."  # falls back to text[:30]


def test_summarize_prompt_empty() -> None:
    from yinshi.api.stream import _summarize_prompt

    result = _summarize_prompt("")
    assert result == ""


def test_summarize_prompt_short_input() -> None:
    from yinshi.api.stream import _summarize_prompt

    assert _summarize_prompt("auth") == "auth"
    assert _summarize_prompt("fix tests") == "fix-tests"


# --- Session PATCH and tree endpoint tests ---


def test_update_session_model(client: TestClient, test_entities: Entities) -> None:
    """PATCH /api/sessions/:id should update the model field."""
    resp = client.patch(
        f"/api/sessions/{test_entities.session_id}",
        json={"model": "sonnet"},
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "anthropic/claude-sonnet-4-20250514"

    # Verify it persisted
    get_resp = client.get(f"/api/sessions/{test_entities.session_id}")
    assert get_resp.json()["model"] == "anthropic/claude-sonnet-4-20250514"


def test_update_session_not_found(client: TestClient) -> None:
    """PATCH /api/sessions/:id with bad ID should 404."""
    resp = client.patch(
        "/api/sessions/nonexistent",
        json={"model": "sonnet"},
    )
    assert resp.status_code == 404


def test_update_session_no_changes(client: TestClient, test_entities: Entities) -> None:
    """PATCH /api/sessions/:id with empty body should return session unchanged."""
    resp = client.patch(
        f"/api/sessions/{test_entities.session_id}",
        json={},
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "minimax/MiniMax-M2.7"


def test_get_session_tree(client: TestClient, test_entities: Entities) -> None:
    """GET /api/sessions/:id/tree should return workspace file listing."""
    resp = client.get(f"/api/sessions/{test_entities.session_id}/tree")
    assert resp.status_code == 200
    data = resp.json()
    assert "files" in data
    # The test git repo has a README.md
    assert "README.md" in data["files"]


def test_session_tree_excludes_common_dirs(
    client: TestClient,
    test_entities: Entities,
) -> None:
    """GET /api/sessions/:id/tree should skip bulky generated directories."""
    from yinshi.db import get_db

    with get_db() as db:
        row = db.execute(
            "SELECT path FROM workspaces WHERE id = ?",
            (test_entities.workspace_id,),
        ).fetchone()

    assert row is not None
    workspace_path = Path(row["path"])
    included_file = workspace_path / "src" / "main.py"
    included_file.parent.mkdir(parents=True, exist_ok=True)
    included_file.write_text("print('ok')\n", encoding="utf-8")

    for excluded_dir in ("node_modules", ".venv", "__pycache__", "dist", "build"):
        excluded_file = workspace_path / excluded_dir / "ignored.txt"
        excluded_file.parent.mkdir(parents=True, exist_ok=True)
        excluded_file.write_text("ignore me\n", encoding="utf-8")

    resp = client.get(f"/api/sessions/{test_entities.session_id}/tree")
    assert resp.status_code == 200
    files = resp.json()["files"]
    assert "src/main.py" in files
    assert "node_modules/ignored.txt" not in files
    assert ".venv/ignored.txt" not in files
    assert "__pycache__/ignored.txt" not in files
    assert "dist/ignored.txt" not in files
    assert "build/ignored.txt" not in files


def test_session_tree_limits_file_count(
    client: TestClient,
    test_entities: Entities,
) -> None:
    """GET /api/sessions/:id/tree should cap the file list at 5000 entries."""
    from yinshi.db import get_db

    with get_db() as db:
        row = db.execute(
            "SELECT path FROM workspaces WHERE id = ?",
            (test_entities.workspace_id,),
        ).fetchone()

    assert row is not None
    workspace_path = Path(row["path"])
    for index in range(5005):
        file_path = workspace_path / f"{index:04}.txt"
        file_path.write_text("x\n", encoding="utf-8")

    resp = client.get(f"/api/sessions/{test_entities.session_id}/tree")
    assert resp.status_code == 200
    files = resp.json()["files"]
    assert len(files) == 5000
    assert "0000.txt" in files
    assert "5004.txt" not in files


def test_get_session_tree_not_found(client: TestClient) -> None:
    """GET /api/sessions/:id/tree with bad ID should 404."""
    resp = client.get("/api/sessions/nonexistent/tree")
    assert resp.status_code == 404


# --- Workspace PATCH endpoint tests ---


def test_archive_workspace(client: TestClient, test_entities: Entities) -> None:
    """PATCH /api/workspaces/:id should archive a workspace."""
    resp = client.patch(
        f"/api/workspaces/{test_entities.workspace_id}",
        json={"state": "archived"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "archived"

    # Verify persistence
    ws_list = client.get(f"/api/repos/{test_entities.repo_id}/workspaces").json()
    target = [w for w in ws_list if w["id"] == test_entities.workspace_id][0]
    assert target["state"] == "archived"


def test_unarchive_workspace(client: TestClient, test_entities: Entities) -> None:
    """PATCH /api/workspaces/:id should restore an archived workspace."""
    client.patch(
        f"/api/workspaces/{test_entities.workspace_id}",
        json={"state": "archived"},
    )
    resp = client.patch(
        f"/api/workspaces/{test_entities.workspace_id}",
        json={"state": "ready"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "ready"


def test_update_workspace_not_found(client: TestClient) -> None:
    """PATCH /api/workspaces/:id with bad ID should 404."""
    resp = client.patch(
        "/api/workspaces/nonexistent",
        json={"state": "archived"},
    )
    assert resp.status_code == 404


def test_update_workspace_no_changes(client: TestClient, test_entities: Entities) -> None:
    """PATCH /api/workspaces/:id with empty body should return workspace unchanged."""
    resp = client.patch(
        f"/api/workspaces/{test_entities.workspace_id}",
        json={},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "ready"


def test_update_workspace_invalid_state(client: TestClient, test_entities: Entities) -> None:
    """PATCH /api/workspaces/:id with invalid state should 422."""
    resp = client.patch(
        f"/api/workspaces/{test_entities.workspace_id}",
        json={"state": "bogus"},
    )
    assert resp.status_code == 422
