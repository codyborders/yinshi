"""Tests for sidecar runtime mount resolution."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Request

from yinshi.exceptions import ContainerNotReadyError
from yinshi.services.container import ContainerInfo, ContainerManager, ContainerMount
from yinshi.services.sidecar_runtime import (
    _container_mounts_for_runtime,
    resolve_tenant_sidecar_context,
    tenant_container_activity,
    workspace_runtime_environment,
)
from yinshi.tenant import TenantContext


@pytest.mark.asyncio
async def test_sidecar_local_preparation_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Runtime configuration should leave unrelated async work responsive."""
    from yinshi.config import Settings
    from yinshi.services import sidecar_runtime
    from yinshi.services.pi_config import PiRuntimeInputs

    settings = Settings(
        container_enabled=False,
        google_client_id="test-client",
        google_client_secret="test-secret",
        managed_runtime_provider="disabled",
        _env_file=None,
    )
    tenant = TenantContext(
        user_id="a" * 32,
        email="test@example.com",
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "yinshi.db"),
    )
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(container_manager=None)),
        }
    )
    release_operation = threading.Event()
    stop_ticker = asyncio.Event()
    ticks = 0

    def blocking_runtime(*_args: object, **_kwargs: object) -> PiRuntimeInputs:
        assert release_operation.wait(timeout=2)
        return PiRuntimeInputs(agent_dir=None, settings_payload={"model": "test"})

    async def ticker() -> None:
        nonlocal ticks
        while not stop_ticker.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    monkeypatch.setattr(sidecar_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(sidecar_runtime, "resolve_effective_pi_runtime", blocking_runtime)
    release_timer = threading.Timer(0.2, release_operation.set)
    release_timer.start()
    ticker_task = asyncio.create_task(ticker())
    try:
        context = await resolve_tenant_sidecar_context(request, tenant)
    finally:
        stop_ticker.set()
        await ticker_task
        release_timer.cancel()

    assert context.settings_payload == {"model": "test"}
    assert ticks >= 5


@pytest.mark.asyncio
async def test_tenant_activity_never_enters_runtime_selected_for_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Activity waits for removal and rejects the retired runtime generation."""
    from yinshi.config import Settings

    settings = Settings(
        container_enabled=True,
        google_client_id="test-client",
        google_client_secret="test-secret",
        managed_runtime_provider="disabled",
        _env_file=None,
    )
    monkeypatch.setattr("yinshi.services.sidecar_runtime.get_settings", lambda: settings)
    manager = ContainerManager(settings=settings)
    tenant = TenantContext(
        user_id="a" * 32,
        email="test@example.com",
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "yinshi.db"),
    )
    info = ContainerInfo(
        container_id="retiring-container",
        user_id=tenant.user_id,
        socket_path="/tmp/retiring.sock",
    )
    manager._containers[tenant.user_id] = info
    removal_started = asyncio.Event()
    finish_removal = asyncio.Event()
    work_container_ids: list[str] = []

    async def remove_container(container_id: str) -> bool:
        assert container_id == info.container_id
        removal_started.set()
        await finish_removal.wait()
        return True

    manager._remove_container = AsyncMock(side_effect=remove_container)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(container_manager=manager),
            ),
        }
    )

    async def run_activity() -> None:
        try:
            async with tenant_container_activity(request, tenant):
                work_container_ids.append(info.container_id)
        except ContainerNotReadyError:
            return

    destroy_task = asyncio.create_task(manager.destroy_container(tenant.user_id))
    await removal_started.wait()
    activity_task = asyncio.create_task(run_activity())
    await asyncio.sleep(0)

    assert not activity_task.done()
    assert work_container_ids == []
    finish_removal.set()
    destroyed, _ = await asyncio.gather(destroy_task, activity_task)

    assert destroyed is True
    assert work_container_ids == []


@pytest.mark.asyncio
async def test_tenant_activity_installs_protection_before_releasing_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Protection closes the removal window before active work is released."""
    from yinshi.config import Settings

    settings = Settings(
        container_enabled=True,
        google_client_id="test-client",
        google_client_secret="test-secret",
        managed_runtime_provider="disabled",
        _env_file=None,
    )
    monkeypatch.setattr("yinshi.services.sidecar_runtime.get_settings", lambda: settings)
    tenant = TenantContext(
        user_id="a" * 32,
        email="test@example.com",
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "yinshi.db"),
    )
    reservation = object()
    cleanup_order: list[str] = []
    manager = AsyncMock()
    manager.acquire_activity.return_value = reservation
    manager.protect = Mock(side_effect=lambda *_args, **_kwargs: cleanup_order.append("protect"))
    manager.release_activity = AsyncMock(
        side_effect=lambda *_args, **_kwargs: cleanup_order.append("release")
    )
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(container_manager=manager)),
        }
    )

    async with tenant_container_activity(
        request,
        tenant,
        protect_lease_key="oauth:test-flow",
        protect_timeout_s=30,
    ):
        pass

    assert cleanup_order == ["protect", "release"]
    manager.release_activity.assert_awaited_once_with(reservation)


def test_container_mounts_include_repo_host_path_for_git_worktrees(
    tmp_path: Path,
) -> None:
    """Repo mounts should support Git worktree metadata inside sidecars.

    Linked worktrees keep absolute gitdir pointers into repository metadata.
    The agent works below /data, but Git still follows those absolute pointers.
    Mounting the repo at both /data and its host path preserves narrow mounts
    without rewriting Git metadata.
    """
    data_dir = tmp_path / "tenant"
    repo_path = data_dir / "repos" / "yinshi"
    workspace_path = repo_path / ".worktrees" / "codyborders" / "azure-fox"
    workspace_path.mkdir(parents=True)
    tenant = TenantContext(
        user_id="a" * 32,
        email="test@example.com",
        data_dir=str(data_dir),
        db_path=str(data_dir / "yinshi.db"),
    )

    mounts = _container_mounts_for_runtime(
        tenant,
        agent_dir=None,
        repo_root_path=str(repo_path),
        workspace_path=str(workspace_path),
        workspace_id=None,
    )

    assert (
        ContainerMount(
            source_path=str(repo_path.resolve()),
            target_path="/data/repos/yinshi",
            read_only=False,
        )
        in mounts
    )
    assert (
        ContainerMount(
            source_path=str(repo_path.resolve()),
            target_path=str(repo_path.resolve()),
            read_only=False,
        )
        in mounts
    )
    assert (
        ContainerMount(
            source_path=str(workspace_path.resolve()),
            target_path="/data/repos/yinshi/.worktrees/codyborders/azure-fox",
            read_only=False,
        )
        in mounts
    )


def test_workspace_runtime_mounts_include_persistent_home(tmp_path: Path) -> None:
    """Workspace runtimes should mount a durable home shared by terminal and agent."""
    workspace_id = "b" * 32
    data_dir = tmp_path / "tenant"
    repo_path = data_dir / "repos" / "yinshi"
    workspace_path = repo_path / ".worktrees" / "branch"
    workspace_path.mkdir(parents=True)
    tenant = TenantContext(
        user_id="a" * 32,
        email="test@example.com",
        data_dir=str(data_dir),
        db_path=str(data_dir / "yinshi.db"),
    )

    mounts = _container_mounts_for_runtime(
        tenant,
        agent_dir=None,
        repo_root_path=str(repo_path),
        workspace_path=str(workspace_path),
        workspace_id=workspace_id,
    )
    env = workspace_runtime_environment(workspace_id)

    home_path = data_dir / "runtime" / "workspaces" / workspace_id / "home"
    assert home_path.is_dir()
    assert (home_path / "bin").is_dir()
    assert (
        ContainerMount(
            source_path=str(home_path.resolve()),
            target_path="/home/yinshi",
            read_only=False,
        )
        in mounts
    )
    assert env is not None
    assert env["HOME"] == "/home/yinshi"
    assert "/home/yinshi/bin" in env["PATH"]
