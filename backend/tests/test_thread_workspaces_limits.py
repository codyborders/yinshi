"""Snapshot size-limit rejection for thread workspaces."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from yinshi.config import get_settings
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


def test_snapshot_rejects_file_count_limit(
    db,
    git_repo,
    monkeypatch: pytest.MonkeyPatch,
):
    """Snapshots beyond the configured file-count limit fail closed."""
    seed_parent(db, git_repo)
    monkeypatch.setenv("THREAD_SNAPSHOT_MAX_FILES", "1")
    monkeypatch.setenv("THREAD_SNAPSHOT_MAX_BYTES", "1048576")
    get_settings.cache_clear()
    try:
        assert get_settings().thread_snapshot_max_files == 1
        (Path(git_repo) / "one.txt").write_text("one\n", encoding="utf-8")
        (Path(git_repo) / "two.txt").write_text("two\n", encoding="utf-8")

        with pytest.raises(YinshiError, match="file-count limit"):
            asyncio.run(
                ThreadWorkspaceService().provision_child(
                    db,
                    None,
                    parent_workspace_id="parent-ws",
                    delegation_id=DELEGATION_ID,
                )
            )
    finally:
        get_settings.cache_clear()

    assert run_git("for-each-ref", "--format=%(refname)", "refs/yinshi", cwd=git_repo) == ""


def test_snapshot_rejects_byte_limit(db, git_repo, monkeypatch: pytest.MonkeyPatch):
    """Snapshots beyond the configured byte limit fail closed."""
    seed_parent(db, git_repo)
    monkeypatch.setenv("THREAD_SNAPSHOT_MAX_FILES", "100")
    monkeypatch.setenv("THREAD_SNAPSHOT_MAX_BYTES", "10")
    get_settings.cache_clear()
    try:
        (Path(git_repo) / "big.txt").write_bytes(b"0123456789abcdef\n")

        with pytest.raises(YinshiError, match="byte limit"):
            asyncio.run(
                ThreadWorkspaceService().provision_child(
                    db,
                    None,
                    parent_workspace_id="parent-ws",
                    delegation_id=DELEGATION_ID,
                )
            )
    finally:
        get_settings.cache_clear()

    assert run_git("for-each-ref", "--format=%(refname)", "refs/yinshi", cwd=git_repo) == ""
