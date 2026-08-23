"""Workspace file APIs hide secrets, expose Git status, and guard path access."""

from __future__ import annotations

import asyncio
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _create_workspace(client: TestClient, git_repo: str) -> dict[str, str]:
    """Create a repo and workspace through the public API."""
    repo = client.post(
        "/api/repos",
        json={"name": "demo", "local_path": git_repo},
    ).json()
    workspace = client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json()
    return workspace


@pytest.mark.asyncio
async def test_workspace_tree_filesystem_work_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tree construction should leave unrelated async work responsive."""
    from yinshi.api import workspace_files

    application = FastAPI()
    application.include_router(workspace_files.router)
    release_operation = threading.Event()
    stop_ticker = asyncio.Event()
    ticks = 0

    async def prepare_workspace(*_args: object) -> str:
        return str(tmp_path)

    def build_tree(_workspace_path: str):
        assert release_operation.wait(timeout=2)
        return []

    async def ticker() -> None:
        nonlocal ticks
        while not stop_ticker.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    monkeypatch.setattr(workspace_files, "_prepare_workspace_files", prepare_workspace)
    monkeypatch.setattr(workspace_files, "build_file_tree", build_tree)
    release_timer = threading.Timer(0.2, release_operation.set)
    release_timer.start()
    ticker_task = asyncio.create_task(ticker())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/workspaces/workspace-id/files/tree")
    finally:
        stop_ticker.set()
        await ticker_task
        release_timer.cancel()

    assert response.status_code == 200, response.text
    assert ticks >= 5


@pytest.mark.asyncio
async def test_workspace_tree_database_open_does_not_block_event_loop(
    noauth_client: TestClient,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Database opening should leave unrelated async work responsive."""
    from yinshi.api import deps, workspace_files

    workspace = _create_workspace(noauth_client, git_repo)
    application = FastAPI()
    application.include_router(workspace_files.router)
    original_database_for_request = deps.get_db_for_request
    release_operation = threading.Event()
    stop_ticker = asyncio.Event()
    ticks = 0

    @contextmanager
    def blocking_database_for_request(request: object):
        assert release_operation.wait(timeout=2)
        with original_database_for_request(request) as database:
            yield database

    async def ticker() -> None:
        nonlocal ticks
        while not stop_ticker.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    monkeypatch.setattr(deps, "get_db_for_request", blocking_database_for_request)
    monkeypatch.setattr(
        workspace_files,
        "get_db_for_request",
        blocking_database_for_request,
        raising=False,
    )
    release_timer = threading.Timer(0.2, release_operation.set)
    release_timer.start()
    ticker_task = asyncio.create_task(ticker())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.get(f"/api/workspaces/{workspace['id']}/files/tree")
    finally:
        stop_ticker.set()
        await ticker_task
        release_timer.cancel()

    assert response.status_code == 200, response.text
    assert ticks >= 5


def test_stable_download_closes_descriptor_when_stat_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed descriptor stat must close the opened download file."""
    from yinshi.api import workspace_files

    closed_descriptors: list[int] = []

    @contextmanager
    def fake_parent(_workspace_path: str, _path: str):
        yield 10, "file.txt"

    monkeypatch.setattr(workspace_files, "_open_workspace_parent", fake_parent)
    monkeypatch.setattr(workspace_files.os, "open", lambda *_args, **_kwargs: 99)
    monkeypatch.setattr(
        workspace_files.os,
        "fstat",
        lambda _descriptor: (_ for _ in ()).throw(OSError("stat failed")),
    )
    monkeypatch.setattr(workspace_files.os, "close", closed_descriptors.append)

    with pytest.raises(OSError, match="stat failed"):
        workspace_files._open_stable_download("/workspace", "file.txt")

    assert closed_descriptors == [99]


def test_stable_download_closes_descriptor_when_file_wrapper_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed file wrapper must close the opened download descriptor."""
    from yinshi.api import workspace_files

    closed_descriptors: list[int] = []

    @contextmanager
    def fake_parent(_workspace_path: str, _path: str):
        yield 10, "file.txt"

    monkeypatch.setattr(workspace_files, "_open_workspace_parent", fake_parent)
    monkeypatch.setattr(workspace_files.os, "open", lambda *_args, **_kwargs: 99)
    monkeypatch.setattr(
        workspace_files.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=0o100600),
    )
    monkeypatch.setattr(
        workspace_files.os,
        "fdopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("wrapper failed")),
    )
    monkeypatch.setattr(workspace_files.os, "close", closed_descriptors.append)

    with pytest.raises(OSError, match="wrapper failed"):
        workspace_files._open_stable_download("/workspace", "file.txt")

    assert closed_descriptors == [99]


@pytest.mark.asyncio
async def test_changed_files_path_resolution_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changed-file polling should leave unrelated async work responsive."""
    from yinshi.services import workspace_files as file_service

    release_operation = threading.Event()
    stop_ticker = asyncio.Event()
    ticks = 0

    def blocking_root(_workspace_path: str) -> Path:
        assert release_operation.wait(timeout=2)
        return tmp_path

    class SuccessfulProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def create_process(*_args: object, **_kwargs: object) -> SuccessfulProcess:
        return SuccessfulProcess()

    async def ticker() -> None:
        nonlocal ticks
        while not stop_ticker.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    monkeypatch.setattr(file_service, "_workspace_root", blocking_root)
    monkeypatch.setattr(file_service.asyncio, "create_subprocess_exec", create_process)
    release_timer = threading.Timer(0.2, release_operation.set)
    release_timer.start()
    ticker_task = asyncio.create_task(ticker())
    try:
        changes = await file_service.changed_files(str(tmp_path))
    finally:
        stop_ticker.set()
        await ticker_task
        release_timer.cancel()

    assert changes == ()
    assert ticks >= 5


@pytest.mark.asyncio
async def test_workspace_diff_file_read_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diff worktree reads should leave unrelated async work responsive."""
    from yinshi.services import workspace_files as file_service

    release_operation = threading.Event()
    stop_ticker = asyncio.Event()
    ticks = 0

    def blocking_read(_workspace_path: str, _display_path: str) -> str:
        assert release_operation.wait(timeout=2)
        return "current\n"

    async def changed_file(_root: Path, _display_path: str):
        return file_service.ChangedFile(path="README.md", status=" M", kind="modified")

    async def head_text(_root: Path, _display_path: str) -> str:
        return "committed\n"

    async def ticker() -> None:
        nonlocal ticks
        while not stop_ticker.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    monkeypatch.setattr(file_service, "_workspace_root", lambda _path: tmp_path)
    monkeypatch.setattr(
        file_service,
        "validate_visible_relative_path",
        lambda _workspace_path, _relative_path: tmp_path / "README.md",
    )
    monkeypatch.setattr(file_service, "_changed_file_for_path", changed_file)
    monkeypatch.setattr(file_service, "read_text_file", blocking_read)
    monkeypatch.setattr(file_service, "_head_file_text", head_text)
    release_timer = threading.Timer(0.2, release_operation.set)
    release_timer.start()
    ticker_task = asyncio.create_task(ticker())
    try:
        diff = await file_service.diff_file(str(tmp_path), "README.md")
    finally:
        stop_ticker.set()
        await ticker_task
        release_timer.cancel()

    assert "+current" in diff
    assert ticks >= 5


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_name", ["status", "path-status", "object-read"])
async def test_workspace_git_cancellation_kills_and_drains_child(
    operation_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled workspace Git work should kill and drain its piped child."""
    from yinshi.services import workspace_files as file_service

    communication_started = asyncio.Event()
    calls: list[str] = []

    class BlockingProcess:
        returncode: int | None = None
        communication_count = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communication_count += 1
            calls.append("communicate")
            if self.communication_count == 1:
                communication_started.set()
                await asyncio.Event().wait()
            calls.append("drained")
            return b"", b""

        def kill(self) -> None:
            calls.append("kill")
            self.returncode = -9

    process = BlockingProcess()

    async def create_process(*_args: object, **_kwargs: object) -> BlockingProcess:
        return process

    monkeypatch.setattr(file_service.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(file_service, "_workspace_root", lambda _path: tmp_path)
    if operation_name == "status":
        operation = file_service.changed_files(str(tmp_path))
    elif operation_name == "path-status":
        operation = file_service._changed_file_for_path(tmp_path, "README.md")
    else:
        operation = file_service._head_file_text(tmp_path, "README.md")

    task = asyncio.create_task(operation)
    await communication_started.wait()
    task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert calls == ["communicate", "kill", "communicate", "drained"]


def test_workspace_file_tree_hides_env_and_dependency_dirs(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """File tree should show source files while hiding secrets and noisy directories."""
    workspace = _create_workspace(noauth_client, git_repo)
    workspace_path = Path(workspace["path"])
    (workspace_path / "src").mkdir()
    (workspace_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (workspace_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (workspace_path / "node_modules").mkdir()
    (workspace_path / "node_modules" / "package.js").write_text("bad\n", encoding="utf-8")

    response = noauth_client.get(f"/api/workspaces/{workspace['id']}/files/tree")

    assert response.status_code == 200
    payload = response.json()
    serialized = repr(payload)
    assert "app.py" in serialized
    assert ".env" not in serialized
    assert "node_modules" not in serialized


def test_workspace_changed_files_clear_after_commit(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """Changed files endpoint should reflect current worktree Git status."""
    workspace = _create_workspace(noauth_client, git_repo)
    workspace_path = Path(workspace["path"])
    readme_path = workspace_path / "README.md"
    readme_path.write_text("# Test\n\nChanged\n", encoding="utf-8")

    changed_response = noauth_client.get(f"/api/workspaces/{workspace['id']}/files/changed")
    assert changed_response.status_code == 200
    assert changed_response.json()["files"] == [
        {
            "path": "README.md",
            "status": " M",
            "kind": "modified",
            "original_path": None,
        }
    ]

    subprocess.run(["git", "add", "README.md"], cwd=workspace_path, check=True)
    subprocess.run(["git", "commit", "-m", "update readme"], cwd=workspace_path, check=True)

    cleared_response = noauth_client.get(f"/api/workspaces/{workspace['id']}/files/changed")
    assert cleared_response.status_code == 200
    assert cleared_response.json()["files"] == []


def test_workspace_file_diff_compares_stable_file_with_git_head(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """Diff endpoint should compare a stable worktree read with committed object data."""
    workspace = _create_workspace(noauth_client, git_repo)
    workspace_path = Path(workspace["path"])
    (workspace_path / "README.md").write_text("# Test\n\nChanged\n", encoding="utf-8")

    response = noauth_client.get(
        f"/api/workspaces/{workspace['id']}/files/diff",
        params={"path": "README.md"},
    )

    assert response.status_code == 200
    diff = response.json()["diff"]
    assert "--- a/README.md" in diff
    assert "+++ b/README.md" in diff
    assert "+Changed" in diff


def test_workspace_file_read_never_follows_concurrent_parent_symlink(tmp_path: Path) -> None:
    """A parent-directory swap must not redirect a preview outside the workspace."""
    from yinshi.services.workspace_files import read_text_file

    workspace_path = tmp_path / "workspace"
    live_directory = workspace_path / "switch"
    saved_directory = workspace_path / "switch-saved"
    outside_directory = tmp_path / "outside"
    live_directory.mkdir(parents=True)
    outside_directory.mkdir()
    (live_directory / "target.txt").write_text("inside", encoding="utf-8")
    (outside_directory / "target.txt").write_text("outside", encoding="utf-8")

    stop_event = threading.Event()

    def swap_parent() -> None:
        while not stop_event.is_set():
            try:
                live_directory.rename(saved_directory)
                live_directory.symlink_to(outside_directory, target_is_directory=True)
                live_directory.unlink()
                saved_directory.rename(live_directory)
            except FileNotFoundError:
                continue

    attacker = threading.Thread(target=swap_parent)
    attacker.start()
    try:
        for _ in range(2000):
            try:
                content = read_text_file(str(workspace_path), "switch/target.txt")
            except (FileNotFoundError, PermissionError, ValueError):
                continue
            assert content == "inside"
    finally:
        stop_event.set()
        attacker.join(timeout=2)


def test_workspace_file_preview_rejects_env_and_path_traversal(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """Preview endpoint should reject secret files and paths outside the worktree."""
    workspace = _create_workspace(noauth_client, git_repo)
    workspace_path = Path(workspace["path"])
    (workspace_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    env_response = noauth_client.get(
        f"/api/workspaces/{workspace['id']}/files/preview",
        params={"path": ".env"},
    )
    traversal_response = noauth_client.get(
        f"/api/workspaces/{workspace['id']}/files/preview",
        params={"path": "../README.md"},
    )

    assert env_response.status_code == 403
    assert traversal_response.status_code == 400


def test_workspace_file_preview_rejects_tenant_path_outside_storage(
    auth_client: TestClient,
    git_repo: str,
    tmp_path: Path,
) -> None:
    """Tenant file APIs should reject workspace rows that point outside tenant storage."""
    workspace = _create_workspace(auth_client, git_repo)
    outside_path = tmp_path / "outside-workspace"
    outside_path.mkdir()
    (outside_path / "README.md").write_text("# Outside\n", encoding="utf-8")

    from yinshi.tenant import get_user_db

    tenant = getattr(auth_client, "yinshi_tenant")
    with get_user_db(tenant) as db:
        db.execute(
            "UPDATE workspaces SET path = ? WHERE id = ?",
            (str(outside_path), workspace["id"]),
        )
        db.commit()

    response = auth_client.get(
        f"/api/workspaces/{workspace['id']}/files/preview",
        params={"path": "README.md"},
    )

    assert response.status_code == 403


def test_workspace_creation_installs_env_git_guardrails(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """Workspace creation should add repo-local Git excludes and commit hook for env files."""
    workspace = _create_workspace(noauth_client, git_repo)
    workspace_path = Path(workspace["path"])
    repo_path = Path(git_repo)
    exclude_text = (repo_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    hook_path = repo_path / ".git" / "hooks" / "pre-commit"
    push_hook_path = repo_path / ".git" / "hooks" / "pre-push"
    hook_text = hook_path.read_text(encoding="utf-8")
    push_hook_text = push_hook_path.read_text(encoding="utf-8")

    assert ".env" in exclude_text
    assert ".env.*" in exclude_text
    assert "Yinshi secret commit guard" in hook_text
    assert "Yinshi secret push guard" in push_hook_text

    (workspace_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".env"], cwd=workspace_path, check=True)
    commit = subprocess.run(
        ["git", "commit", "-m", "try env"],
        cwd=workspace_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert commit.returncode != 0
    assert "Yinshi blocks committing .env files" in commit.stderr
