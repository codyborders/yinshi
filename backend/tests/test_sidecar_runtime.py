"""Tests for sidecar runtime mount resolution."""

from __future__ import annotations

from pathlib import Path

from yinshi.services.container import ContainerMount
from yinshi.services.sidecar_runtime import (
    _container_mounts_for_runtime,
    workspace_runtime_environment,
)
from yinshi.tenant import TenantContext


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
