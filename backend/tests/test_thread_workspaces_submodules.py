"""Dirty-submodule rejection for thread workspace snapshots."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from yinshi.exceptions import YinshiError
from yinshi.services.thread_workspaces import ThreadWorkspaceService


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


def seed_parent(db, git_repo: str) -> None:
    """Insert one repo, parent workspace, and parent session."""
    branch = run_git("symbolic-ref", "--short", "HEAD", cwd=git_repo)
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'repo', ?)",
        (git_repo,),
    )
    db.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path, state)
           VALUES ('parent-ws', 'repo1', 'parent', ?, ?, 'ready')""",
        (branch, git_repo),
    )
    db.execute(
        "INSERT INTO sessions (id, workspace_id) VALUES ('parent-session', 'parent-ws')",
    )
    db.commit()


def test_snapshot_rejects_dirty_submodule(db, git_repo):
    """Parents with uncommitted submodule state fail closed."""
    seed_parent(db, git_repo)

    sub_source = Path(git_repo).parent / "sub-source"
    sub_source.mkdir()
    run_git("init", str(sub_source), cwd=str(sub_source.parent))
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
        "../sub-source",
        "vendor/sub",
        cwd=git_repo,
    )
    run_git(
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@t",
        "commit",
        "-m",
        "sub",
        cwd=git_repo,
    )
    # Make the checked-out submodule dirty without committing inside it.
    sub_checkout = Path(git_repo) / "vendor" / "sub"
    (sub_checkout / "lib.txt").write_text("v2\n", encoding="utf-8")

    with pytest.raises(YinshiError, match="dirty submodule"):
        asyncio.run(
            ThreadWorkspaceService().provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id="d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801",
            )
        )
