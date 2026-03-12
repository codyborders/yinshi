"""Endpoints for agent sessions."""

import logging
import os

from fastapi import APIRouter, HTTPException, Request

from yinshi.api.deps import check_owner, get_user_email
from yinshi.db import get_db
from yinshi.models import MessageOut, SessionCreate, SessionOut, SessionUpdate

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sessions"])

@router.get("/api/workspaces/{workspace_id}/sessions", response_model=list[SessionOut])
def list_sessions(workspace_id: str, request: Request) -> list[dict]:
    """List all sessions for a workspace."""
    email = get_user_email(request)
    with get_db() as db:
        ws = db.execute(
            "SELECT w.id, r.owner_email FROM workspaces w "
            "JOIN repos r ON w.repo_id = r.id WHERE w.id = ?",
            (workspace_id,),
        ).fetchone()
        if ws:
            check_owner(ws["owner_email"], email)
        rows = db.execute(
            "SELECT * FROM sessions WHERE workspace_id = ? ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
        return [dict(r) for r in rows]

@router.post(
    "/api/workspaces/{workspace_id}/sessions",
    response_model=SessionOut,
    status_code=201,
)
def create_session(workspace_id: str, body: SessionCreate, request: Request) -> dict:
    """Create a new agent session for a workspace."""
    email = get_user_email(request)
    with get_db() as db:
        ws = db.execute(
            "SELECT w.id, r.owner_email FROM workspaces w "
            "JOIN repos r ON w.repo_id = r.id WHERE w.id = ?",
            (workspace_id,),
        ).fetchone()
        if not ws:
            raise HTTPException(status_code=404, detail="Workspace not found")
        check_owner(ws["owner_email"], email)

        cursor = db.execute(
            """INSERT INTO sessions (workspace_id, status, model)
               VALUES (?, 'idle', ?)""",
            (workspace_id, body.model),
        )
        db.commit()

        row = db.execute(
            "SELECT * FROM sessions WHERE rowid = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)

@router.get("/api/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: str, request: Request) -> dict:
    """Get a session by ID."""
    email = get_user_email(request)
    with get_db() as db:
        row = db.execute(
            "SELECT s.*, r.owner_email FROM sessions s "
            "JOIN workspaces w ON s.workspace_id = w.id "
            "JOIN repos r ON w.repo_id = r.id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        check_owner(row["owner_email"], email)
        return dict(row)

@router.patch("/api/sessions/{session_id}", response_model=SessionOut)
def update_session(session_id: str, body: SessionUpdate, request: Request) -> dict:
    """Update session fields (currently only model)."""
    email = get_user_email(request)
    with get_db() as db:
        row = db.execute(
            "SELECT s.*, r.owner_email FROM sessions s "
            "JOIN workspaces w ON s.workspace_id = w.id "
            "JOIN repos r ON w.repo_id = r.id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        check_owner(row["owner_email"], email)

        updates = body.model_dump(exclude_none=True)
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [session_id]
            db.execute(f"UPDATE sessions SET {sets} WHERE id = ?", vals)  # noqa: S608
            db.commit()

        updated = db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(updated)


@router.get("/api/sessions/{session_id}/messages", response_model=list[MessageOut])
def get_messages(session_id: str, request: Request) -> list[dict]:
    """Get all messages for a session."""
    email = get_user_email(request)
    with get_db() as db:
        sess = db.execute(
            "SELECT s.id, r.owner_email FROM sessions s "
            "JOIN workspaces w ON s.workspace_id = w.id "
            "JOIN repos r ON w.repo_id = r.id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if not sess:
            raise HTTPException(status_code=404, detail="Session not found")
        check_owner(sess["owner_email"], email)

        rows = db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]


@router.get("/api/sessions/{session_id}/tree")
def get_session_tree(session_id: str, request: Request) -> dict:
    """Return the workspace file tree for a session."""
    email = get_user_email(request)
    with get_db() as db:
        row = db.execute(
            "SELECT s.id, w.path as workspace_path, r.owner_email "
            "FROM sessions s "
            "JOIN workspaces w ON s.workspace_id = w.id "
            "JOIN repos r ON w.repo_id = r.id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        check_owner(row["owner_email"], email)

    workspace_path = row["workspace_path"]
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(workspace_path):
        # Skip .git directory
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fname in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fname), workspace_path)
            files.append(rel)
    files.sort()
    return {"files": files}
