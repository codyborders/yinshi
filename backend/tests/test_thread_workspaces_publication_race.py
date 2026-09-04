"""Concurrent hidden-ref publication races for thread provisioning and finalization.

Another writer publishes the target hidden ref after the service's existence
check but before its own publication. The service must fail closed and leave
the competing ref exactly as the other writer wrote it.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from yinshi.services import thread_workspaces

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
RESULT_REF = f"refs/yinshi/results/{DELEGATION_ID}"


def run_git(*args: str, cwd: str) -> str:
    """Run one setup git command."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_finalize_preserves_result_ref_published_after_absence_check(
    db,
    git_repo,
    monkeypatch,
):
    """A competing result-ref writer wins the race and keeps its ref."""
    from yinshi.services.thread_workspaces import ThreadResultRefConflictError

    run_git("config", "user.name", "T", cwd=git_repo)
    run_git("config", "user.email", "t@t", cwd=git_repo)
    parent_path = str(Path(git_repo) / ".worktrees" / "parent-branch")
    run_git("worktree", "add", "-b", "parent-branch", parent_path, cwd=git_repo)
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'repo', ?)",
        (git_repo,),
    )
    db.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path, state)
           VALUES ('parent-ws', 'repo1', 'parent', 'parent-branch', ?, 'ready')""",
        (parent_path,),
    )
    db.execute(
        "INSERT INTO sessions (id, workspace_id) VALUES ('parent-session', 'parent-ws')",
    )
    db.commit()
    base_commit = run_git("rev-parse", "HEAD", cwd=parent_path)
    provisioned = asyncio.run(
        thread_workspaces.ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )
    (Path(provisioned.path) / "done.txt").write_text("done\n", encoding="utf-8")
    real_run_git = thread_workspaces._run_git

    async def competing_result_ref_writer(args, **kwargs):
        if args[:1] == ["update-ref"] and RESULT_REF in args:
            # Another writer publishes a competing result just before us.
            subprocess.run(
                ["git", "update-ref", RESULT_REF, base_commit],
                cwd=git_repo,
                capture_output=True,
                text=True,
                check=True,
            )
        return await real_run_git(args, **kwargs)

    monkeypatch.setattr(thread_workspaces, "_run_git", competing_result_ref_writer)

    with pytest.raises(ThreadResultRefConflictError, match="published concurrently"):
        asyncio.run(
            thread_workspaces.ThreadWorkspaceService().finalize_child(
                db,
                None,
                delegation_id=DELEGATION_ID,
                workspace_id=provisioned.workspace_id,
                base_commit=base_commit,
            )
        )

    monkeypatch.undo()
    # The competing ref must survive untouched, not be overwritten.
    assert run_git("rev-parse", "--verify", RESULT_REF, cwd=git_repo) == base_commit
