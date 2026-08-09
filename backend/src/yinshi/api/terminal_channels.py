"""Reconnectable terminal channels for encrypted and JSON runtime transports."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from yinshi.api.deps import get_db_for_request, get_tenant
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
    remap_path_for_container,
    resolve_tenant_sidecar_context,
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
) -> tuple[str, str, str]:
    user_id, tenant = _tenant_identity(request)
    try:
        with get_db_for_request(request) as database:
            paths = await prepare_tenant_workspace_runtime_paths(
                database,
                tenant,
                workspace_id,
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
            effective_cwd = remap_path_for_container(paths.workspace_path, tenant.data_dir)
    except (PermissionError, WorkspaceNotFoundError):
        raise HTTPException(status_code=404, detail="Workspace not found") from None
    except (ContainerStartError, ContainerNotReadyError, GitError, OSError, ValueError):
        raise HTTPException(status_code=503, detail="Terminal runtime unavailable") from None
    return user_id, socket_path, effective_cwd


def _batch_response(batch: TerminalEventBatch) -> TerminalEventBatchResponse:
    return TerminalEventBatchResponse(
        terminal_id=batch.terminal_id,
        events=list(batch.events),
        next_sequence=batch.next_sequence,
        closed=batch.closed,
    )


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
    user_id, socket_path, cwd = await _terminal_context(request, workspace_id)
    try:
        terminal_id = await _journal(request).start(
            user_id=user_id,
            workspace_id=workspace_id,
            socket_path=socket_path,
            cwd=cwd,
            cols=body.cols,
            rows=body.rows,
        )
    except TerminalLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except (ConnectionError, OSError, SidecarError, SidecarNotConnectedError):
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
    user_id, _tenant = _tenant_identity(request)
    try:
        await _journal(request).input(
            user_id=user_id,
            workspace_id=workspace_id,
            terminal_id=terminal_id,
            data=body.data,
        )
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
    user_id, _tenant = _tenant_identity(request)
    try:
        await _journal(request).resize(
            user_id=user_id,
            workspace_id=workspace_id,
            terminal_id=terminal_id,
            cols=body.cols,
            rows=body.rows,
        )
    except TerminalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Terminal not found") from exc


@router.post("/api/workspaces/{workspace_id}/terminals/{terminal_id}/restart", status_code=204)
async def restart_terminal_channel(
    workspace_id: str,
    terminal_id: str,
    request: Request,
) -> None:
    """Restart one attached terminal with its original options."""
    user_id, _tenant = _tenant_identity(request)
    try:
        await _journal(request).restart(
            user_id=user_id,
            workspace_id=workspace_id,
            terminal_id=terminal_id,
        )
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
    user_id, _tenant = _tenant_identity(request)
    try:
        batch = await _journal(request).events(
            user_id=user_id,
            workspace_id=workspace_id,
            terminal_id=terminal_id,
            next_sequence=next_sequence,
        )
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
    user_id, _tenant = _tenant_identity(request)
    await _journal(request).close(
        user_id=user_id,
        workspace_id=workspace_id,
        terminal_id=terminal_id,
    )
