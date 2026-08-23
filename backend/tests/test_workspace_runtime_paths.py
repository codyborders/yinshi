"""Workspace runtime path tests cover staged database and checkout preparation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from yinshi.exceptions import WorkspaceNotFoundError
from yinshi.services import workspace_runtime_paths
from yinshi.services.workspace import WorkspaceCheckoutPreparation, WorkspaceCheckoutState
from yinshi.tenant import TenantContext


def _tenant(tmp_path: Path) -> TenantContext:
    data_dir = tmp_path / "tenant"
    data_dir.mkdir()
    return TenantContext(
        user_id="user-id",
        email="user@example.com",
        data_dir=str(data_dir),
        db_path=str(data_dir / "yinshi.db"),
    )


def _state(repo_id: str = "repo-id") -> WorkspaceCheckoutState:
    return WorkspaceCheckoutState(
        workspace_id="workspace-id",
        repo_id=repo_id,
        repo_path="/source/repo",
        remote_url=None,
        installation_id=None,
        workspaces=(("workspace-id", "branch-name"),),
    )


@pytest.mark.asyncio
async def test_runtime_checkout_preparation_runs_without_open_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async checkout and local validation should run outside database callbacks."""
    tenant = _tenant(tmp_path)
    repo_path = Path(tenant.data_dir) / "repos" / "repo-id"
    workspace_path = repo_path / "worktrees" / "branch-name"
    workspace_path.mkdir(parents=True)
    database_active = False
    operation_count = 0

    async def run_database_operation(operation):
        nonlocal database_active, operation_count
        assert database_active is False
        database_active = True
        operation_count += 1
        try:
            return operation(object())
        finally:
            database_active = False

    def load_state(_database: object, _workspace_id: str) -> WorkspaceCheckoutState:
        assert database_active is True
        return _state()

    @asynccontextmanager
    async def lifecycle(_repo_id: str, _lock_root: Path):
        yield

    async def prepare_checkout(
        _tenant: TenantContext,
        state: WorkspaceCheckoutState,
    ) -> WorkspaceCheckoutPreparation:
        assert database_active is False
        return WorkspaceCheckoutPreparation(
            workspace_id=state.workspace_id,
            repo_id=state.repo_id,
            repo_path=str(repo_path),
            remote_url=state.remote_url,
            installation_id=state.installation_id,
            workspace_paths=((state.workspace_id, str(workspace_path)),),
            update_repo_metadata=True,
            repaired_repo=True,
        )

    def apply_checkout(_database: object, _preparation: object) -> dict[str, object]:
        assert database_active is True
        return {}

    def runtime_row(_database: object, _workspace_id: str) -> dict[str, object]:
        assert database_active is True
        return {
            "path": str(workspace_path),
            "root_path": str(repo_path),
            "agents_md": None,
        }

    def install_guardrails(_repo_path: str) -> None:
        assert database_active is False

    monkeypatch.setattr(workspace_runtime_paths, "load_workspace_checkout_state", load_state)
    monkeypatch.setattr(
        workspace_runtime_paths,
        "repository_lifecycle_root",
        lambda _database, _tenant: Path(tenant.data_dir),
    )
    monkeypatch.setattr(workspace_runtime_paths, "repository_lifecycle", lifecycle)
    monkeypatch.setattr(
        workspace_runtime_paths,
        "prepare_workspace_checkout_for_tenant",
        prepare_checkout,
    )
    monkeypatch.setattr(
        workspace_runtime_paths,
        "apply_workspace_checkout_preparation",
        apply_checkout,
    )
    monkeypatch.setattr(workspace_runtime_paths, "_workspace_runtime_row", runtime_row)
    monkeypatch.setattr(workspace_runtime_paths, "ensure_secret_guardrails", install_guardrails)

    paths = await workspace_runtime_paths.prepare_tenant_workspace_runtime_paths(
        tenant,
        "workspace-id",
        run_database_operation,
    )

    assert operation_count == 3
    assert paths.workspace_path == str(workspace_path)
    assert paths.repo_root_path == str(repo_path)


@pytest.mark.asyncio
async def test_runtime_checkout_rejects_repository_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preparation should stop when a workspace moves to another repository."""
    tenant = _tenant(tmp_path)
    states = iter((_state("repo-one"), _state("repo-two")))

    async def run_database_operation(operation):
        return operation(object())

    @asynccontextmanager
    async def lifecycle(_repo_id: str, _lock_root: Path):
        yield

    monkeypatch.setattr(
        workspace_runtime_paths,
        "load_workspace_checkout_state",
        lambda _database, _workspace_id: next(states),
    )
    monkeypatch.setattr(
        workspace_runtime_paths,
        "repository_lifecycle_root",
        lambda _database, _tenant: Path(tenant.data_dir),
    )
    monkeypatch.setattr(workspace_runtime_paths, "repository_lifecycle", lifecycle)

    with pytest.raises(WorkspaceNotFoundError, match="repository changed"):
        await workspace_runtime_paths.prepare_tenant_workspace_runtime_paths(
            tenant,
            "workspace-id",
            run_database_operation,
        )
