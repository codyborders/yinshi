"""Snapshot-ref publication race for thread child provisioning.

Another writer publishes refs/yinshi/snapshots/<delegation> after the
availability check but before this attempt's own publication. Provisioning
must fail and leave the competing ref untouched, including during cleanup.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from yinshi.exceptions import GitError
from yinshi.services import thread_workspaces

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
SNAPSHOT_REF = f"refs/yinshi/snapshots/{DELEGATION_ID}"


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


def test_provision_preserves_snapshot_ref_published_after_availability_check(
    db,
    git_repo,
    monkeypatch,
):
    """A competing snapshot-ref writer wins the race and keeps its ref."""
    from yinshi.services.thread_workspaces import ThreadSnapshotRefExistsError

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
    # Dirty the parent so provisioning takes the snapshot path.
    (Path(parent_path) / "README.md").write_text("dirty\n", encoding="utf-8")
    competing_commit = run_git("rev-parse", "HEAD", cwd=parent_path)
    real_run_git = thread_workspaces._run_git

    async def competing_snapshot_ref_writer(args, **kwargs):
        if args[:1] == ["update-ref"] and SNAPSHOT_REF in args:
            # Another writer publishes the snapshot ref before this attempt.
            subprocess.run(
                ["git", "update-ref", SNAPSHOT_REF, competing_commit],
                cwd=git_repo,
                capture_output=True,
                text=True,
                check=True,
            )
        return await real_run_git(args, **kwargs)

    monkeypatch.setattr(thread_workspaces, "_run_git", competing_snapshot_ref_writer)

    with pytest.raises(ThreadSnapshotRefExistsError, match="already exists"):
        asyncio.run(
            thread_workspaces.ThreadWorkspaceService().provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id=DELEGATION_ID,
            )
        )

    monkeypatch.undo()
    # The competing ref must survive: this attempt never published it, so
    # cleanup must not delete it either.
    assert run_git("rev-parse", "--verify", SNAPSHOT_REF, cwd=git_repo) == competing_commit
    assert (
        db.execute(
            "SELECT count(*) FROM workspaces WHERE kind = 'delegated'",
        ).fetchone()[0]
        == 0
    )


def test_provision_failure_after_publication_deletes_owned_snapshot_ref(
    db,
    git_repo,
    monkeypatch,
):
    """Cleanup deletes a snapshot ref only this attempt really published."""
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
    # Dirty the parent so provisioning publishes a snapshot ref first.
    (Path(parent_path) / "README.md").write_text("dirty\n", encoding="utf-8")

    async def failing_create_worktree(*args, **kwargs):
        raise GitError("injected worktree creation failure")

    monkeypatch.setattr(thread_workspaces, "create_worktree", failing_create_worktree)

    with pytest.raises(GitError, match="injected worktree creation failure"):
        asyncio.run(
            thread_workspaces.ThreadWorkspaceService().provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id=DELEGATION_ID,
            )
        )

    monkeypatch.undo()
    # This attempt definitely published the ref, so cleanup owns it.
    assert (
        run_git(
            "for-each-ref",
            "--format=%(refname)",
            "refs/yinshi/snapshots",
            cwd=git_repo,
        )
        == ""
    )
    assert (
        db.execute(
            "SELECT count(*) FROM workspaces WHERE kind = 'delegated'",
        ).fetchone()[0]
        == 0
    )
