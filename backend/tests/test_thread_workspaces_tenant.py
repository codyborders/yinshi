"""Tenant-scoped thread workspace provisioning."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"


@pytest.fixture
def tenant_env(tmp_path, monkeypatch):
    """Isolated tenant environment matching test_thread_schema conventions."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    from yinshi.config import get_settings

    get_settings.cache_clear()
    yield {"user_data_dir": str(tmp_path / "users")}
    get_settings.cache_clear()


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


def test_provision_scopes_lock_and_rows_to_tenant(tenant_env):
    """Provisioning works against a tenant database and preserves user rows."""
    import yinshi.tenant as tenant_module
    from yinshi.tenant import TenantContext, get_user_db

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "threadsuser")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)

    git_repo = os.path.join(data_dir, "repos", "repo-checkout")
    os.makedirs(git_repo)
    run_git("init", git_repo, cwd=data_dir)
    run_git("config", "user.name", "T", cwd=git_repo)
    run_git("config", "user.email", "t@t", cwd=git_repo)
    os.makedirs(os.path.join(git_repo, ".worktrees"), exist_ok=True)
    Path(git_repo, "README.md").write_text("# t\n", encoding="utf-8")
    run_git("add", ".", cwd=git_repo)
    run_git(
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@t",
        "commit",
        "-m",
        "init",
        cwd=git_repo,
    )
    parent_path = os.path.join(git_repo, ".worktrees", "parent-branch")
    run_git("worktree", "add", "-b", "parent-branch", parent_path, cwd=git_repo)

    context = TenantContext(
        user_id="threadsuser",
        email="threadsuser@example.com",
        data_dir=data_dir,
        db_path=db_path,
    )

    with get_user_db(context) as conn:
        conn.execute(
            "INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'repo', ?)",
            (git_repo,),
        )
        conn.execute(
            """INSERT INTO workspaces (id, repo_id, name, branch, path, state)
               VALUES ('parent-ws', 'repo1', 'parent', 'parent-branch', ?, 'ready')""",
            (parent_path,),
        )
        conn.execute(
            "INSERT INTO sessions (id, workspace_id) VALUES ('parent-session', 'parent-ws')",
        )
        conn.commit()

        provisioned = asyncio.run(
            ThreadWorkspaceService().provision_child(
                conn,
                context,
                parent_workspace_id="parent-ws",
                delegation_id=DELEGATION_ID,
            )
        )

        parent_row = conn.execute(
            "SELECT kind FROM workspaces WHERE id = 'parent-ws'",
        ).fetchone()
        child_row = conn.execute(
            "SELECT kind, parent_workspace_id, state FROM workspaces WHERE id = ?",
            (provisioned.workspace_id,),
        ).fetchone()

    assert parent_row is not None and parent_row["kind"] == "user"
    assert child_row is not None
    assert child_row["kind"] == "delegated"
    assert child_row["parent_workspace_id"] == "parent-ws"
    assert child_row["state"] == "ready"
    assert provisioned.base_kind == "head"
    tenant_module._MIGRATION_THREAD_LOCKS.clear()
