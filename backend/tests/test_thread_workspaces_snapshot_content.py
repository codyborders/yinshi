"""Snapshot content correctness across parent dirty-state scenarios."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest

from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"


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


def apply_scenario(parent_path: str, scenario: str) -> None:
    """Arrange one specific dirty-parent Git state."""
    root = Path(parent_path)
    if scenario == "modified":
        (root / "README.md").write_text("# changed\n", encoding="utf-8")
    elif scenario == "staged":
        (root / "staged.txt").write_text("staged\n", encoding="utf-8")
        run_git("add", "staged.txt", cwd=parent_path)
    elif scenario == "unstaged":
        (root / "README.md").write_text("# staged\n", encoding="utf-8")
        run_git("add", "README.md", cwd=parent_path)
        (root / "README.md").write_text("# unstaged\n", encoding="utf-8")
    elif scenario == "added":
        (root / "added.txt").write_text("added\n", encoding="utf-8")
    elif scenario == "deleted":
        (root / "README.md").unlink()
    elif scenario == "renamed":
        run_git("mv", "README.md", "NOTES.md", cwd=parent_path)
    elif scenario == "ignored":
        (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
        (root / "noise.log").write_text("noise\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("scenario", "present", "absent", "content_of", "content"),
    [
        ("modified", ["README.md"], [], "README.md", "# changed\n"),
        ("staged", ["staged.txt"], [], None, None),
        ("unstaged", ["README.md"], [], "README.md", "# unstaged\n"),
        ("added", ["added.txt"], [], None, None),
        ("deleted", [], ["README.md"], None, None),
        ("renamed", ["NOTES.md"], ["README.md"], None, None),
        ("ignored", [], ["noise.log"], None, None),
    ],
)
def test_snapshot_captures_parent_state_exactly(
    db,
    git_repo,
    scenario,
    present,
    absent,
    content_of,
    content,
):
    """The child worktree mirrors exactly the parent's dirty Git state."""
    parent_path = seed_parent(db, git_repo)
    apply_scenario(parent_path, scenario)

    provisioned = asyncio.run(
        ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )

    assert provisioned.base_kind == "snapshot"
    for relative_path in present:
        assert Path(provisioned.path, relative_path).exists(), relative_path
    for relative_path in absent:
        assert not Path(provisioned.path, relative_path).exists(), relative_path
    if content_of is not None:
        actual = Path(provisioned.path, content_of).read_text(encoding="utf-8")
        assert actual == content
