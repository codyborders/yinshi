"""Parent-state preservation across thread workspace provisioning."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

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
    return result.stdout


def seed_parent(db: sqlite3.Connection, git_repo: str) -> str:
    """Insert one repo plus a parent workspace backed by a real worktree."""
    run_git("config", "user.name", "T", cwd=git_repo)
    run_git("config", "user.email", "t@t", cwd=git_repo)
    parent_path = str(Path(git_repo) / ".worktrees" / "parent-branch")
    run_git(
        "worktree",
        "add",
        "-b",
        "parent-branch",
        parent_path,
        cwd=git_repo,
    )
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


def capture_parent_state(workspace_path: str) -> dict[str, str]:
    """Record every observable parent Git surface."""
    return {
        "head": run_git("rev-parse", "HEAD", cwd=workspace_path).strip(),
        "branch": run_git("symbolic-ref", "HEAD", cwd=workspace_path).strip(),
        "status": run_git(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
            cwd=workspace_path,
        ),
        "index": run_git("ls-files", "-s", cwd=workspace_path),
        "staged": run_git("diff", "--cached", "--stat", cwd=workspace_path),
        "stash": run_git("stash", "list", cwd=workspace_path),
    }


def test_provision_preserves_dirty_parent_state(db, git_repo):
    """Provisioning never changes parent HEAD, branch, index, or files."""
    parent_path = seed_parent(db, git_repo)
    root = Path(parent_path)
    (root / "README.md").write_text("# dirty\n", encoding="utf-8")
    (root / "tracked.txt").write_text("staged\n", encoding="utf-8")
    (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    (root / "note.log").write_text("ignored\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    run_git("add", "tracked.txt", cwd=parent_path)

    before = capture_parent_state(parent_path)
    file_before = {
        path.name: path.read_bytes() for path in sorted(root.iterdir()) if path.is_file()
    }

    provisioned = asyncio.run(
        ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )

    after = capture_parent_state(parent_path)
    assert after == before
    file_after = {path.name: path.read_bytes() for path in sorted(root.iterdir()) if path.is_file()}
    assert file_after == file_before
    # The ignored note must not leak into the child either.
    child = Path(provisioned.path)
    assert not (child / "note.log").exists()
    assert (child / "untracked.txt").read_text(encoding="utf-8") == "untracked\n"
