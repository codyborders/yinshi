"""Tests for REST API endpoints including SSE streaming."""

import asyncio
import json
import logging
import sqlite3
import subprocess
from collections import namedtuple
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.conftest import reset_rate_limiter
from tests.factories import create_full_stack, make_mock_sidecar, parse_sse_events
from yinshi.config import get_settings

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


def test_untrusted_host_is_rejected(client: TestClient) -> None:
    """Requests with an unconfigured Host header must fail before routing."""
    response = client.get("/health", headers={"Host": "attacker.example"})

    assert response.status_code == 400
    assert response.text == "Invalid host header"


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
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "disabled")
    monkeypatch.setenv("REQUIRE_HTTPS", "disabled")
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


def test_worktree_failure_preserves_tenant_runtime_and_database_rows(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Failed Git cleanup must leave runtime data and records retryable."""
    from yinshi.exceptions import GitError
    from yinshi.main import app
    from yinshi.services.sidecar_runtime import _workspace_home_source
    from yinshi.tenant import get_user_db

    stack = create_full_stack(auth_client, git_repo, name="failed-worktree-delete")
    workspace_id = str(stack["workspace"]["id"])
    session_id = str(stack["session"]["id"])
    tenant = getattr(auth_client, "yinshi_tenant")
    home_path = Path(_workspace_home_source(tenant, workspace_id))
    marker_path = home_path / "private-runtime-state"
    marker_path.write_text("keep", encoding="utf-8")
    container_manager = AsyncMock()
    container_manager.destroy_container.return_value = True
    app.state.container_manager = container_manager

    with patch(
        "yinshi.services.workspace.delete_worktree",
        new=AsyncMock(side_effect=GitError("forced worktree failure")),
    ):
        response = auth_client.delete(f"/api/workspaces/{workspace_id}")

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to delete workspace"}
    assert marker_path.read_text(encoding="utf-8") == "keep"
    with get_user_db(tenant) as database:
        workspace_row = database.execute(
            "SELECT id FROM workspaces WHERE id = ?",
            (workspace_id,),
        ).fetchone()
        session_row = database.execute(
            "SELECT id FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    assert workspace_row is not None
    assert session_row is not None

    retry_response = auth_client.delete(f"/api/workspaces/{workspace_id}")

    assert retry_response.status_code == 204
    assert not home_path.exists()


def test_delete_workspace_returns_retryable_conflict_while_container_is_busy(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Requested cancellation should preserve workspace state until runtime stops."""
    from yinshi.auth import get_session_identity
    from yinshi.db import get_control_db
    from yinshi.main import app
    from yinshi.services.accounts import make_tenant
    from yinshi.services.sidecar_runtime import _workspace_home_source

    stack = create_full_stack(auth_client, git_repo, name="busy-workspace-delete")
    workspace_id = stack["workspace"]["id"]
    workspace_path = Path(stack["workspace"]["path"])
    session_token = auth_client.cookies.get("yinshi_session")
    assert session_token is not None
    identity = get_session_identity(session_token)
    assert identity is not None
    with get_control_db() as db:
        user = db.execute("SELECT email FROM users WHERE id = ?", (identity[0],)).fetchone()
    assert user is not None
    tenant = make_tenant(identity[0], user["email"])
    home_path = Path(_workspace_home_source(tenant, workspace_id))
    marker_path = home_path / "private.txt"
    marker_path.write_text("private", encoding="utf-8")

    container_manager = AsyncMock()
    container_manager.destroy_container.return_value = False
    coordinator = AsyncMock()
    coordinator.request_cancel.return_value = True
    app.state.container_manager = container_manager

    with patch("yinshi.api.workspaces.get_run_coordinator", return_value=coordinator):
        response = auth_client.delete(f"/api/workspaces/{workspace_id}")

    assert response.status_code == 409
    assert response.json() == {"detail": "Workspace is still stopping; deletion can be retried"}
    assert workspace_path.exists()
    assert marker_path.read_text(encoding="utf-8") == "private"
    assert auth_client.get(f"/api/workspaces/{workspace_id}/sessions").status_code == 200


def test_delete_repo_removes_tenant_checkout_and_runtime(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Tenant repo deletion should destroy runtimes and all Yinshi-owned paths."""
    from yinshi.auth import get_session_identity
    from yinshi.config import get_settings
    from yinshi.db import get_control_db
    from yinshi.main import app
    from yinshi.services.accounts import make_tenant
    from yinshi.services.sidecar_runtime import _workspace_home_source

    get_settings().container_enabled = True
    stack = create_full_stack(auth_client, git_repo, name="repo-path-delete")
    repo_id = stack["repo"]["id"]
    workspace_id = stack["workspace"]["id"]
    repo_root = Path(stack["repo"]["root_path"])
    assert repo_root.exists()

    session_token = auth_client.cookies.get("yinshi_session")
    assert session_token is not None
    identity = get_session_identity(session_token)
    assert identity is not None
    with get_control_db() as db:
        user = db.execute("SELECT email FROM users WHERE id = ?", (identity[0],)).fetchone()
    assert user is not None
    tenant = make_tenant(identity[0], user["email"])
    home_path = Path(_workspace_home_source(tenant, workspace_id))
    (home_path / "private.txt").write_text("private", encoding="utf-8")

    from yinshi.services import repository_lifecycle

    moved_sources: list[Path] = []
    original_rename = repository_lifecycle.os.rename

    def record_move(source: str | Path, target: str | Path) -> None:
        moved_sources.append(Path(source))
        original_rename(source, target)

    container_manager = AsyncMock()
    app.state.container_manager = container_manager
    with patch(
        "yinshi.services.repository_lifecycle.os.rename",
        side_effect=record_move,
    ):
        response = auth_client.delete(f"/api/repos/{repo_id}")

    assert response.status_code == 204
    assert repo_root in moved_sources
    assert not home_path.exists()
    assert not repo_root.exists()
    container_manager.destroy_container.assert_awaited_once_with(
        tenant.user_id,
        runtime_id=workspace_id,
    )


def test_delete_repo_preflights_all_runtimes_before_workspace_cleanup(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """One busy runtime should preserve every workspace and managed path."""
    from yinshi.auth import get_session_identity
    from yinshi.db import get_control_db
    from yinshi.main import app
    from yinshi.services.accounts import make_tenant
    from yinshi.services.sidecar_runtime import _workspace_home_source

    stack = create_full_stack(auth_client, git_repo, name="busy-repo-delete")
    repo_id = stack["repo"]["id"]
    workspaces = [stack["workspace"]]
    for _ in range(2):
        workspace = auth_client.post(f"/api/repos/{repo_id}/workspaces", json={}).json()
        session_response = auth_client.post(
            f"/api/workspaces/{workspace['id']}/sessions",
            json={},
        )
        assert session_response.status_code == 201
        workspaces.append(workspace)

    session_token = auth_client.cookies.get("yinshi_session")
    assert session_token is not None
    identity = get_session_identity(session_token)
    assert identity is not None
    with get_control_db() as db:
        user = db.execute("SELECT email FROM users WHERE id = ?", (identity[0],)).fetchone()
    assert user is not None
    tenant = make_tenant(identity[0], user["email"])
    workspace_ids = [str(workspace["id"]) for workspace in workspaces]
    workspace_paths = [Path(str(workspace["path"])) for workspace in workspaces]
    home_paths = [
        Path(_workspace_home_source(tenant, workspace_id)) for workspace_id in workspace_ids
    ]
    for index, home_path in enumerate(home_paths):
        (home_path / "private.txt").write_text(str(index), encoding="utf-8")
    repo_root = Path(stack["repo"]["root_path"])
    busy_workspace_id = workspace_ids[1]

    async def destroy_container(_user_id: str, *, runtime_id: str) -> bool:
        return runtime_id != busy_workspace_id

    container_manager = AsyncMock()
    container_manager.destroy_container.side_effect = destroy_container
    coordinator = AsyncMock()
    coordinator.request_cancel.return_value = True
    app.state.container_manager = container_manager

    with patch("yinshi.api.repos.get_run_coordinator", return_value=coordinator):
        response = auth_client.delete(f"/api/repos/{repo_id}")

    assert response.status_code == 409
    assert response.json() == {"detail": "Workspace is still stopping; deletion can be retried"}
    assert container_manager.destroy_container.await_args_list == [
        call(tenant.user_id, runtime_id=workspace_id) for workspace_id in workspace_ids
    ]
    assert repo_root.exists()
    assert all(path.exists() for path in workspace_paths)
    assert all((path / "private.txt").exists() for path in home_paths)
    assert auth_client.get(f"/api/repos/{repo_id}").status_code == 200
    for workspace_id in workspace_ids:
        assert auth_client.get(f"/api/workspaces/{workspace_id}/sessions").status_code == 200


def test_delete_repo_without_container_runtime(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Tenant repo deletion should work when local execution disables containers."""
    from yinshi.config import get_settings
    from yinshi.main import app

    get_settings().container_enabled = False
    stack = create_full_stack(auth_client, git_repo, name="local-repo-delete")
    app.state.container_manager = None

    response = auth_client.delete(f"/api/repos/{stack['repo']['id']}")

    assert response.status_code == 204


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


def test_delete_registered_repo_prunes_worktrees_and_workspace_branches(
    client: TestClient,
    git_repo: str,
) -> None:
    """Repository deletion should remove linked metadata and generated branches."""
    repo = client.post(
        "/api/repos",
        json={"name": "registered-repo", "local_path": git_repo},
    ).json()
    workspaces = [
        client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json() for _ in range(3)
    ]
    worktree_entries = {
        f"worktree {Path(str(workspace['path'])).resolve()}" for workspace in workspaces
    }
    before_delete = subprocess.run(
        ["/usr/bin/git", "worktree", "list", "--porcelain"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert all(entry in before_delete for entry in worktree_entries)
    for workspace in workspaces:
        branch = str(workspace["branch"])
        branch_check = subprocess.run(
            ["/usr/bin/git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=git_repo,
            check=False,
        )
        assert branch_check.returncode == 0

    response = client.delete(f"/api/repos/{repo['id']}")

    assert response.status_code == 204
    after_delete = subprocess.run(
        ["/usr/bin/git", "worktree", "list", "--porcelain"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert all(entry not in after_delete for entry in worktree_entries)
    for workspace in workspaces:
        branch = str(workspace["branch"])
        branch_check = subprocess.run(
            ["/usr/bin/git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=git_repo,
            check=False,
        )
        assert branch_check.returncode == 1


def test_delete_repo_succeeds_when_git_cleanup_fails_after_commit(
    client: TestClient,
    git_repo: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A committed deletion stays successful while later cleanup continues."""
    from yinshi.db import get_db

    stack = create_full_stack(client, git_repo, name="post-commit-cleanup")
    repo_id = str(stack["repo"]["id"])
    workspace_id = str(stack["workspace"]["id"])
    session_id = str(stack["session"]["id"])
    workspace_path = Path(str(stack["workspace"]["path"]))
    repository_path = Path(git_repo)
    git_metadata_path = repository_path / ".git"
    unavailable_git_metadata_path = repository_path / ".git-unavailable"
    git_metadata_path.rename(unavailable_git_metadata_path)
    private_values = (
        repo_id,
        workspace_id,
        session_id,
        str(repository_path),
        str(workspace_path),
        str(stack["workspace"]["branch"]),
    )
    caplog.clear()
    caplog.set_level(logging.ERROR, logger="yinshi.api.repos")

    response = client.delete(f"/api/repos/{repo_id}")

    assert response.status_code == 204
    with get_db() as db:
        assert db.execute("SELECT id FROM repos WHERE id = ?", (repo_id,)).fetchone() is None
        assert (
            db.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone() is None
        )
        assert db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone() is None
    assert not workspace_path.exists()
    assert not (repository_path / ".yinshi-delete-quarantine").exists()
    cleanup_records = [
        record
        for record in caplog.records
        if record.name == "yinshi.api.repos" and record.levelno == logging.ERROR
    ]
    assert [record.getMessage() for record in cleanup_records] == ["Repository Git cleanup failed"]
    for record in cleanup_records:
        rendered_record = f"{record.getMessage()} {record.args!r}"
        assert all(private_value not in rendered_record for private_value in private_values)


def test_delete_repo_uses_repository_lifecycle_lock(
    client: TestClient,
    git_repo: str,
    tmp_path: Path,
) -> None:
    """Repository deletion should hold the keyed lifecycle lock."""
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    repo = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    ).json()
    entered_repo_ids: list[str] = []
    entered_lock_roots: list[Path] = []

    @asynccontextmanager
    async def record_lock(repo_id: str, lock_root: Path) -> AsyncIterator[None]:
        entered_repo_ids.append(repo_id)
        entered_lock_roots.append(lock_root)
        yield

    with (
        patch(
            "yinshi.api.repos.repository_lifecycle_root",
            return_value=tmp_path,
            create=True,
        ) as root_resolver,
        patch("yinshi.api.repos.repository_lifecycle", side_effect=record_lock, create=True),
    ):
        response = client.delete(f"/api/repos/{repo['id']}")

    assert response.status_code == 204
    assert entered_repo_ids == [repo["id"]]
    assert entered_lock_roots == [tmp_path]
    root_resolver.assert_called_once()


@pytest.mark.asyncio
async def test_repository_delete_lock_blocks_workspace_creation(
    db: sqlite3.Connection,
    git_repo: str,
) -> None:
    """Workspace creation should wait while repository deletion owns lifecycle."""
    from yinshi.services.repository_lifecycle import repository_lifecycle
    from yinshi.services.workspace import create_workspace_for_repo

    lock_root = Path(get_settings().db_path).expanduser().absolute().parent
    repo_id = "repository-delete-lock"
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES (?, ?, ?)",
        (repo_id, "locked-repo", git_repo),
    )
    db.commit()

    with (
        patch("yinshi.services.workspace.generate_branch_name", return_value="locked-branch"),
        patch("yinshi.services.workspace.create_worktree", new_callable=AsyncMock),
        patch("yinshi.services.workspace.ensure_secret_guardrails"),
    ):
        async with repository_lifecycle(repo_id, lock_root):
            creation = asyncio.create_task(create_workspace_for_repo(db, repo_id))
            await asyncio.sleep(0)
            assert not creation.done()

        workspace = await asyncio.wait_for(creation, timeout=1)

    assert workspace["repo_id"] == repo_id


@pytest.mark.asyncio
async def test_repository_delete_lock_blocks_workspace_deletion(
    db: sqlite3.Connection,
    git_repo: str,
) -> None:
    """Workspace deletion should wait while repository deletion owns lifecycle."""
    from yinshi.services.repository_lifecycle import repository_lifecycle
    from yinshi.services.workspace import delete_workspace

    lock_root = Path(get_settings().db_path).expanduser().absolute().parent
    repo_id = "repository-workspace-delete-lock"
    workspace_id = "workspace-delete-lock"
    workspace_path = str(Path(git_repo) / ".worktrees" / "locked-branch")
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES (?, ?, ?)",
        (repo_id, "locked-repo", git_repo),
    )
    db.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path)
           VALUES (?, ?, ?, ?, ?)""",
        (workspace_id, repo_id, "locked", "locked-branch", workspace_path),
    )
    db.commit()

    with patch("yinshi.services.workspace.delete_worktree", new_callable=AsyncMock):
        async with repository_lifecycle(repo_id, lock_root):
            deletion = asyncio.create_task(delete_workspace(db, workspace_id))
            await asyncio.sleep(0)
            assert not deletion.done()

        await asyncio.wait_for(deletion, timeout=1)

    assert db.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone() is None


def test_delete_repo_restores_all_workspaces_when_later_quarantine_move_fails(
    client: TestClient,
    git_repo: str,
) -> None:
    """A later move failure should preserve every workspace path and row."""
    repo = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    ).json()
    workspaces = [
        client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json() for _ in range(3)
    ]
    workspace_paths = [Path(str(workspace["path"])) for workspace in workspaces]
    for index, workspace_path in enumerate(workspace_paths):
        (workspace_path / "private.txt").write_text(str(index), encoding="utf-8")
        session_response = client.post(
            f"/api/workspaces/{workspaces[index]['id']}/sessions",
            json={},
        )
        assert session_response.status_code == 201

    attempted_sources: list[Path] = []
    failure_source = workspace_paths[1]

    from yinshi.services import repository_lifecycle

    original_rename = repository_lifecycle.os.rename

    def fail_later_move(source: str | Path, target: str | Path) -> None:
        source_path = Path(source)
        attempted_sources.append(source_path)
        if source_path == failure_source:
            raise OSError("later move failed")
        original_rename(source, target)

    with patch(
        "yinshi.services.repository_lifecycle.os.rename",
        side_effect=fail_later_move,
    ):
        response = client.delete(f"/api/repos/{repo['id']}")

    assert response.status_code == 500
    assert response.json() == {"detail": "Repository cleanup failed; deletion can be retried"}
    assert attempted_sources[:2] == workspace_paths[:2]
    assert client.get(f"/api/repos/{repo['id']}").status_code == 200
    listed_workspace_ids = {
        workspace["id"] for workspace in client.get(f"/api/repos/{repo['id']}/workspaces").json()
    }
    assert listed_workspace_ids == {workspace["id"] for workspace in workspaces}
    for index, workspace in enumerate(workspaces):
        workspace_path = workspace_paths[index]
        assert workspace_path.exists()
        assert (workspace_path / "private.txt").read_text(encoding="utf-8") == str(index)
        assert client.get(f"/api/workspaces/{workspace['id']}/sessions").status_code == 200


def test_delete_repo_removes_local_pi_session_files(
    client: TestClient,
    git_repo: str,
) -> None:
    """Repository deletion should remove durable local Pi session files."""
    from yinshi.services.sidecar_runtime import local_pi_session_file

    repo = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    ).json()
    session_paths: list[Path] = []
    for _ in range(2):
        workspace = client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json()
        session = client.post(
            f"/api/workspaces/{workspace['id']}/sessions",
            json={},
        ).json()
        session_path = Path(local_pi_session_file(str(session["id"])))
        session_path.write_text("private", encoding="utf-8")
        session_paths.append(session_path)

    response = client.delete(f"/api/repos/{repo['id']}")

    assert response.status_code == 204
    assert all(not session_path.exists() for session_path in session_paths)


def test_delete_repo_restores_all_paths_when_database_delete_fails(
    client: TestClient,
    git_repo: str,
) -> None:
    """A database failure after all moves should restore every managed path."""
    from yinshi.db import get_db

    repo = client.post(
        "/api/repos",
        json={"name": "test-repo", "local_path": git_repo},
    ).json()
    workspaces = [
        client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json() for _ in range(3)
    ]
    workspace_paths = [Path(str(workspace["path"])) for workspace in workspaces]
    for index, workspace_path in enumerate(workspace_paths):
        (workspace_path / "private.txt").write_text(str(index), encoding="utf-8")

    with get_db() as db:
        db.execute("""CREATE TRIGGER fail_repo_delete
               BEFORE DELETE ON repos
               BEGIN
                   SELECT RAISE(ABORT, 'forced repository delete failure');
               END""")
        db.commit()

    response = client.delete(f"/api/repos/{repo['id']}")

    assert response.status_code == 500
    assert response.json() == {"detail": "Repository cleanup failed; deletion can be retried"}
    assert client.get(f"/api/repos/{repo['id']}").status_code == 200
    listed_workspace_ids = {
        workspace["id"] for workspace in client.get(f"/api/repos/{repo['id']}/workspaces").json()
    }
    assert listed_workspace_ids == {workspace["id"] for workspace in workspaces}
    for index, workspace_path in enumerate(workspace_paths):
        assert workspace_path.exists()
        assert (workspace_path / "private.txt").read_text(encoding="utf-8") == str(index)


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


def test_create_workspace_fetches_remote_base_for_remote_repo(
    auth_client: TestClient,
) -> None:
    """Remote repos should create worktrees from the fetched remote branch tip."""
    from yinshi.tenant import get_user_db

    tenant = getattr(auth_client, "yinshi_tenant")
    with get_user_db(tenant) as db:
        cursor = db.execute(
            """INSERT INTO repos (name, remote_url, root_path, installation_id)
               VALUES (?, ?, ?, ?)""",
            (
                "remote-repo",
                "https://github.com/acme/private-repo.git",
                str(Path(tenant.data_dir) / "repos" / "remote-repo"),
                12,
            ),
        )
        repo_row = db.execute(
            "SELECT id FROM repos WHERE rowid = ?",
            (cursor.lastrowid,),
        ).fetchone()
        assert repo_row is not None
        repo_id = repo_row["id"]
        db.commit()

    with (
        patch(
            "yinshi.services.workspace.resolve_remote_base_ref",
            new=AsyncMock(return_value="origin/main"),
        ) as mock_resolve_remote_base_ref,
        patch(
            "yinshi.services.workspace.create_worktree",
            new=AsyncMock(
                return_value=str(
                    Path(tenant.data_dir) / "repos" / "remote-repo" / ".worktrees" / "branch"
                )
            ),
        ) as mock_create_worktree,
        patch(
            "yinshi.services.workspace._resolve_remote_checkout",
            new=AsyncMock(
                return_value=(
                    "https://github.com/acme/private-repo.git",
                    "token-123",
                    12,
                )
            ),
        ),
    ):
        response = auth_client.post(f"/api/repos/{repo_id}/workspaces", json={})

    assert response.status_code == 201
    mock_resolve_remote_base_ref.assert_awaited_once_with(
        str(Path(tenant.data_dir) / "repos" / "remote-repo"),
        access_token="token-123",
    )
    assert mock_create_worktree.await_args.kwargs["base_ref"] == "origin/main"


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


def test_create_workspace_repairs_trusted_repo_remote_metadata(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Trusted tenant repos should still reconcile stale origin URLs and install ids.

    This regression covers repos imported before GitHub was connected. Those
    repos already live inside tenant storage, so the old repair path returned
    early and never corrected a placeholder origin or refreshed the stored
    installation id.
    """
    from yinshi.tenant import get_user_db

    tenant = getattr(auth_client, "yinshi_tenant")
    trusted_repo_path = Path(tenant.data_dir) / "repos" / "trusted-repo"
    trusted_repo_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", git_repo, str(trusted_repo_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "remote",
            "set-url",
            "origin",
            "https://github.com/your-username/devtoolscrape.git",
        ],
        cwd=trusted_repo_path,
        check=True,
        capture_output=True,
    )

    with get_user_db(tenant) as db:
        cursor = db.execute(
            """INSERT INTO repos (name, remote_url, root_path, installation_id)
               VALUES (?, ?, ?, ?)""",
            (
                "devtoolscrape",
                "https://github.com/codyborders/devtoolscrape",
                str(trusted_repo_path),
                None,
            ),
        )
        repo_row = db.execute(
            "SELECT id FROM repos WHERE rowid = ?",
            (cursor.lastrowid,),
        ).fetchone()
        assert repo_row is not None
        repo_id = repo_row["id"]
        db.commit()

    with (
        patch(
            "yinshi.services.workspace._resolve_remote_checkout",
            new=AsyncMock(
                return_value=(
                    "https://github.com/codyborders/devtoolscrape.git",
                    None,
                    117632573,
                )
            ),
        ),
        patch(
            "yinshi.services.workspace.resolve_remote_base_ref",
            new=AsyncMock(return_value="origin/main"),
        ),
        patch(
            "yinshi.services.workspace.create_worktree",
            new=AsyncMock(return_value=str(trusted_repo_path / ".worktrees" / "branch")),
        ),
    ):
        response = auth_client.post(f"/api/repos/{repo_id}/workspaces", json={})

    assert response.status_code == 201
    repaired_remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=trusted_repo_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert repaired_remote == "https://github.com/codyborders/devtoolscrape.git"

    with get_user_db(tenant) as db:
        refreshed_repo = db.execute(
            "SELECT remote_url, installation_id FROM repos WHERE id = ?",
            (repo_id,),
        ).fetchone()
    assert refreshed_repo is not None
    assert refreshed_repo["remote_url"] == "https://github.com/codyborders/devtoolscrape.git"
    assert refreshed_repo["installation_id"] == 117632573


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


def test_prompt_repairs_trusted_workspace_remote_metadata(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Prompting should reconcile a trusted repo remote before sidecar execution.

    An earlier agent turn can rewrite ``origin`` to an SSH URL inside a valid
    tenant checkout. The next prompt must repair that remote back to the
    canonical HTTPS GitHub URL and refresh the stored installation id before
    resolving runtime git auth.
    """
    from yinshi.api.stream import ExecutionContext
    from yinshi.tenant import get_user_db

    tenant = getattr(auth_client, "yinshi_tenant")
    repaired_repo_path = Path(tenant.data_dir) / "repos" / "repo-prompt-repair"
    repaired_repo_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", git_repo, str(repaired_repo_path)],
        check=True,
        capture_output=True,
    )
    branch = "feature-trusted-repair"
    repaired_worktree_path = repaired_repo_path / ".worktrees" / branch
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(repaired_worktree_path)],
        cwd=repaired_repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:codyborders/devtoolscrape.git"],
        cwd=repaired_repo_path,
        check=True,
        capture_output=True,
    )

    with get_user_db(tenant) as db:
        repo_cursor = db.execute(
            """
            INSERT INTO repos (name, remote_url, root_path, installation_id)
            VALUES (?, ?, ?, ?)
            """,
            (
                "devtoolscrape",
                "https://github.com/codyborders/devtoolscrape",
                str(repaired_repo_path),
                None,
            ),
        )
        repo_row = db.execute(
            "SELECT id FROM repos WHERE rowid = ?",
            (repo_cursor.lastrowid,),
        ).fetchone()
        assert repo_row is not None
        workspace_cursor = db.execute(
            """
            INSERT INTO workspaces (repo_id, name, branch, path, state)
            VALUES (?, ?, ?, ?, 'ready')
            """,
            (
                repo_row["id"],
                branch,
                branch,
                str(repaired_worktree_path),
            ),
        )
        workspace_row = db.execute(
            "SELECT id FROM workspaces WHERE rowid = ?",
            (workspace_cursor.lastrowid,),
        ).fetchone()
        assert workspace_row is not None
        session_cursor = db.execute(
            """
            INSERT INTO sessions (workspace_id, status, model)
            VALUES (?, 'idle', 'minimax')
            """,
            (workspace_row["id"],),
        )
        session_row = db.execute(
            "SELECT id FROM sessions WHERE rowid = ?",
            (session_cursor.lastrowid,),
        ).fetchone()
        assert session_row is not None
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
        del sid, prompt, model, cwd, provider_auth, provider_config, agent_dir, settings_payload
        yield {
            "type": "message",
            "data": {"type": "result", "usage": {}},
        }

    mock_sidecar = make_mock_sidecar(fake_query)
    with (
        patch(
            "yinshi.services.workspace._resolve_remote_checkout",
            new=AsyncMock(
                return_value=(
                    "https://github.com/codyborders/devtoolscrape.git",
                    None,
                    117632573,
                )
            ),
        ),
        patch(
            "yinshi.api.stream._resolve_execution_context",
            new=AsyncMock(
                return_value=ExecutionContext(
                    sidecar_socket=None,
                    effective_cwd=str(repaired_worktree_path),
                    key_source="platform",
                    provider="test-provider",
                    provider_auth=None,
                    provider_config=None,
                    model_ref="minimax/MiniMax-M2.7",
                )
            ),
        ),
        patch(
            "yinshi.api.stream.create_sidecar_connection",
            return_value=mock_sidecar,
        ),
    ):
        response = auth_client.post(
            f"/api/sessions/{session_row['id']}/prompt",
            json={"prompt": "repair trusted origin before push"},
        )

    assert response.status_code == 200
    repaired_remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repaired_repo_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert repaired_remote == "https://github.com/codyborders/devtoolscrape.git"

    with get_user_db(tenant) as db:
        repo_state = db.execute(
            "SELECT remote_url, installation_id FROM repos WHERE id = ?",
            (repo_row["id"],),
        ).fetchone()
    assert repo_state is not None
    assert repo_state["remote_url"] == "https://github.com/codyborders/devtoolscrape.git"
    assert repo_state["installation_id"] == 117632573


def test_prompt_forwards_github_runtime_git_auth(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Prompt execution should forward ephemeral GitHub git auth for app-backed repos."""
    from yinshi.db import get_control_db
    from yinshi.services.crypto import encrypt_api_key
    from yinshi.services.git_runtime import GitRuntimeAuth
    from yinshi.services.keys import get_user_dek

    tenant = getattr(auth_client, "yinshi_tenant")
    stack = create_full_stack(auth_client, git_repo, name="github-runtime-auth")
    session_id = stack["session"]["id"]
    repo_id = stack["repo"]["id"]

    dek = get_user_dek(tenant.user_id)
    encrypted_key = encrypt_api_key("sk-user-minimax-key", dek)
    with get_control_db() as db:
        db.execute(
            "INSERT INTO api_keys (user_id, provider, encrypted_key) VALUES (?, ?, ?)",
            (tenant.user_id, "minimax", encrypted_key),
        )
        db.commit()

    with sqlite3.connect(tenant.db_path) as user_db:
        user_db.execute(
            "UPDATE repos SET remote_url = ?, installation_id = ? WHERE id = ?",
            ("https://github.com/acme/private-repo.git", 321, repo_id),
        )
        user_db.commit()

    async def fake_query(
        sid,
        prompt,
        model=None,
        cwd=None,
        provider_auth=None,
        provider_config=None,
        git_auth=None,
        agent_dir=None,
        settings_payload=None,
    ):
        yield {
            "type": "message",
            "data": {"type": "result", "usage": {}},
        }

    mock_sidecar = make_mock_sidecar(fake_query)
    with (
        patch(
            "yinshi.api.stream.create_sidecar_connection",
            return_value=mock_sidecar,
        ),
        patch(
            "yinshi.api.stream.resolve_git_runtime_auth",
            new=AsyncMock(
                return_value=GitRuntimeAuth(
                    strategy="github_app_https",
                    host="github.com",
                    access_token="runtime-installation-token",
                )
            ),
        ),
    ):
        resp = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "push the branch to github"},
        )

    assert resp.status_code == 200
    assert mock_sidecar.warmup.call_args.kwargs["git_auth"] == {
        "strategy": "github_app_https",
        "host": "github.com",
        "accessToken": "runtime-installation-token",
    }


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
                "message": {"content": [{"type": "thinking", "thinking": "Need inspect."}]},
            },
        }
        yield {
            "type": "message",
            "data": {
                "type": "tool_use",
                "id": "tool-1",
                "name": "read",
                "input": {"path": "README.md"},
            },
        }
        yield {
            "type": "tool_result",
            "tool_use_id": "tool-1",
            "content": "# Test",
        }
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

    mock_sidecar = make_mock_sidecar(fake_query)
    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=mock_sidecar,
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
    assert "tool_use" in types
    assert "tool_result" in types
    assert "result" in types

    # Verify user + assistant messages persisted
    msgs = client.get(f"/api/sessions/{session_id}/messages").json()
    roles = [m["role"] for m in msgs]
    assert "user" in roles
    assert "assistant" in roles
    assistant_message = next(message for message in msgs if message["role"] == "assistant")
    stored_turn = json.loads(assistant_message["full_message"])
    assert stored_turn["schema"] == "yinshi.assistant_turn.v1"
    assert [event["type"] for event in stored_turn["events"]] == [
        "assistant",
        "tool_use",
        "tool_result",
        "assistant",
        "result",
    ]
    assert assistant_message["content"] == "Hello world"
    assert mock_sidecar.warmup.call_args.kwargs["pi_session_file"].endswith(f"{session_id}.jsonl")


def test_desktop_bearer_prompt_streams_without_browser_cookie(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Desktop bearer authority should not require a browser session cookie."""
    from yinshi.api.stream import ExecutionContext
    from yinshi.main import app
    from yinshi.services.desktop_tokens import VerifiedDesktopAccess

    tenant = getattr(auth_client, "yinshi_tenant")
    stack = create_full_stack(auth_client, git_repo, name="desktop-bearer-prompt")
    session_id = stack["session"]["id"]

    async def fake_query(*_args, **_kwargs):
        yield {
            "type": "message",
            "data": {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "desktop reply"}]},
            },
        }
        yield {"type": "message", "data": {"type": "result", "usage": {}}}

    mock_sidecar = make_mock_sidecar(fake_query)
    with (
        TestClient(app) as desktop_client,
        patch(
            "yinshi.auth.resolve_desktop_principal",
            return_value=(
                tenant,
                VerifiedDesktopAccess(
                    user_id=tenant.user_id,
                    device_id="desktop-device",
                ),
            ),
        ),
        patch(
            "yinshi.api.stream._resolve_execution_context",
            new=AsyncMock(
                return_value=ExecutionContext(
                    sidecar_socket=None,
                    effective_cwd=stack["workspace"]["path"],
                    key_source="api_key",
                    provider="test-provider",
                    provider_auth=None,
                    provider_config=None,
                    model_ref="test/model",
                )
            ),
        ),
        patch(
            "yinshi.api.stream.desktop_device_is_active",
            return_value=True,
            create=True,
        ),
        patch(
            "yinshi.api.stream.create_sidecar_connection",
            return_value=mock_sidecar,
        ),
    ):
        desktop_client.headers.update(
            {
                "Authorization": "Bearer desktop-access-token",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        response = desktop_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "reply from desktop"},
        )

    assert response.status_code == 200
    assert "desktop reply" in response.text


def test_prompt_stream_stops_after_auth_session_revocation(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Revoking a session should cancel its run before later output is exposed."""
    from yinshi.api.stream import ExecutionContext
    from yinshi.auth import get_session_identity, revoke_auth_session

    stack = create_full_stack(auth_client, git_repo, name="revoked-prompt-stream")
    session_id = stack["session"]["id"]
    session_token = auth_client.cookies.get("yinshi_session")
    assert session_token is not None
    identity = get_session_identity(session_token)
    assert identity is not None

    async def fake_query(*_args, **_kwargs):
        yield {
            "type": "message",
            "data": {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "visible-before-revoke"}]},
            },
        }
        revoke_auth_session(*identity)
        yield {
            "type": "message",
            "data": {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "private-after-revoke"}]},
            },
        }

    mock_sidecar = make_mock_sidecar(fake_query)
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
                    key_source="api_key",
                    provider="test-provider",
                    provider_auth=None,
                    provider_config=None,
                    model_ref="test/model",
                )
            ),
        ),
    ):
        response = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "start revocable run"},
        )

    assert response.status_code == 200
    assert "visible-before-revoke" in response.text
    assert "private-after-revoke" not in response.text
    mock_sidecar.cancel.assert_awaited_once_with(session_id)


@pytest.mark.asyncio
async def test_session_bound_events_enforces_connection_lifetime(monkeypatch) -> None:
    """An idle sidecar stream must end at the independent connection deadline."""
    from yinshi.api import stream

    class LifetimeSidecar:
        def __init__(self) -> None:
            self.cancelled_session_ids: list[str] = []

        async def cancel(self, session_id: str) -> None:
            self.cancelled_session_ids.append(session_id)

    async def idle_events():
        await asyncio.Event().wait()
        yield {"type": "unreachable"}

    sidecar = LifetimeSidecar()
    monkeypatch.setattr(stream, "_STREAM_LIFETIME_S_MAX", 0.01)
    monkeypatch.setattr(stream, "get_session_identity", lambda _token: ("user", "auth-session"))
    events = stream._session_bound_events(
        events=idle_events(),
        sidecar=sidecar,
        session_token="signed-session",
        session_id="prompt-session",
    )

    with pytest.raises(stream._StreamLifetimeReached):
        await anext(events)

    assert sidecar.cancelled_session_ids == ["prompt-session"]


@pytest.mark.asyncio
async def test_desktop_device_bound_events_stop_on_immediate_revocation(
    monkeypatch,
) -> None:
    """Revoking a desktop device should cancel its active prompt immediately."""
    from yinshi.api import stream
    from yinshi.services.live_auth_sessions import signal_desktop_device_revoked

    class DesktopSidecar:
        def __init__(self) -> None:
            self.cancelled_session_ids: list[str] = []

        async def cancel(self, session_id: str) -> None:
            self.cancelled_session_ids.append(session_id)

    async def idle_events():
        await asyncio.Event().wait()
        yield {"type": "unreachable"}

    sidecar = DesktopSidecar()
    monkeypatch.setattr(stream, "desktop_device_is_active", lambda **_kwargs: True)
    events = stream._desktop_device_bound_events(
        idle_events(),
        user_id="user-id",
        device_id="device-id",
        sidecar=sidecar,
        session_id="prompt-session",
    )
    next_event = asyncio.create_task(anext(events))
    await asyncio.sleep(0)
    signal_desktop_device_revoked("device-id")

    with pytest.raises(stream._AuthSessionRevoked):
        await asyncio.wait_for(next_event, timeout=0.2)

    assert sidecar.cancelled_session_ids == ["prompt-session"]


def test_prompt_rejects_legacy_transcript_without_durable_pi_context(
    client: TestClient,
    session_id: str,
) -> None:
    """Legacy transcript-only sessions should not pretend to resume exact Pi context."""
    from yinshi.db import get_db

    with get_db() as db:
        db.execute(
            "UPDATE sessions SET pi_context_version = 0 WHERE id = ?",
            (session_id,),
        )
        db.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, 'user', 'old prompt')",
            (session_id,),
        )
        db.commit()

    resp = client.post(
        f"/api/sessions/{session_id}/prompt",
        json={"prompt": "continue"},
    )

    assert resp.status_code == 409
    payload = resp.json()
    assert payload["detail"]["code"] == "legacy_pi_context"


def test_prompt_upgrades_empty_legacy_session_to_durable_pi_context(
    client: TestClient,
    session_id: str,
) -> None:
    """Empty legacy sessions should be upgraded before first durable Pi prompt."""
    from yinshi.db import get_db

    with get_db() as db:
        db.execute(
            "UPDATE sessions SET pi_context_version = 0 WHERE id = ?",
            (session_id,),
        )
        db.commit()

    async def fake_query(*args, **kwargs):
        del args, kwargs
        yield {"type": "message", "data": {"type": "result", "usage": {}}}

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=make_mock_sidecar(fake_query),
    ):
        resp = client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "start"},
        )

    assert resp.status_code == 200
    with get_db() as db:
        row = db.execute(
            "SELECT pi_context_version FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    assert row is not None
    assert row["pi_context_version"] == 1


def test_prompt_forwards_explicit_thinking_override_for_reasoning_model(
    client: TestClient,
    session_id: str,
) -> None:
    """Explicit thinking overrides should reach the sidecar for reasoning models."""
    from yinshi.api.stream import ExecutionContext

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
        del sid, prompt, model, cwd, provider_auth, provider_config, agent_dir
        assert settings_payload == {"mode": "quiet", "defaultThinkingLevel": "off"}
        yield {
            "type": "message",
            "data": {"type": "result", "usage": {}},
        }

    mock_sidecar = make_mock_sidecar(fake_query)
    mock_sidecar.get_catalog = AsyncMock(
        return_value={
            "models": [
                {
                    "ref": "minimax/MiniMax-M2.7",
                    "reasoning": True,
                }
            ]
        }
    )

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
                    settings_payload={"mode": "quiet"},
                    model_ref="minimax/MiniMax-M2.7",
                )
            ),
        ),
    ):
        response = client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "say hello", "thinking": False},
        )

    assert response.status_code == 200
    assert mock_sidecar.warmup.call_args.kwargs["settings_payload"] == {
        "mode": "quiet",
        "defaultThinkingLevel": "off",
    }
    mock_sidecar.get_catalog.assert_awaited_once()


def test_prompt_enables_reasoning_with_pi_thinking_level(
    client: TestClient,
    session_id: str,
) -> None:
    """A positive thinking override should set a Pi thinking level."""
    from yinshi.api.stream import ExecutionContext

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
        del sid, prompt, model, cwd, provider_auth, provider_config, agent_dir
        assert settings_payload == {"defaultThinkingLevel": "medium"}
        yield {
            "type": "message",
            "data": {"type": "result", "usage": {}},
        }

    mock_sidecar = make_mock_sidecar(fake_query)
    mock_sidecar.get_catalog = AsyncMock(
        return_value={
            "models": [
                {
                    "ref": "minimax/MiniMax-M2.7",
                    "reasoning": True,
                }
            ]
        }
    )

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
                    settings_payload={"defaultThinkingLevel": "off"},
                    model_ref="minimax/MiniMax-M2.7",
                )
            ),
        ),
    ):
        response = client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "say hello", "thinking": True},
        )

    assert response.status_code == 200
    assert mock_sidecar.warmup.call_args.kwargs["settings_payload"] == {
        "defaultThinkingLevel": "medium",
    }
    mock_sidecar.get_catalog.assert_awaited_once()


def test_prompt_forwards_explicit_thinking_level(
    client: TestClient,
    session_id: str,
) -> None:
    """A named thinking level should reach Pi settings unchanged when supported."""
    from yinshi.api.stream import ExecutionContext

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
        del sid, prompt, model, cwd, provider_auth, provider_config, agent_dir
        assert settings_payload == {"defaultThinkingLevel": "xhigh"}
        yield {
            "type": "message",
            "data": {"type": "result", "usage": {}},
        }

    mock_sidecar = make_mock_sidecar(fake_query)
    mock_sidecar.get_catalog = AsyncMock(
        return_value={
            "models": [
                {
                    "ref": "minimax/MiniMax-M2.7",
                    "reasoning": True,
                    "thinking_levels": [
                        "off",
                        "minimal",
                        "low",
                        "medium",
                        "high",
                        "xhigh",
                    ],
                }
            ]
        }
    )

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
        response = client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "say hello", "thinking": "xhigh"},
        )

    assert response.status_code == 200
    assert mock_sidecar.warmup.call_args.kwargs["settings_payload"] == {
        "defaultThinkingLevel": "xhigh",
    }
    mock_sidecar.get_catalog.assert_awaited_once()


def test_prompt_ignores_thinking_override_for_non_reasoning_model(
    client: TestClient,
    session_id: str,
) -> None:
    """Non-reasoning models should keep their existing settings payload."""
    from yinshi.api.stream import ExecutionContext

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
        del sid, prompt, model, cwd, provider_auth, provider_config, agent_dir
        assert settings_payload == {"mode": "quiet", "thinking": False}
        yield {
            "type": "message",
            "data": {"type": "result", "usage": {}},
        }

    mock_sidecar = make_mock_sidecar(fake_query)
    mock_sidecar.get_catalog = AsyncMock(
        return_value={
            "models": [
                {
                    "ref": "minimax/MiniMax-M2.7",
                    "reasoning": False,
                }
            ]
        }
    )

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
                    settings_payload={"mode": "quiet", "thinking": False},
                    model_ref="minimax/MiniMax-M2.7",
                )
            ),
        ),
    ):
        response = client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "say hello", "thinking": True},
        )

    assert response.status_code == 200
    assert mock_sidecar.warmup.call_args.kwargs["settings_payload"] == {
        "mode": "quiet",
        "thinking": False,
    }
    mock_sidecar.get_catalog.assert_awaited_once()


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


def test_prompt_marks_explicit_sidecar_error_as_failed(
    client: TestClient,
    session_id: str,
) -> None:
    """Explicit sidecar error events should mark the assistant turn as failed."""

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
                "message": {"content": [{"type": "text", "text": "partial reply"}]},
            },
        }
        yield {
            "type": "error",
            "error": "model backend failed",
        }

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=make_mock_sidecar(failing_query),
    ):
        response = client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "trigger explicit sidecar error"},
        )

    assert response.status_code == 200
    events = parse_sse_events(response.text)
    assert [event["type"] for event in events] == ["assistant", "error"]

    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assistant_messages = [message for message in messages if message["role"] == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["turn_status"] == "failed"


def test_tenant_prompt_persists_turn_status(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Tenant-mode prompts should write turn status into the per-user database."""
    from yinshi.api.stream import ExecutionContext
    from yinshi.tenant import get_user_db

    tenant = getattr(auth_client, "yinshi_tenant")
    stack = create_full_stack(auth_client, git_repo, name="tenant-turn-status")
    session_id = stack["session"]["id"]

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
                "message": {"content": [{"type": "text", "text": "tenant reply"}]},
            },
        }
        yield {
            "type": "message",
            "data": {"type": "result", "usage": {}},
        }

    with (
        patch(
            "yinshi.api.stream._resolve_execution_context",
            new=AsyncMock(
                return_value=ExecutionContext(
                    sidecar_socket=None,
                    effective_cwd=stack["workspace"]["path"],
                    key_source="platform",
                    provider="test-provider",
                    provider_auth=None,
                    provider_config=None,
                    model_ref="minimax/MiniMax-M2.7",
                )
            ),
        ),
        patch(
            "yinshi.api.stream.create_sidecar_connection",
            return_value=make_mock_sidecar(fake_query),
        ),
    ):
        response = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "persist tenant status"},
        )

    assert response.status_code == 200

    messages = auth_client.get(f"/api/sessions/{session_id}/messages").json()
    assistant_messages = [message for message in messages if message["role"] == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["turn_status"] == "completed"

    with get_user_db(tenant) as user_db:
        message_columns = [
            row[1] for row in user_db.execute("PRAGMA table_info(messages)").fetchall()
        ]
    assert "turn_status" in message_columns


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
