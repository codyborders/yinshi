"""CRUD endpoints for repositories."""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from yinshi.config import get_settings
from yinshi.db import get_db
from yinshi.models import RepoCreate, RepoOut, RepoUpdate
from yinshi.services.git import clone_repo, validate_local_repo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/repos", tags=["repos"])

# Only these columns can be updated via PATCH
_UPDATABLE_COLUMNS = {"name", "custom_prompt"}


def _validate_local_path(path_str: str) -> str:
    """Validate and resolve a local path, checking against allowed base."""
    resolved = str(Path(path_str).resolve())
    settings = get_settings()
    if settings.allowed_repo_base:
        allowed = str(Path(settings.allowed_repo_base).resolve())
        if not resolved.startswith(allowed + "/") and resolved != allowed:
            raise HTTPException(status_code=400, detail="Path not in allowed directory")
    return resolved


@router.get("", response_model=list[RepoOut])
def list_repos() -> list[dict]:
    """List all imported repositories."""
    with get_db() as db:
        rows = db.execute("SELECT * FROM repos ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


@router.post("", response_model=RepoOut, status_code=201)
async def import_repo(body: RepoCreate) -> dict:
    """Import a repository (clone from URL or register local path)."""
    if body.local_path:
        resolved = _validate_local_path(body.local_path)
        if not Path(resolved).is_dir():
            raise HTTPException(status_code=400, detail="Path does not exist")
        is_repo = await validate_local_repo(resolved)
        if not is_repo:
            raise HTTPException(status_code=400, detail="Not a valid git repository")
        root_path = resolved
    elif body.remote_url:
        clone_dir = str(Path.home() / ".yinshi" / "repos" / body.name)
        root_path = await clone_repo(body.remote_url, clone_dir)
    else:
        raise HTTPException(
            status_code=400, detail="Either remote_url or local_path is required"
        )

    with get_db() as db:
        cursor = db.execute(
            """INSERT INTO repos (name, remote_url, root_path, custom_prompt)
               VALUES (?, ?, ?, ?)""",
            (body.name, body.remote_url, root_path, body.custom_prompt),
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM repos WHERE rowid = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)


@router.get("/{repo_id}", response_model=RepoOut)
def get_repo(repo_id: str) -> dict:
    """Get a single repository by ID."""
    with get_db() as db:
        row = db.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Repo not found")
        return dict(row)


@router.patch("/{repo_id}", response_model=RepoOut)
def update_repo(repo_id: str, body: RepoUpdate) -> dict:
    """Update a repository."""
    with get_db() as db:
        row = db.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Repo not found")

        updates = {
            k: v
            for k, v in body.model_dump(exclude_unset=True).items()
            if k in _UPDATABLE_COLUMNS
        }
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [repo_id]
            db.execute(f"UPDATE repos SET {set_clause} WHERE id = ?", values)
            db.commit()

        row = db.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        return dict(row)


@router.delete("/{repo_id}", status_code=204)
def delete_repo(repo_id: str) -> None:
    """Delete a repository and all its workspaces."""
    with get_db() as db:
        row = db.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Repo not found")
        db.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
        db.commit()
