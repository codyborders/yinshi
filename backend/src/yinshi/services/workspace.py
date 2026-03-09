"""Workspace lifecycle management."""

import logging
import os
import sqlite3

from yinshi.exceptions import RepoNotFoundError, WorkspaceNotFoundError
from yinshi.services.git import create_worktree, delete_worktree, generate_branch_name

logger = logging.getLogger(__name__)


async def create_workspace_for_repo(
    db: sqlite3.Connection,
    repo_id: str,
    name: str | None = None,
    username: str | None = None,
) -> dict:
    """Create a new worktree workspace for a repo."""
    repo = db.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if not repo:
        raise RepoNotFoundError(f"Repo {repo_id} not found")

    branch = generate_branch_name(username=username)
    if not name:
        name = branch

    repo_path = repo["root_path"]
    worktree_dir = os.path.join(repo_path, ".worktrees", branch)

    await create_worktree(repo_path, worktree_dir, branch)

    cursor = db.execute(
        """INSERT INTO workspaces (repo_id, name, branch, path, state)
           VALUES (?, ?, ?, ?, 'ready')""",
        (repo_id, name, branch, worktree_dir),
    )
    db.commit()

    row = db.execute(
        "SELECT * FROM workspaces WHERE rowid = ?", (cursor.lastrowid,)
    ).fetchone()
    return dict(row)


async def delete_workspace(db: sqlite3.Connection, workspace_id: str) -> None:
    """Delete a workspace and its worktree from disk."""
    workspace = db.execute(
        "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
    ).fetchone()
    if not workspace:
        raise WorkspaceNotFoundError(f"Workspace {workspace_id} not found")

    repo = db.execute(
        "SELECT * FROM repos WHERE id = ?", (workspace["repo_id"],)
    ).fetchone()
    if not repo:
        raise RepoNotFoundError(f"Repo {workspace['repo_id']} not found")

    try:
        await delete_worktree(repo["root_path"], workspace["path"])
    except Exception as e:
        logger.warning("Failed to delete worktree on disk: %s", e)

    db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
    db.commit()
