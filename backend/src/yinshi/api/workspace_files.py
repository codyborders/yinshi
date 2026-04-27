"""Workspace file tree, status, preview, diff, edit, and download endpoints."""

from __future__ import annotations

import sqlite3
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from yinshi.api.deps import check_workspace_owner, get_db_for_request
from yinshi.exceptions import GitError
from yinshi.services.workspace_files import (
    build_file_tree,
    changed_files,
    changed_files_to_dicts,
    diff_file,
    ensure_secret_guardrails,
    file_tree_to_dicts,
    read_text_file,
    validate_visible_relative_path,
    write_text_file,
)

router = APIRouter()


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


def _prepare_workspace_files(
    db: sqlite3.Connection,
    workspace_id: str,
    request: Request,
) -> tuple[str, str]:
    """Return workspace and repo paths, installing Git secret guardrails."""
    row = _workspace_row(db, workspace_id, request)
    workspace_path = str(row["path"])
    repo_root_path = str(row["root_path"])
    try:
        ensure_secret_guardrails(repo_root_path)
    except OSError as exc:
        raise HTTPException(status_code=409, detail="Failed to prepare secret guardrails") from exc
    return workspace_path, repo_root_path


def _map_file_error(exc: Exception) -> HTTPException:
    """Convert file service exceptions into stable HTTP responses."""
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc) or "File not found")
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc) or "File is not available")
    if isinstance(exc, (TypeError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc) or "Invalid file request")
    if isinstance(exc, GitError):
        return HTTPException(status_code=409, detail=str(exc) or "Git command failed")
    return HTTPException(status_code=500, detail="Workspace file operation failed")


@router.get("/api/workspaces/{workspace_id}/files/tree")
def get_workspace_file_tree(workspace_id: str, request: Request) -> dict[str, Any]:
    """Return a bounded visible nested file tree for one workspace."""
    with get_db_for_request(request) as db:
        workspace_path, _ = _prepare_workspace_files(db, workspace_id, request)
    try:
        nodes = build_file_tree(workspace_path)
    except Exception as exc:
        raise _map_file_error(exc) from exc
    return {"files": file_tree_to_dicts(nodes)}


@router.get("/api/workspaces/{workspace_id}/files/changed")
async def get_workspace_changed_files(workspace_id: str, request: Request) -> dict[str, Any]:
    """Return visible Git status changes for one workspace."""
    with get_db_for_request(request) as db:
        workspace_path, _ = _prepare_workspace_files(db, workspace_id, request)
    try:
        changes = await changed_files(workspace_path)
    except Exception as exc:
        raise _map_file_error(exc) from exc
    return {"files": changed_files_to_dicts(changes)}


@router.get("/api/workspaces/{workspace_id}/files/preview")
def preview_workspace_file(
    workspace_id: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=4096),
) -> dict[str, str]:
    """Return text content for one visible workspace file."""
    with get_db_for_request(request) as db:
        workspace_path, _ = _prepare_workspace_files(db, workspace_id, request)
    try:
        return {"path": path, "content": read_text_file(workspace_path, path)}
    except Exception as exc:
        raise _map_file_error(exc) from exc


@router.get("/api/workspaces/{workspace_id}/files/diff")
async def diff_workspace_file(
    workspace_id: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=4096),
) -> dict[str, str]:
    """Return a Git diff for one visible workspace file."""
    with get_db_for_request(request) as db:
        workspace_path, _ = _prepare_workspace_files(db, workspace_id, request)
    try:
        return {"path": path, "diff": await diff_file(workspace_path, path)}
    except Exception as exc:
        raise _map_file_error(exc) from exc


@router.put("/api/workspaces/{workspace_id}/files/content")
def edit_workspace_file(
    workspace_id: str,
    body: FileEditRequest,
    request: Request,
    path: str = Query(..., min_length=1, max_length=4096),
) -> dict[str, str]:
    """Replace one visible workspace text file from the browser editor."""
    with get_db_for_request(request) as db:
        workspace_path, _ = _prepare_workspace_files(db, workspace_id, request)
    try:
        write_text_file(workspace_path, path, body.content)
    except Exception as exc:
        raise _map_file_error(exc) from exc
    return {"path": path, "status": "saved"}


@router.get("/api/workspaces/{workspace_id}/files/download")
def download_workspace_file(
    workspace_id: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=4096),
) -> FileResponse:
    """Download one visible workspace file."""
    with get_db_for_request(request) as db:
        workspace_path, _ = _prepare_workspace_files(db, workspace_id, request)
    try:
        file_path = validate_visible_relative_path(workspace_path, path)
        if not file_path.is_file():
            raise FileNotFoundError("file does not exist")
    except Exception as exc:
        raise _map_file_error(exc) from exc
    return FileResponse(file_path, filename=file_path.name)
