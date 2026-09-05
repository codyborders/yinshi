"""Endpoints for workspace (worktree) management."""

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from yinshi.api.deps import (
    check_owner,
    check_workspace_owner,
    get_db_for_request,
    get_tenant,
    get_user_email,
)
from yinshi.config import get_settings
from yinshi.exceptions import (
    GitError,
    RepoNotFoundError,
    WorkspaceHasDelegatedThreads,
    WorkspaceNotFoundError,
)
from yinshi.models import WorkspaceCreate, WorkspaceOut, WorkspaceUpdate
from yinshi.services.run_coordinator import get_run_coordinator
from yinshi.services.sidecar import release_sessions
from yinshi.services.workspace import (
    create_workspace_for_repo,
    delete_workspace,
    ensure_workspace_has_no_delegated_children,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["workspaces"])

_UPDATABLE_COLUMNS = {"state"}
_RUNTIME_BUSY_DETAIL = "Workspace is still stopping; deletion can be retried"
_WORKSPACE_PROJECTION = """
    SELECT w.id, w.created_at, w.updated_at, w.repo_id, w.name, w.branch,
           w.path, w.state,
           CASE WHEN w.kind = 'delegated' THEN 'delegated' ELSE 'primary' END AS kind,
           w.parent_workspace_id,
           d.id AS delegation_id,
           d.status AS delegation_status
      FROM workspaces w
      LEFT JOIN thread_delegations d ON d.child_workspace_id = w.id
"""


def _workspace_projection(
    db: sqlite3.Connection,
    workspace_id: str,
) -> dict[str, Any]:
    """Load one workspace with delegated-thread metadata."""
    row = db.execute(
        _WORKSPACE_PROJECTION + " WHERE w.id = ?",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return dict(row)


def _thread_children_present_error() -> HTTPException:
    """Return the public conflict for a workspace with delegated children."""
    return HTTPException(
        status_code=409,
        detail={
            "code": "thread_children_present",
            "message": "Workspace sessions parent delegated child threads",
        },
    )


def _reject_delegated_parent_workspace(
    db: sqlite3.Connection,
    workspace_id: str,
) -> None:
    """Refuse deletion before any teardown when sessions parent children."""
    try:
        ensure_workspace_has_no_delegated_children(db, workspace_id)
    except WorkspaceHasDelegatedThreads as exc:
        raise _thread_children_present_error() from exc


def _check_repo_owner(
    db: sqlite3.Connection,
    repo_id: str,
    request: Request,
) -> None:
    """In legacy mode, verify the authenticated user owns the repo."""
    if get_tenant(request):
        return
    repo = db.execute("SELECT owner_email FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if repo:
        check_owner(repo["owner_email"], get_user_email(request))
    else:
        raise HTTPException(status_code=404, detail="Repo not found")


@router.get("/api/repos/{repo_id}/workspaces", response_model=list[WorkspaceOut])
def list_workspaces(repo_id: str, request: Request) -> list[dict[str, Any]]:
    """List all workspaces for a repo."""
    with get_db_for_request(request) as db:
        _check_repo_owner(db, repo_id, request)
        rows = db.execute(
            _WORKSPACE_PROJECTION + " WHERE w.repo_id = ? ORDER BY w.created_at DESC",
            (repo_id,),
        ).fetchall()
        return [dict(r) for r in rows]


@router.post(
    "/api/repos/{repo_id}/workspaces",
    response_model=WorkspaceOut,
    status_code=201,
)
async def create_workspace(
    repo_id: str,
    body: WorkspaceCreate,
    request: Request,
) -> dict[str, Any]:
    """Create a new worktree workspace."""
    email = get_user_email(request)
    username = email.split("@")[0] if email else None
    tenant = get_tenant(request)

    with get_db_for_request(request) as db:
        _check_repo_owner(db, repo_id, request)
        try:
            created = await create_workspace_for_repo(
                db,
                repo_id,
                body.name,
                username=username,
                tenant=tenant,
            )
            return _workspace_projection(db, str(created["id"]))
        except RepoNotFoundError:
            raise HTTPException(status_code=404, detail="Repo not found")
        except GitError as exc:
            raise HTTPException(status_code=409, detail=str(exc))


@router.patch("/api/workspaces/{workspace_id}", response_model=WorkspaceOut)
def update_workspace(
    workspace_id: str,
    body: WorkspaceUpdate,
    request: Request,
) -> dict[str, Any]:
    """Update workspace fields (currently only state)."""
    with get_db_for_request(request) as db:
        row = db.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workspace not found")
        check_workspace_owner(db, workspace_id, request)

        updates = {
            k: v for k, v in body.model_dump(exclude_unset=True).items() if k in _UPDATABLE_COLUMNS
        }
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [workspace_id]
            db.execute(f"UPDATE workspaces SET {sets} WHERE id = ?", vals)  # noqa: S608
            db.commit()
        return _workspace_projection(db, workspace_id)


@router.delete("/api/workspaces/{workspace_id}", status_code=204)
async def remove_workspace(workspace_id: str, request: Request) -> None:
    """Stop runtime activity, then delete a workspace and its durable paths."""
    tenant = get_tenant(request)
    with get_db_for_request(request) as db:
        check_workspace_owner(db, workspace_id, request)
        _reject_delegated_parent_workspace(db, workspace_id)
        session_rows = db.execute(
            "SELECT id FROM sessions WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchall()
        try:
            coordinator = get_run_coordinator()
            for session_row in session_rows:
                await coordinator.request_cancel(str(session_row["id"]))
            if request.app.state.mode == "desktop":
                # Desktop shares one long-lived sidecar, so these pi sessions
                # would otherwise stay resident until the app quits. Hosted
                # mode destroys the whole container below instead.
                await release_sessions(
                    get_settings().sidecar_socket_path,
                    [str(session_row["id"]) for session_row in session_rows],
                )
            elif tenant is not None:
                container_manager = getattr(request.app.state, "container_manager", None)
                if container_manager is not None:
                    container_removed = await container_manager.destroy_container(
                        tenant.user_id,
                        runtime_id=workspace_id,
                    )
                    if not container_removed:
                        raise HTTPException(status_code=409, detail=_RUNTIME_BUSY_DETAIL)
                elif get_settings().container_enabled:
                    raise RuntimeError("container manager is unavailable")
            await delete_workspace(db, workspace_id, tenant=tenant)
        except (WorkspaceNotFoundError, RepoNotFoundError):
            raise HTTPException(status_code=404, detail="Workspace not found")
        except WorkspaceHasDelegatedThreads as exc:
            raise _thread_children_present_error() from exc
        except HTTPException:
            raise
        except Exception:
            logger.error("Failed to delete workspace")
            raise HTTPException(status_code=500, detail="Failed to delete workspace")
