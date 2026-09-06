"""Staging must not adopt or publish through unknown symbolic references."""

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from tests.test_thread_workspaces import run_git
from yinshi.exceptions import YinshiError
from yinshi.models import ThreadChildCreate
from yinshi.services.thread_orchestration import ThreadOrchestrationService


@pytest.mark.parametrize("owner_exists", [False, True])
async def test_spawn_preserves_a_preexisting_dangling_snapshot_ref(db, git_repo, owner_exists):
    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    if owner_exists:
        await service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="First",
                task="Inspect",
                start_immediately=False,
            ),
        )
    identifier = "a123456789abcdef0123456789abcdef"
    ref = f"refs/yinshi/snapshots/{identifier}"
    target = "refs/heads/foreign-snapshot"
    run_git("symbolic-ref", ref, target, cwd=git_repo)
    Path(git_repo, "dirty.txt").write_text("Parent content\n")
    before = run_git("for-each-ref", cwd=git_repo)
    with (
        patch(
            "yinshi.services.thread_orchestration.uuid.uuid4", return_value=uuid.UUID(identifier)
        ),
        pytest.raises(YinshiError),
    ):
        await service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.UUID("b" * 32)),
                title="Child",
                task="Inspect",
                start_immediately=False,
            ),
        )
    assert run_git("symbolic-ref", ref, cwd=git_repo) == target
    assert run_git("for-each-ref", cwd=git_repo) == before
    assert not Path(git_repo, ".worktrees", "yinshi", f"thread-{identifier[:8]}").exists()
    assert Path(git_repo, ".git", ".yinshi-thread-owner-v1.json").exists() == owner_exists


async def test_dirty_root_siblings_exclude_only_owned_registered_worktrees(db, git_repo):
    seed_parent_stack(db, git_repo)
    Path(git_repo, "dirty.txt").write_text("Parent content\n")
    Path(git_repo, ".worktrees").mkdir()
    Path(git_repo, ".worktrees", "notes.txt").write_text("Unrelated user content\n")
    before = tuple(
        run_git(*args, cwd=git_repo)
        for args in (
            ("rev-parse", "HEAD"),
            ("ls-files", "--stage"),
            ("status", "--porcelain=v1", "--untracked-files=all"),
        )
    )
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    children = []
    for number in range(2):
        child = await service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title=f"Child {number}",
                task="Inspect",
                start_immediately=False,
            ),
        )
        children.append(child)
        workspace = db.execute(
            "SELECT w.path FROM workspaces w JOIN thread_delegations d ON d.child_workspace_id = w.id WHERE d.id = ?",
            (child.delegation_id,),
        ).fetchone()[0]
        assert Path(workspace, "dirty.txt").read_text() == "Parent content\n"
        assert Path(workspace, ".worktrees", "notes.txt").read_text() == "Unrelated user content\n"
        assert "160000" not in run_git("ls-files", "--stage", cwd=workspace)
        Path(workspace, "result.txt").write_text(f"Result {number}\n")
    for child in children:
        db.execute(
            "UPDATE thread_delegations SET status = 'completed' WHERE id = ?",
            (child.delegation_id,),
        )
        db.execute(
            "INSERT INTO thread_results (delegation_id, version, source, summary) VALUES (?, 1, 'reported', 'Done')",
            (child.delegation_id,),
        )
        db.commit()
        result = await service.seal_result(request, child_session_id=child.child_session_id)
        assert result["sealed"] is True
        assert [entry["path"] for entry in result["changed_files"]] == ["result.txt"]
    assert before[:2] == tuple(
        run_git(*args, cwd=git_repo)
        for args in (
            ("rev-parse", "HEAD"),
            ("ls-files", "--stage"),
        )
    )
    after_status = set(
        run_git("status", "--porcelain=v1", "--untracked-files=all", cwd=git_repo).splitlines()
    )
    assert after_status == set(before[2].splitlines()) | {
        f"?? .worktrees/yinshi/thread-{child.delegation_id[:8]}/" for child in children
    }
