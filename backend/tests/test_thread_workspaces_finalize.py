"""Synthetic result commit finalization for thread workspaces."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest

from yinshi.services.thread_workspaces import (
    ThreadResultRefConflictError,
    ThreadWorkspaceService,
)

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
RESULT_REF = f"refs/yinshi/results/{DELEGATION_ID}"


def run_git(*args: str, cwd: str, check: bool = True) -> str:
    """Run one setup git command."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def seed_parent(db: sqlite3.Connection, git_repo: str) -> str:
    """Insert one repo plus a parent workspace backed by a real worktree."""
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
    return parent_path


def make_child(db: sqlite3.Connection, git_repo: str) -> tuple[object, str]:
    """Provision one child workspace from a clean parent and return it."""
    parent_path = seed_parent(db, git_repo)
    base_commit = run_git("rev-parse", "HEAD", cwd=parent_path)
    provisioned = asyncio.run(
        ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )
    return provisioned, base_commit


def test_finalize_creates_synthetic_result_commit(db, git_repo):
    """Finalization seals the child filesystem into one result commit."""
    provisioned, base_commit = make_child(db, git_repo)
    child = Path(provisioned.path)
    (child / "work.txt").write_text("child work\n", encoding="utf-8")
    run_git("add", "work.txt", cwd=provisioned.path)
    run_git(
        "-c",
        "user.name=Child",
        "-c",
        "user.email=child@t",
        "commit",
        "-m",
        "child commit",
        cwd=provisioned.path,
    )
    # Uncommitted and untracked final state must also be captured.
    (child / "work.txt").write_text("child work v2\n", encoding="utf-8")
    (child / "loose.txt").write_text("loose\n", encoding="utf-8")

    finalized = asyncio.run(
        ThreadWorkspaceService().finalize_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=provisioned.workspace_id,
            base_commit=base_commit,
        )
    )

    assert finalized.base_commit == base_commit
    assert finalized.result_ref == RESULT_REF
    ref_commit = run_git("rev-parse", RESULT_REF, cwd=git_repo)
    assert ref_commit == finalized.result_commit
    result_parent = run_git("rev-parse", f"{finalized.result_commit}^", cwd=git_repo)
    assert result_parent == base_commit
    # The synthetic commit tree reflects the final filesystem, not child HEAD.
    loose_blob = run_git(
        "ls-tree",
        "-r",
        "--name-only",
        finalized.result_commit,
        cwd=git_repo,
    )
    assert "loose.txt" in loose_blob
    assert "work.txt" in loose_blob


def test_finalize_changed_files_cover_all_kinds(db, git_repo):
    """Changed files report modified, deleted, renamed, and added entries."""
    parent_path = seed_parent(db, git_repo)
    root = Path(parent_path)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    (root / "c.txt").write_text("c\n", encoding="utf-8")
    run_git("add", ".", cwd=parent_path)
    run_git(
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@t",
        "commit",
        "-m",
        "files",
        cwd=parent_path,
    )
    base_commit = run_git("rev-parse", "HEAD", cwd=parent_path)

    provisioned = asyncio.run(
        ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )
    child = Path(provisioned.path)
    (child / "a.txt").write_text("a2\n", encoding="utf-8")
    (child / "b.txt").unlink()
    run_git("mv", "c.txt", "d.txt", cwd=provisioned.path)
    (child / "new.txt").write_text("new\n", encoding="utf-8")

    finalized = asyncio.run(
        ThreadWorkspaceService().finalize_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=provisioned.workspace_id,
            base_commit=base_commit,
        )
    )

    by_path = {change.path: change for change in finalized.changed_files}
    assert by_path["a.txt"].kind == "modified"
    assert by_path["b.txt"].kind == "deleted"
    assert by_path["d.txt"].kind == "renamed"
    assert by_path["d.txt"].original_path == "c.txt"
    assert by_path["new.txt"].kind == "added"


def test_finalize_is_idempotent_for_unchanged_worktree(db, git_repo):
    """Re-finalizing an unchanged child returns the same result commit."""
    provisioned, base_commit = make_child(db, git_repo)
    (Path(provisioned.path) / "done.txt").write_text("done\n", encoding="utf-8")
    service = ThreadWorkspaceService()

    first = asyncio.run(
        service.finalize_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=provisioned.workspace_id,
            base_commit=base_commit,
        )
    )
    second = asyncio.run(
        service.finalize_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=provisioned.workspace_id,
            base_commit=base_commit,
        )
    )

    assert second.result_commit == first.result_commit
    assert second.result_ref == first.result_ref
    ref_commit = run_git("rev-parse", RESULT_REF, cwd=git_repo)
    assert ref_commit == first.result_commit


def test_finalize_rejects_existing_result_ref_with_different_tree(db, git_repo):
    """A retry with a new tree is rejected and the published ref survives."""
    provisioned, base_commit = make_child(db, git_repo)
    service = ThreadWorkspaceService()
    first = asyncio.run(
        service.finalize_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=provisioned.workspace_id,
            base_commit=base_commit,
        )
    )
    # New child work changes the computed tree away from the published one.
    (Path(provisioned.path) / "more.txt").write_text("more\n", encoding="utf-8")

    with pytest.raises(ThreadResultRefConflictError, match="result ref"):
        asyncio.run(
            service.finalize_child(
                db,
                None,
                delegation_id=DELEGATION_ID,
                workspace_id=provisioned.workspace_id,
                base_commit=base_commit,
            )
        )

    # The published result ref must keep pointing at the first result commit.
    assert run_git("rev-parse", "--verify", RESULT_REF, cwd=git_repo) == first.result_commit


def test_finalize_rejects_existing_result_with_different_base(db, git_repo):
    """Result reuse requires both the same tree and the same base parent."""
    provisioned, base_commit = make_child(db, git_repo)
    service = ThreadWorkspaceService()
    first = asyncio.run(
        service.finalize_child(
            db,
            None,
            delegation_id=DELEGATION_ID,
            workspace_id=provisioned.workspace_id,
            base_commit=base_commit,
        )
    )
    alternate_base = run_git(
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@t",
        "commit-tree",
        f"{base_commit}^{{tree}}",
        "-p",
        base_commit,
        "-m",
        "alternate base",
        cwd=git_repo,
    )

    with pytest.raises(ThreadResultRefConflictError, match="result ref"):
        asyncio.run(
            service.finalize_child(
                db,
                None,
                delegation_id=DELEGATION_ID,
                workspace_id=provisioned.workspace_id,
                base_commit=alternate_base,
            )
        )

    assert run_git("rev-parse", "--verify", RESULT_REF, cwd=git_repo) == first.result_commit
