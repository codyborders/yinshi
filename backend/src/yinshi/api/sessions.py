"""Endpoints for agent sessions."""

import logging

from fastapi import APIRouter, HTTPException, Request

from yinshi.api.deps import check_owner, get_user_email
from yinshi.db import get_db
from yinshi.models import MessageOut, SessionCreate, SessionOut

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
