"""Concurrent sibling provisioning for thread workspaces."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_IDS = (
    "11111111b8c9d0e1f2a3b4c5d6e7f801",
    "22222222b8c9d0e1f2a3b4c5d6e7f801",
)


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


def test_concurrent_siblings_provision_independently(db, git_repo):
    """Two simultaneous provisions create distinct isolated worktrees."""
    parent_path = seed_parent(db, git_repo)
    (Path(parent_path) / "shared.txt").write_text("base\n", encoding="utf-8")
    service = ThreadWorkspaceService()

    async def two_children():
        return tuple(
            await asyncio.gather(
                service.provision_child(
                    db,
                    None,
                    parent_workspace_id="parent-ws",
                    delegation_id=DELEGATION_IDS[0],
                ),
                service.provision_child(
                    db,
                    None,
                    parent_workspace_id="parent-ws",
                    delegation_id=DELEGATION_IDS[1],
                ),
            )
        )

    first, second = asyncio.run(two_children())

    assert first.branch == "yinshi/thread-11111111"
    assert second.branch == "yinshi/thread-22222222"
    # Each sibling owns its own snapshot commit; the captured content matches.
    first_tree = run_git("rev-parse", f"{first.base_commit}^{{tree}}", cwd=git_repo)
    second_tree = run_git("rev-parse", f"{second.base_commit}^{{tree}}", cwd=git_repo)
    assert first_tree == second_tree
    assert first.path != second.path
    delegated_rows = db.execute(
        "SELECT count(*) FROM workspaces WHERE kind = 'delegated'",
    ).fetchone()[0]
    assert delegated_rows == 2

    # A child edit stays invisible to its sibling.
    (Path(first.path) / "only-first.txt").write_text("one\n", encoding="utf-8")
    assert (Path(first.path) / "only-first.txt").exists()
    assert not (Path(second.path) / "only-first.txt").exists()
    assert not (Path(parent_path) / "only-first.txt").exists()


def test_provision_serializes_on_repository_lifecycle_lock(db, git_repo, monkeypatch):
    """Provisioning waits while one ordinary workspace operation holds the lock."""
    from yinshi.services.repository_lifecycle import repository_lifecycle, repository_lifecycle_root

    parent_path = seed_parent(db, git_repo)
    lock_root = repository_lifecycle_root(db, None)
    service = ThreadWorkspaceService()
    events: list[str] = []

    async def scenario():
        async def hold_lock_like_workspace_creation():
            async with repository_lifecycle("repo1", lock_root):
                events.append("lock-held")
                await asyncio.sleep(0.05)
                events.append("lock-released")

        async def provision():
            await service.provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id=DELEGATION_IDS[0],
            )
            events.append("provisioned")

        await asyncio.gather(hold_lock_like_workspace_creation(), provision())

    asyncio.run(scenario())
    assert events == ["lock-held", "lock-released", "provisioned"]
    assert Path(parent_path).exists()
