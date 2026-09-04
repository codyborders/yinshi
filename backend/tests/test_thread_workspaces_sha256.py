"""Hidden-ref publication in SHA-256 Git repositories."""

from __future__ import annotations

import asyncio
import subprocess

from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"


def run_git(*args: str, cwd: str) -> str:
    """Run one setup Git command."""
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def test_snapshot_publication_supports_sha256_repositories(db, tmp_path):
    """Create-only hidden refs use the repository object format."""
    repo_path = tmp_path / "sha256-repo"
    repo_path.mkdir()
    run_git("init", "--object-format=sha256", "-b", "main", cwd=str(repo_path))
    run_git("config", "user.name", "T", cwd=str(repo_path))
    run_git("config", "user.email", "t@t", cwd=str(repo_path))
    (repo_path / "README.md").write_text("base\n", encoding="utf-8")
    run_git("add", "README.md", cwd=str(repo_path))
    run_git("commit", "-m", "base", cwd=str(repo_path))
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'repo', ?)",
        (str(repo_path),),
    )
    db.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path, state)
           VALUES ('parent-ws', 'repo1', 'parent', 'main', ?, 'ready')""",
        (str(repo_path),),
    )
    db.commit()
    (repo_path / "README.md").write_text("dirty\n", encoding="utf-8")

    provisioned = asyncio.run(
        ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )

    assert provisioned.snapshot_ref is not None
    assert len(provisioned.base_commit) == 64
    assert run_git(
        "rev-parse",
        "--verify",
        provisioned.snapshot_ref,
        cwd=str(repo_path),
    )
