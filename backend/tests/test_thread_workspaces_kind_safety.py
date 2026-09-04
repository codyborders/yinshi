"""Workspace-kind safety for child-only Git operations."""

from __future__ import annotations

import asyncio

import pytest

from yinshi.exceptions import YinshiError
from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"


def seed_user_workspace(db, git_repo: str) -> tuple[str, str]:
    """Insert one ordinary workspace and return its ID plus HEAD."""
    import subprocess

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'repo', ?)",
        (git_repo,),
    )
    db.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path, state, kind)
           VALUES ('user-ws', 'repo1', 'user', 'main', ?, 'ready', 'user')""",
        (git_repo,),
    )
    db.commit()
    return "user-ws", head


def test_child_git_operations_reject_user_workspaces(db, git_repo):
    """Child cleanup and finalization cannot mutate an ordinary workspace."""
    workspace_id, base_commit = seed_user_workspace(db, git_repo)
    service = ThreadWorkspaceService()

    with pytest.raises(YinshiError, match="delegated workspace"):
        asyncio.run(
            service.finalize_child(
                db,
                None,
                delegation_id=DELEGATION_ID,
                workspace_id=workspace_id,
                base_commit=base_commit,
            )
        )
    with pytest.raises(YinshiError, match="delegated workspace"):
        asyncio.run(
            service.discard_partial_child(
                db,
                None,
                delegation_id=DELEGATION_ID,
                workspace_id=workspace_id,
            )
        )
