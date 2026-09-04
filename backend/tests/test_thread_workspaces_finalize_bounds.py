"""Finalization bounds for thread workspaces."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest

from yinshi.exceptions import YinshiError
from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"


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


def seed_parent(db: sqlite3.Connection, git_repo: str) -> str:
    """Insert one repo plus a parent workspace worktree."""
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


def test_finalize_rejects_changed_files_over_entry_bound(db, git_repo):
    """Finalization fails closed beyond 5000 changed-file entries."""
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
    child = Path(provisioned.path)
    for number in range(5001):
        (child / f"n{number:05d}.txt").write_text("x\n", encoding="utf-8")

    with pytest.raises(YinshiError, match="changed-file entry limit"):
        asyncio.run(
            ThreadWorkspaceService().finalize_child(
                db,
                None,
                delegation_id=DELEGATION_ID,
                workspace_id=provisioned.workspace_id,
                base_commit=base_commit,
            )
        )

    # The bound must fail before any new result ref is written.
    assert run_git("for-each-ref", "--format=%(refname)", "refs/yinshi/results", cwd=git_repo) == ""


def test_finalize_rejects_child_over_snapshot_limits(
    db,
    git_repo,
    monkeypatch: pytest.MonkeyPatch,
):
    """Finalization applies the configured snapshot size bounds to the child."""
    from yinshi.config import get_settings

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
    monkeypatch.setenv("THREAD_SNAPSHOT_MAX_FILES", "1")
    monkeypatch.setenv("THREAD_SNAPSHOT_MAX_BYTES", "1048576")
    get_settings.cache_clear()
    try:
        child = Path(provisioned.path)
        (child / "one.txt").write_text("one\n", encoding="utf-8")
        (child / "two.txt").write_text("two\n", encoding="utf-8")

        with pytest.raises(YinshiError, match="file-count limit"):
            asyncio.run(
                ThreadWorkspaceService().finalize_child(
                    db,
                    None,
                    delegation_id=DELEGATION_ID,
                    workspace_id=provisioned.workspace_id,
                    base_commit=base_commit,
                )
            )
    finally:
        get_settings.cache_clear()

    # The rejected result ref must not exist.
    assert run_git("for-each-ref", "--format=%(refname)", "refs/yinshi/results", cwd=git_repo) == ""


def test_finalize_rejects_dirty_child_submodule(db, git_repo):
    """Finalization refuses a child whose submodule state is uncommitted."""
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
    sub_source = Path(git_repo) / "sub-source"
    sub_source.mkdir()
    run_git("init", str(sub_source), cwd=str(git_repo))
    (sub_source / "lib.txt").write_text("v1\n", encoding="utf-8")
    run_git("add", ".", cwd=str(sub_source))
    run_git(
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@t",
        "commit",
        "-m",
        "c",
        cwd=str(sub_source),
    )
    run_git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(sub_source),
        "vendor/sub",
        cwd=provisioned.path,
    )
    run_git(
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@t",
        "commit",
        "-m",
        "sub",
        cwd=provisioned.path,
    )
    (Path(provisioned.path) / "vendor" / "sub" / "lib.txt").write_text(
        "dirty\n",
        encoding="utf-8",
    )

    with pytest.raises(YinshiError, match="dirty submodule"):
        asyncio.run(
            ThreadWorkspaceService().finalize_child(
                db,
                None,
                delegation_id=DELEGATION_ID,
                workspace_id=provisioned.workspace_id,
                base_commit=base_commit,
            )
        )

    assert run_git("for-each-ref", "--format=%(refname)", "refs/yinshi/results", cwd=git_repo) == ""
