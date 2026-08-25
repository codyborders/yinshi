"""Reconnectable terminal channels for encrypted and JSON runtime transports."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from yinshi.api.deps import get_tenant, run_db_operation_for_request
from yinshi.config import get_settings
from yinshi.exceptions import (
    ContainerNotReadyError,
    ContainerStartError,
    GitError,
    SidecarError,
    SidecarNotConnectedError,
    WorkspaceNotFoundError,
)
from yinshi.services.sidecar_runtime import (
    protect_tenant_container,
    release_tenant_container,
    remap_path_for_container,
    resolve_tenant_sidecar_context,
    tenant_container_activity,
)
from yinshi.services.terminal_journal import (
    TerminalCursorExpiredError,
    TerminalEventBatch,
    TerminalJournal,
    TerminalLimitError,
    TerminalNotFoundError,
)
from yinshi.services.workspace_runtime_paths import prepare_tenant_workspace_runtime_paths

router = APIRouter()


class TerminalStartRequest(BaseModel):
    cols: int = Field(default=100, ge=2, le=500)
    rows: int = Field(default=30, ge=2, le=500)
    owner_id: str | None = Field(
        default=None,
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
    )


class TerminalStartResponse(BaseModel):
    id: str
    workspace_id: str
    status: Literal["attached"]


class TerminalInputRequest(BaseModel):
    data: str = Field(..., min_length=1, max_length=16_384)


class TerminalResizeRequest(BaseModel):
    cols: int = Field(..., ge=2, le=500)
    rows: int = Field(..., ge=2, le=500)


class TerminalEventBatchResponse(BaseModel):
    terminal_id: str
    events: list[dict[str, Any]]
    next_sequence: int
    closed: bool


def _journal(request: Request) -> TerminalJournal:
    terminal_journal = getattr(request.app.state, "terminal_journal", None)
    if not isinstance(terminal_journal, TerminalJournal):
        raise RuntimeError("terminal journal is unavailable")
    return terminal_journal


def _tenant_identity(request: Request) -> tuple[str, Any]:
    tenant = get_tenant(request)
    if tenant is None:
        raise HTTPException(status_code=403, detail="Terminal account context is required")
    if not tenant.user_id:
        raise RuntimeError("terminal tenant user ID is empty")
    return tenant.user_id, tenant


async def _terminal_context(
    request: Request,
    workspace_id: str,
) -> tuple[str, Any, str, str, str | None]:
    user_id, tenant = _tenant_identity(request)
    try:
        paths = await prepare_tenant_workspace_runtime_paths(
            tenant,
            workspace_id,
            lambda operation: run_db_operation_for_request(request, operation),
        )
        runtime = await resolve_tenant_sidecar_context(
            request,
            tenant,
            repo_agents_md=paths.agents_md,
            repo_root_path=paths.repo_root_path,
            workspace_path=paths.workspace_path,
            workspace_id=workspace_id,
        )
        if runtime.socket_path is None:
            socket_path = get_settings().sidecar_socket_path
            effective_cwd = paths.workspace_path
        else:
            socket_path = runtime.socket_path
            effective_cwd = await asyncio.to_thread(
                remap_path_for_container,
                paths.workspace_path,
                tenant.data_dir,
            )
    except (PermissionError, WorkspaceNotFoundError):
        raise HTTPException(status_code=404, detail="Workspace not found") from None
    except (ContainerStartError, ContainerNotReadyError, GitError, OSError, ValueError):
        raise HTTPException(status_code=503, detail="Terminal runtime unavailable") from None
    return user_id, tenant, socket_path, effective_cwd, runtime.runtime_id


def _batch_response(batch: TerminalEventBatch) -> TerminalEventBatchResponse:
    return TerminalEventBatchResponse(
        terminal_id=batch.terminal_id,
        events=list(batch.events),
        next_sequence=batch.next_sequence,
        closed=batch.closed,
    )


def _terminal_lease_key(workspace_id: str, terminal_id: str) -> str:
    return f"terminal:{workspace_id}:{terminal_id}"


def _refresh_terminal_lease(
    request: Request,
    tenant: Any,
    workspace_id: str,
    terminal_id: str,
) -> None:
    protect_tenant_container(
        request,
        tenant,
        lease_key=_terminal_lease_key(workspace_id, terminal_id),
        timeout_s=get_settings().terminal_keepalive_s,
        runtime_id=workspace_id,
    )


def _release_terminal_lease(
    request: Request,
    tenant: Any,
    workspace_id: str,
    terminal_id: str,
) -> None:
    release_tenant_container(
        request,
        tenant,
        lease_key=_terminal_lease_key(workspace_id, terminal_id),
        runtime_id=workspace_id,
    )


@asynccontextmanager
async def _terminal_runtime_activity(
    request: Request,
    tenant: Any,
    workspace_id: str,
) -> AsyncIterator[None]:
    try:
        async with tenant_container_activity(
            request,
            tenant,
            runtime_id=workspace_id,
        ):
            yield
    except ContainerNotReadyError:
        raise HTTPException(
            status_code=503,
            detail="Terminal runtime unavailable",
        ) from None


@router.post(
    "/api/workspaces/{workspace_id}/terminals",
    response_model=TerminalStartResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_terminal_channel(
    workspace_id: str,
    body: TerminalStartRequest,
    request: Request,
) -> TerminalStartResponse:
    """Attach one account-scoped terminal to the workspace sidecar."""
    user_id, tenant, socket_path, cwd, runtime_id = await _terminal_context(
        request,
        workspace_id,
    )
    try:
        async with tenant_container_activity(
            request,
            tenant,
            runtime_id=runtime_id,
        ):
            start_task = asyncio.create_task(
                _journal(request).start(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    socket_path=socket_path,
                    cwd=cwd,
                    cols=body.cols,
                    rows=body.rows,
                    owner_id=body.owner_id,
                )
            )
            cancellation: asyncio.CancelledError | None = None
            while True:
                try:
                    start_result = await asyncio.shield(start_task)
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
                    if not start_task.done():
                        continue
                    try:
                        start_result = start_task.result()
                    except BaseException:
                        raise cancellation
                break
            terminal_id = start_result.terminal_id
            _refresh_terminal_lease(request, tenant, workspace_id, terminal_id)
            if start_result.replaced_terminal_id is not None:
                if start_result.replaced_workspace_id is None:
                    raise RuntimeError("replaced terminal workspace is missing")
                _release_terminal_lease(
                    request,
                    tenant,
                    start_result.replaced_workspace_id,
                    start_result.replaced_terminal_id,
                )
            elif start_result.replaced_workspace_id is not None:
                raise RuntimeError("replaced terminal ID is missing")
            if cancellation is not None:
                raise cancellation
    except TerminalLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except (
        ConnectionError,
        ContainerNotReadyError,
        OSError,
        SidecarError,
        SidecarNotConnectedError,
    ):
        raise HTTPException(status_code=503, detail="Terminal runtime unavailable") from None
    return TerminalStartResponse(
        id=terminal_id,
        workspace_id=workspace_id,
        status="attached",
    )


@router.post("/api/workspaces/{workspace_id}/terminals/{terminal_id}/input", status_code=204)
async def send_terminal_input(
    workspace_id: str,
    terminal_id: str,
    body: TerminalInputRequest,
    request: Request,
) -> None:
    """Forward one bounded input fragment to an attached terminal."""
    user_id, tenant = _tenant_identity(request)
    try:
        async with _terminal_runtime_activity(request, tenant, workspace_id):
            await _journal(request).input(
                user_id=user_id,
                workspace_id=workspace_id,
                terminal_id=terminal_id,
                data=body.data,
            )
            _refresh_terminal_lease(request, tenant, workspace_id, terminal_id)
    except TerminalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Terminal not found") from exc


@router.post("/api/workspaces/{workspace_id}/terminals/{terminal_id}/resize", status_code=204)
async def resize_terminal_channel(
    workspace_id: str,
    terminal_id: str,
    body: TerminalResizeRequest,
    request: Request,
) -> None:
    """Resize one attached terminal."""
    user_id, tenant = _tenant_identity(request)
    try:
        async with _terminal_runtime_activity(request, tenant, workspace_id):
            await _journal(request).resize(
                user_id=user_id,
                workspace_id=workspace_id,
                terminal_id=terminal_id,
                cols=body.cols,
                rows=body.rows,
            )
            _refresh_terminal_lease(request, tenant, workspace_id, terminal_id)
    except TerminalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Terminal not found") from exc


@router.post("/api/workspaces/{workspace_id}/terminals/{terminal_id}/restart", status_code=204)
async def restart_terminal_channel(
    workspace_id: str,
    terminal_id: str,
    request: Request,
) -> None:
    """Restart one attached terminal with its original options."""
    user_id, tenant = _tenant_identity(request)
    try:
        async with _terminal_runtime_activity(request, tenant, workspace_id):
            await _journal(request).restart(
                user_id=user_id,
                workspace_id=workspace_id,
                terminal_id=terminal_id,
            )
            _refresh_terminal_lease(request, tenant, workspace_id, terminal_id)
    except TerminalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Terminal not found") from exc


@router.get(
    "/api/workspaces/{workspace_id}/terminals/{terminal_id}/events/{next_sequence}",
    response_model=TerminalEventBatchResponse,
)
async def get_terminal_events(
    workspace_id: str,
    terminal_id: str,
    next_sequence: int,
    request: Request,
) -> TerminalEventBatchResponse:
    """Poll one contiguous bounded terminal-output page."""
    user_id, tenant = _tenant_identity(request)
    try:
        async with _terminal_runtime_activity(request, tenant, workspace_id):
            batch = await _journal(request).events(
                user_id=user_id,
                workspace_id=workspace_id,
                terminal_id=terminal_id,
                next_sequence=next_sequence,
            )
            if batch.closed:
                _release_terminal_lease(request, tenant, workspace_id, terminal_id)
            else:
                _refresh_terminal_lease(request, tenant, workspace_id, terminal_id)
    except TerminalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Terminal not found") from exc
    except TerminalCursorExpiredError as exc:
        raise HTTPException(status_code=409, detail="Terminal output cursor expired") from exc
    return _batch_response(batch)


@router.delete(
    "/api/workspaces/{workspace_id}/terminals/{terminal_id}",
    status_code=204,
)
async def close_terminal_channel(
    workspace_id: str,
    terminal_id: str,
    request: Request,
) -> None:
    """Detach one terminal channel idempotently."""
    user_id, tenant = _tenant_identity(request)
    await _journal(request).close(
        user_id=user_id,
        workspace_id=workspace_id,
        terminal_id=terminal_id,
    )
    _release_terminal_lease(request, tenant, workspace_id, terminal_id)
