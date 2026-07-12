"""Tests desktop terminal path resolution against app-managed workspace boundaries."""

from __future__ import annotations

import pytest


def test_desktop_terminal_resolves_only_managed_workspace_paths(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver should return host paths inside the profile and reject escaped rows."""
    from tests.conftest import _configure_test_env
    from yinshi.config import get_settings
    from yinshi.db import get_db, init_db
    from yinshi.services.desktop_terminal import resolve_desktop_terminal_context

    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    repository_base = tmp_path / "repositories"
    repository_path = repository_base / "repo"
    workspace_path = repository_base / "worktree"
    socket_path = tmp_path / "run" / "sidecar.sock"
    repository_path.mkdir(parents=True)
    workspace_path.mkdir()
    monkeypatch.setenv("ALLOWED_REPO_BASE", str(repository_base))
    monkeypatch.setenv("SIDECAR_SOCKET_PATH", str(socket_path))
    get_settings.cache_clear()

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
            database.commit()

        context = resolve_desktop_terminal_context("workspace-id")
        assert context.workspace_path == str(workspace_path.resolve())
        assert context.repo_root_path == str(repository_path.resolve())
        assert context.socket_path == str(socket_path)

        with get_db() as database:
            database.execute(
                "UPDATE workspaces SET path = ? WHERE id = ?",
                (str(tmp_path.parent), "workspace-id"),
            )
            database.commit()
        with pytest.raises(PermissionError, match="managed storage"):
            resolve_desktop_terminal_context("workspace-id")
    finally:
        get_settings.cache_clear()


def test_desktop_terminal_route_uses_local_sidecar_without_browser_session(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Desktop WebSocket should trust bootstrap middleware and bypass hosted containers."""
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
    workspace_path.mkdir()
    monkeypatch.setenv("ALLOWED_REPO_BASE", str(repository_base))
    monkeypatch.setenv("SIDECAR_SOCKET_PATH", str(tmp_path / "run" / "sidecar.sock"))
    monkeypatch.setenv("FRONTEND_URL", "http://testserver")
    (tmp_path / "index.html").write_text("<main>Desktop</main>", encoding="utf-8")
    get_settings.cache_clear()
    from yinshi.main import create_app

    captured: dict[str, object] = {}

    async def fake_desktop_proxy(websocket, context, workspace_id: str) -> None:
        captured["context"] = context
        captured["workspace_id"] = workspace_id
        await websocket.accept()
        await websocket.close()

    monkeypatch.setattr(
        "yinshi.api.terminals._run_desktop_terminal_proxy",
        fake_desktop_proxy,
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
            with client.websocket_connect(
                "/api/workspaces/workspace-id/terminal",
                headers={"Origin": "http://testserver"},
            ):
                pass
        assert captured["workspace_id"] == "workspace-id"
        context = captured["context"]
        assert getattr(context, "workspace_path") == str(workspace_path.resolve())
    finally:
        get_settings.cache_clear()
