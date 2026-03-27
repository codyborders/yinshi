"""Endpoints for agent sessions."""

import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from yinshi.api.deps import (
    check_session_owner,
    check_workspace_owner,
    get_db_for_request,
)
from yinshi.model_catalog import normalize_model_ref
from yinshi.models import MessageOut, SessionCreate, SessionOut, SessionUpdate

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sessions"])

_UPDATABLE_COLUMNS = {"model"}
_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        "vendor",
        ".next",
        "dist",
        "build",
    }
)
_TREE_FILE_LIMIT = 5000


def _normalize_session_row(db, row: Any) -> dict[str, Any]:
    """Normalize stored session models and persist repairs on read."""
    normalized_row = dict(row)
    original_model = normalized_row["model"]
    normalized_model = normalize_model_ref(original_model)
    if normalized_model != original_model:
        db.execute("UPDATE sessions SET model = ? WHERE id = ?", (normalized_model, normalized_row["id"]))
        db.commit()
        normalized_row["model"] = normalized_model
    return normalized_row


def _list_workspace_files(workspace_path: str) -> list[str]:
    """List workspace files while excluding bulky build directories."""
    if not os.path.isdir(workspace_path):
        return []

    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(workspace_path):
        dirnames[:] = sorted(
            dirname
            for dirname in dirnames
            if dirname not in _EXCLUDED_DIRS
        )
        for filename in sorted(filenames):
            relative_path = os.path.relpath(
                os.path.join(dirpath, filename),
                workspace_path,
            )
            files.append(relative_path)
            if len(files) >= _TREE_FILE_LIMIT:
                files.sort()
                return files

    files.sort()
    return files


@router.get("/api/workspaces/{workspace_id}/sessions", response_model=list[SessionOut])
def list_sessions(workspace_id: str, request: Request) -> list[dict[str, Any]]:
    """List all sessions for a workspace."""
    with get_db_for_request(request) as db:
        check_workspace_owner(db, workspace_id, request)
        rows = db.execute(
            "SELECT * FROM sessions WHERE workspace_id = ? ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
        return [_normalize_session_row(db, row) for row in rows]


@router.post(
    "/api/workspaces/{workspace_id}/sessions",
    response_model=SessionOut,
    status_code=201,
)
def create_session(
    workspace_id: str,
    body: SessionCreate,
    request: Request,
) -> dict[str, Any]:
    """Create a new agent session for a workspace."""
    with get_db_for_request(request) as db:
        ws = db.execute(
            "SELECT id FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        if not ws:
            raise HTTPException(status_code=404, detail="Workspace not found")
        check_workspace_owner(db, workspace_id, request)

        cursor = db.execute(
            """INSERT INTO sessions (workspace_id, status, model)
               VALUES (?, 'idle', ?)""",
            (workspace_id, body.model),
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM sessions WHERE rowid = ?", (cursor.lastrowid,)
        ).fetchone()
        assert row is not None, "created session must be queryable"
        return _normalize_session_row(db, row)


@router.get("/api/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: str, request: Request) -> dict[str, Any]:
    """Get a session by ID."""
    with get_db_for_request(request) as db:
        row = db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        check_session_owner(db, session_id, request)
        return _normalize_session_row(db, row)


@router.patch("/api/sessions/{session_id}", response_model=SessionOut)
def update_session(
    session_id: str,
    body: SessionUpdate,
    request: Request,
) -> dict[str, Any]:
    """Update session fields (currently only model)."""
    with get_db_for_request(request) as db:
        row = db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        check_session_owner(db, session_id, request)

        updates = {
            k: v
            for k, v in body.model_dump(exclude_unset=True).items()
            if k in _UPDATABLE_COLUMNS
        }
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [session_id]
            db.execute(f"UPDATE sessions SET {sets} WHERE id = ?", vals)  # noqa: S608
            db.commit()
        updated = db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert updated is not None, "updated session must be queryable"
        return _normalize_session_row(db, updated)


@router.get("/api/sessions/{session_id}/messages", response_model=list[MessageOut])
def get_messages(session_id: str, request: Request) -> list[dict[str, Any]]:
    """Get all messages for a session."""
    with get_db_for_request(request) as db:
        sess = db.execute(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not sess:
            raise HTTPException(status_code=404, detail="Session not found")
        check_session_owner(db, session_id, request)

        rows = db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]


@router.get("/api/sessions/{session_id}/tree")
def get_session_tree(session_id: str, request: Request) -> dict[str, list[str]]:
    """Return the workspace file tree for a session."""
    with get_db_for_request(request) as db:
        row = db.execute(
            "SELECT s.id, w.path as workspace_path "
            "FROM sessions s "
            "JOIN workspaces w ON s.workspace_id = w.id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        check_session_owner(db, session_id, request)

    workspace_path = row["workspace_path"]
    assert isinstance(workspace_path, str)
    return {"files": _list_workspace_files(workspace_path)}
