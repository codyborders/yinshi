"""Shared workspace path preparation for tenant-scoped runtime features."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar, cast

from yinshi.exceptions import WorkspaceNotFoundError
from yinshi.services.repository_lifecycle import repository_lifecycle, repository_lifecycle_root
from yinshi.services.workspace import (
    WorkspaceCheckoutPreparation,
    WorkspaceCheckoutState,
    apply_workspace_checkout_preparation,
    load_workspace_checkout_state,
    prepare_workspace_checkout_for_tenant,
)
from yinshi.services.workspace_files import ensure_secret_guardrails
from yinshi.tenant import TenantContext
from yinshi.utils.paths import is_path_inside

_T = TypeVar("_T")


class DatabaseOperationRunner(Protocol):
    """Run one callback with a short-lived request database connection."""

    async def __call__(self, operation: Callable[[sqlite3.Connection], _T]) -> _T: ...


async def _run_local_operation(operation: Callable[[], _T]) -> _T:
    """Run local filesystem work and drain it after caller cancellation."""
    if not callable(operation):
        raise TypeError("operation must be callable")
    attempt = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(attempt)
    except asyncio.CancelledError:
        while not attempt.done():
            try:
                await asyncio.shield(attempt)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not attempt.cancelled():
            with suppress(BaseException):
                attempt.result()
        raise


@dataclass(frozen=True, slots=True)
class WorkspaceRuntimePaths:
    """Trusted host paths needed by workspace-scoped runtime features."""

    workspace_path: str
    repo_root_path: str
    agents_md: str | None


def _workspace_runtime_row(db: sqlite3.Connection, workspace_id: str) -> sqlite3.Row:
    """Load workspace plus repo path fields needed by runtime features."""
    if not isinstance(workspace_id, str):
        raise TypeError("workspace_id must be a string")
    normalized_workspace_id = workspace_id.strip()
    if not normalized_workspace_id:
        raise ValueError("workspace_id must not be empty")

    row = db.execute(
        "SELECT w.path, r.root_path, r.agents_md "
        "FROM workspaces w JOIN repos r ON w.repo_id = r.id WHERE w.id = ?",
        (normalized_workspace_id,),
    ).fetchone()
    if row is None:
        raise WorkspaceNotFoundError("Workspace not found")
    return cast(sqlite3.Row, row)


def _tenant_owned_path(path: str, tenant: TenantContext, path_name: str) -> str:
    """Return a real path after proving it stays inside tenant storage."""
    if not isinstance(path, str):
        raise TypeError(f"{path_name} must be a string")
    normalized_path = path.strip()
    if not normalized_path:
        raise ValueError(f"{path_name} must not be empty")
    real_path = os.path.realpath(normalized_path)
    if not is_path_inside(real_path, tenant.data_dir):
        raise PermissionError(f"{path_name} is outside tenant storage")
    return real_path


def _load_checkout_and_lock_root(
    db: sqlite3.Connection,
    tenant: TenantContext,
    workspace_id: str,
) -> tuple[WorkspaceCheckoutState, Path]:
    """Load checkout inputs and their process-shared lock root."""
    state = load_workspace_checkout_state(db, workspace_id)
    return state, repository_lifecycle_root(db, tenant)


def _apply_checkout_and_load_paths(
    db: sqlite3.Connection,
    preparation: WorkspaceCheckoutPreparation,
    workspace_id: str,
) -> sqlite3.Row:
    """Apply prepared metadata and load final runtime paths in one operation."""
    apply_workspace_checkout_preparation(db, preparation)
    return _workspace_runtime_row(db, workspace_id)


def _validate_runtime_paths(
    row: sqlite3.Row,
    tenant: TenantContext,
) -> WorkspaceRuntimePaths:
    """Validate tenant containment and install local secret guardrails."""
    workspace_path = _tenant_owned_path(str(row["path"]), tenant, "workspace path")
    repo_root_path = _tenant_owned_path(str(row["root_path"]), tenant, "repo root path")
    ensure_secret_guardrails(repo_root_path)
    agents_md = row["agents_md"]
    if agents_md is not None and not isinstance(agents_md, str):
        raise TypeError("agents_md must be a string or None")
    return WorkspaceRuntimePaths(
        workspace_path=workspace_path,
        repo_root_path=repo_root_path,
        agents_md=agents_md,
    )


async def prepare_tenant_workspace_runtime_paths(
    tenant: TenantContext,
    workspace_id: str,
    run_database_operation: DatabaseOperationRunner,
) -> WorkspaceRuntimePaths:
    """Repair and validate one tenant workspace without blocking the event loop."""
    if tenant is None:
        raise TypeError("tenant must not be None")
    if not callable(run_database_operation):
        raise TypeError("run_database_operation must be callable")

    checkout_state, lock_root = await run_database_operation(
        lambda db: _load_checkout_and_lock_root(db, tenant, workspace_id)
    )
    async with repository_lifecycle(checkout_state.repo_id, lock_root):
        locked_state = await run_database_operation(
            lambda db: load_workspace_checkout_state(db, workspace_id)
        )
        if locked_state.repo_id != checkout_state.repo_id:
            raise WorkspaceNotFoundError("Workspace repository changed during preparation")
        preparation = await prepare_workspace_checkout_for_tenant(tenant, locked_state)
        row = await run_database_operation(
            lambda db: _apply_checkout_and_load_paths(db, preparation, workspace_id)
        )
        return await _run_local_operation(lambda: _validate_runtime_paths(row, tenant))
