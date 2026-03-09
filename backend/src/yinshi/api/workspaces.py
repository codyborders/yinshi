"""Endpoints for workspace (worktree) management."""

import logging

from fastapi import APIRouter, HTTPException, Request

from yinshi.api.deps import check_owner, get_user_email
from yinshi.db import get_db
from yinshi.exceptions import RepoNotFoundError, WorkspaceNotFoundError
from yinshi.models import WorkspaceCreate, WorkspaceOut
from yinshi.services.workspace import create_workspace_for_repo, delete_workspace

logger = logging.getLogger(__name__)
router = APIRouter(tags=["workspaces"])

@router.get("/api/repos/{repo_id}/workspaces", response_model=list[WorkspaceOut])
def list_workspaces(repo_id: str, request: Request) -> list[dict]:
    """List all workspaces for a repo."""
    email = get_user_email(request)
    with get_db() as db:
        repo = db.execute(
            "SELECT owner_email FROM repos WHERE id = ?", (repo_id,)
        ).fetchone()
        if repo:
            check_owner(repo["owner_email"], email)
        rows = db.execute(
            "SELECT * FROM workspaces WHERE repo_id = ? ORDER BY created_at DESC",
            (repo_id,),
        ).fetchall()
        return [dict(r) for r in rows]

@router.post(
    "/api/repos/{repo_id}/workspaces",
    response_model=WorkspaceOut,
    status_code=201,
)
async def create_workspace(repo_id: str, body: WorkspaceCreate, request: Request) -> dict:
    """Create a new worktree workspace."""
    email = get_user_email(request)
    with get_db() as db:
        repo = db.execute(
            "SELECT owner_email FROM repos WHERE id = ?", (repo_id,)
        ).fetchone()
        if repo:
            check_owner(repo["owner_email"], email)
        username = email.split("@")[0] if email else None
        try:
            workspace = await create_workspace_for_repo(db, repo_id, body.name, username=username)
            return workspace
        except RepoNotFoundError:
            raise HTTPException(status_code=404, detail="Repo not found")

@router.delete("/api/workspaces/{workspace_id}", status_code=204)
async def remove_workspace(workspace_id: str, request: Request) -> None:
    """Delete a workspace and its worktree."""
    email = get_user_email(request)
    with get_db() as db:
        ws = db.execute(
            "SELECT w.id, r.owner_email FROM workspaces w "
            "JOIN repos r ON w.repo_id = r.id WHERE w.id = ?",
            (workspace_id,),
        ).fetchone()
        if ws:
            check_owner(ws["owner_email"], email)
        try:
            await delete_workspace(db, workspace_id)
        except (WorkspaceNotFoundError, RepoNotFoundError):
            raise HTTPException(status_code=404, detail="Workspace not found")
        except Exception:
            logger.exception("Failed to delete workspace %s", workspace_id)
            raise HTTPException(status_code=500, detail="Failed to delete workspace")
