"""Workspace file tree, status, preview, diff, edit, and download endpoints."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import suppress
from typing import Any, BinaryIO, TypeVar, cast
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from yinshi.api.deps import check_workspace_owner, get_tenant, run_db_operation_for_request
from yinshi.exceptions import GitError, WorkspaceNotFoundError
from yinshi.services.workspace_files import (
    _open_workspace_parent,
    build_file_tree,
    changed_files,
    changed_files_to_dicts,
    diff_file,
    ensure_secret_guardrails,
    file_tree_to_dicts,
    read_text_file,
    write_text_file,
)
from yinshi.services.workspace_runtime_paths import prepare_tenant_workspace_runtime_paths

logger = logging.getLogger(__name__)
router = APIRouter()

_T = TypeVar("_T")

_EXPECTED_FILE_ERRORS = (
    FileNotFoundError,
    PermissionError,
    TypeError,
    ValueError,
    GitError,
    WorkspaceNotFoundError,
)


class FileEditRequest(BaseModel):
    """Request body for browser-based workspace file edits."""

    content: str = Field(..., max_length=512 * 1024)


def _workspace_row(db: sqlite3.Connection, workspace_id: str, request: Request) -> sqlite3.Row:
    """Load one workspace and its repo paths after owner validation."""
    check_workspace_owner(db, workspace_id, request)
    row = db.execute(
        "SELECT w.id, w.path, r.root_path "
        "FROM workspaces w JOIN repos r ON w.repo_id = r.id WHERE w.id = ?",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return cast(sqlite3.Row, row)


async def _drain_local_attempt(attempt: asyncio.Task[_T]) -> None:
    """Wait through repeated cancellation until local work finishes."""
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


async def _run_local_operation(operation: Callable[[], _T]) -> _T:
    """Run blocking local work without abandoning it after cancellation."""
    if not callable(operation):
        raise TypeError("operation must be callable")
    attempt = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(attempt)
    except asyncio.CancelledError:
        await _drain_local_attempt(attempt)
        raise


async def _prepare_workspace_files(
    workspace_id: str,
    request: Request,
) -> str:
    """Return a trusted workspace path, installing Git secret guardrails."""
    tenant = get_tenant(request)
    if tenant is not None:
        try:
            paths = await prepare_tenant_workspace_runtime_paths(
                tenant,
                workspace_id,
                lambda operation: run_db_operation_for_request(request, operation),
            )
        except PermissionError:
            raise
        except OSError as exc:
            raise HTTPException(
                status_code=409,
                detail="Failed to prepare workspace paths",
            ) from exc
        return paths.workspace_path

    def load_paths(db: sqlite3.Connection) -> tuple[str, str]:
        row = _workspace_row(db, workspace_id, request)
        return str(row["path"]), str(row["root_path"])

    workspace_path, repo_root_path = await run_db_operation_for_request(request, load_paths)
    try:
        await _run_local_operation(lambda: ensure_secret_guardrails(repo_root_path))
    except OSError as exc:
        raise HTTPException(status_code=409, detail="Failed to prepare secret guardrails") from exc
    return workspace_path


def _map_file_error(exc: Exception) -> HTTPException:
    """Convert file service exceptions into stable HTTP responses."""
    if isinstance(exc, (FileNotFoundError, WorkspaceNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc) or "File not found")
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc) or "File is not available")
    if isinstance(exc, (TypeError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc) or "Invalid file request")
    if isinstance(exc, GitError):
        return HTTPException(status_code=409, detail=str(exc) or "Git command failed")
    return HTTPException(status_code=500, detail="Workspace file operation failed")


def _http_file_error(exc: Exception, workspace_id: str) -> HTTPException:
    """Return an HTTP error, logging unexpected workspace file failures."""
    if isinstance(exc, HTTPException):
        return exc
    if not isinstance(exc, _EXPECTED_FILE_ERRORS):
        logger.error("Unexpected workspace file operation failure")
    return _map_file_error(exc)


@router.get("/api/workspaces/{workspace_id}/files/tree")
async def get_workspace_file_tree(workspace_id: str, request: Request) -> dict[str, Any]:
    """Return a bounded visible nested file tree for one workspace."""
    try:
        workspace_path = await _prepare_workspace_files(workspace_id, request)
        nodes = await _run_local_operation(lambda: build_file_tree(workspace_path))
    except Exception as exc:
        raise _http_file_error(exc, workspace_id) from exc
    return {"files": file_tree_to_dicts(nodes)}


@router.get("/api/workspaces/{workspace_id}/files/changed")
async def get_workspace_changed_files(workspace_id: str, request: Request) -> dict[str, Any]:
    """Return visible Git status changes for one workspace."""
    try:
        workspace_path = await _prepare_workspace_files(workspace_id, request)
        changes = await changed_files(workspace_path)
    except Exception as exc:
        raise _http_file_error(exc, workspace_id) from exc
    return {"files": changed_files_to_dicts(changes)}


@router.get("/api/workspaces/{workspace_id}/files/preview")
async def preview_workspace_file(
    workspace_id: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=4096),
) -> dict[str, str]:
    """Return text content for one visible workspace file."""
    try:
        workspace_path = await _prepare_workspace_files(workspace_id, request)
        content = await _run_local_operation(lambda: read_text_file(workspace_path, path))
        return {"path": path, "content": content}
    except Exception as exc:
        raise _http_file_error(exc, workspace_id) from exc


@router.get("/api/workspaces/{workspace_id}/files/diff")
async def diff_workspace_file(
    workspace_id: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=4096),
) -> dict[str, str]:
    """Return a Git diff for one visible workspace file."""
    try:
        workspace_path = await _prepare_workspace_files(workspace_id, request)
        return {"path": path, "diff": await diff_file(workspace_path, path)}
    except Exception as exc:
        raise _http_file_error(exc, workspace_id) from exc


@router.put("/api/workspaces/{workspace_id}/files/content")
async def edit_workspace_file(
    workspace_id: str,
    body: FileEditRequest,
    request: Request,
    path: str = Query(..., min_length=1, max_length=4096),
) -> dict[str, str]:
    """Replace one visible workspace text file from the browser editor."""
    try:
        workspace_path = await _prepare_workspace_files(workspace_id, request)
        await _run_local_operation(lambda: write_text_file(workspace_path, path, body.content))
    except Exception as exc:
        raise _http_file_error(exc, workspace_id) from exc
    return {"path": path, "status": "saved"}


def _stream_open_file(file_handle: BinaryIO) -> Iterator[bytes]:
    """Yield fixed-size chunks and close the pre-opened file on completion."""
    if file_handle.closed:
        raise ValueError("file_handle must be open")
    try:
        while True:
            chunk = file_handle.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        file_handle.close()


def _open_stable_download(
    workspace_path: str,
    path: str,
) -> tuple[BinaryIO, str, os.stat_result]:
    """Open one regular workspace file through a stable descriptor."""
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_descriptor: int | None = None
    try:
        with _open_workspace_parent(workspace_path, path) as (parent_fd, file_name):
            try:
                file_descriptor = os.open(file_name, file_flags, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise PermissionError("path contains a symlink") from exc
                raise
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise FileNotFoundError("file does not exist")
        file_handle = os.fdopen(file_descriptor, "rb", closefd=True)
        file_descriptor = None
        return file_handle, file_name, file_stat
    except BaseException:
        if file_descriptor is not None:
            with suppress(OSError):
                os.close(file_descriptor)
        raise


async def _open_stable_download_async(
    workspace_path: str,
    path: str,
) -> tuple[BinaryIO, str, os.stat_result]:
    """Open a stable descriptor and close it when its caller is cancelled."""
    attempt = asyncio.create_task(asyncio.to_thread(_open_stable_download, workspace_path, path))
    try:
        return await asyncio.shield(attempt)
    except asyncio.CancelledError:
        await _drain_local_attempt(attempt)
        if not attempt.cancelled() and attempt.exception() is None:
            file_handle, _file_name, _file_stat = attempt.result()
            file_handle.close()
        raise


@router.get("/api/workspaces/{workspace_id}/files/download")
async def download_workspace_file(
    workspace_id: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=4096),
) -> StreamingResponse:
    """Download one visible workspace file through a stable descriptor."""
    file_handle: BinaryIO | None = None
    try:
        workspace_path = await _prepare_workspace_files(workspace_id, request)
        file_handle, file_name, file_stat = await _open_stable_download_async(
            workspace_path,
            path,
        )
    except Exception as exc:
        if file_handle is not None:
            file_handle.close()
        raise _http_file_error(exc, workspace_id) from exc

    encoded_name = quote(file_name, safe="")
    return StreamingResponse(
        _stream_open_file(file_handle),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "Content-Length": str(file_stat.st_size),
        },
    )
