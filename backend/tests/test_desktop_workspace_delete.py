"""Desktop workspace deletion should hand pi sessions back to the local sidecar."""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.parametrize(
    "delete_path",
    ["/api/workspaces/workspace-id", "/api/repos/repo-id"],
)
def test_desktop_delete_releases_pi_sessions(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    delete_path: str,
) -> None:
    """Desktop deletion should release sessions from its long-lived sidecar."""
    from fastapi.testclient import TestClient

    from tests.conftest import _configure_test_env
    from yinshi.config import get_settings
    from yinshi.db import get_db, init_db
    from yinshi.desktop_bootstrap import DesktopBootstrapMiddleware

    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    repository_base = tmp_path / "repositories"
    repository_path = repository_base / "repo"
    workspace_path = repository_base / "worktree"
    repository_path.mkdir(parents=True)
    _git("init", "-b", "main", cwd=repository_path)
    _git("config", "user.email", "test@example.com", cwd=repository_path)
    _git("config", "user.name", "Test", cwd=repository_path)
    (repository_path / "README.md").write_text("hello", encoding="utf-8")
    _git("add", "README.md", cwd=repository_path)
    _git("commit", "-m", "init", cwd=repository_path)
    _git(
        "worktree",
        "add",
        "-b",
        "desktop/test",
        str(workspace_path),
        cwd=repository_path,
    )
    socket_path = tmp_path / "run" / "sidecar.sock"
    monkeypatch.setenv("ALLOWED_REPO_BASE", str(repository_base))
    monkeypatch.setenv("SIDECAR_SOCKET_PATH", str(socket_path))
    monkeypatch.setenv("FRONTEND_URL", "http://testserver")
    (tmp_path / "index.html").write_text("<main>Desktop</main>", encoding="utf-8")
    get_settings.cache_clear()

    from yinshi.main import create_app

    release_sessions = AsyncMock()
    monkeypatch.setattr(
        "yinshi.api.workspaces.release_sessions",
        release_sessions,
        raising=False,
    )
    monkeypatch.setattr(
        "yinshi.api.repos.release_sessions",
        release_sessions,
        raising=False,
    )

    try:
        init_db()
        with get_db() as database:
            database.execute(
                "INSERT INTO repos (id, name, root_path) VALUES (?, ?, ?)",
                ("repo-id", "Repo", str(repository_path)),
            )
            database.execute(
                """
                INSERT INTO workspaces (id, repo_id, name, branch, path)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("workspace-id", "repo-id", "Workspace", "desktop/test", str(workspace_path)),
            )
            database.execute(
                "INSERT INTO sessions (id, workspace_id) VALUES (?, ?)",
                ("session-a", "workspace-id"),
            )
            database.execute(
                "INSERT INTO sessions (id, workspace_id) VALUES (?, ?)",
                ("session-b", "workspace-id"),
            )
            database.commit()

        application = DesktopBootstrapMiddleware(
            create_app(mode="desktop", desktop_asset_dir=tmp_path),
            instance_nonce="n" * 43,
        )
        with TestClient(application) as client:
            bootstrap = client.post(
                "/desktop/bootstrap",
                headers={"X-Yinshi-Bootstrap": "n" * 43},
            )
            assert bootstrap.status_code == 204

            response = client.delete(delete_path)

        assert response.status_code == 204
        release_sessions.assert_awaited_once()
        released_socket, released_ids = release_sessions.await_args.args
        assert released_socket == str(socket_path)
        assert sorted(released_ids) == ["session-a", "session-b"]
    finally:
        get_settings.cache_clear()
