"""Owned operations must not write through redirected Git storage."""

import uuid
from pathlib import Path

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from tests.test_thread_provisioning_cancel import _force_pre_attach
from tests.test_thread_workspaces import run_git
from yinshi.exceptions import YinshiError
from yinshi.models import ThreadChildCreate
from yinshi.services.thread_orchestration import ThreadOrchestrationService


@pytest.mark.parametrize("redirect", ["foreign", "main", "worktree_config"])
async def test_parent_gitfile_redirection_cannot_write_into_foreign_objects(
    db, git_repo, tmp_path, redirect
):
    seed_parent_stack(db, git_repo)
    parent = tmp_path / "parent-worktree"
    foreign = tmp_path / "foreign-repository"
    run_git("worktree", "add", "-b", "parent-worktree", str(parent), "HEAD", cwd=git_repo)
    run_git("clone", "--no-hardlinks", git_repo, str(foreign), cwd=tmp_path)
    assert (
        db.execute(
            "UPDATE workspaces SET path = ? WHERE id = (SELECT workspace_id FROM sessions WHERE id = 'parent-session')",
            (str(parent),),
        ).rowcount
        == 1
    )
    db.commit()
    if redirect == "worktree_config":
        run_git("config", "extensions.worktreeConfig", "true", cwd=git_repo)
        run_git("config", "--worktree", "core.worktree", str(foreign), cwd=parent)
    else:
        target = foreign / ".git" if redirect == "foreign" else Path(git_repo, ".git")
        (parent / ".git").write_text(f"gitdir: {target}\n")
    (parent / "private-output.txt").write_text(
        "Parent content must remain in the selected repository\n"
    )
    before = run_git("count-objects", "-v", cwd=foreign)
    with pytest.raises(YinshiError):
        await ThreadOrchestrationService().spawn_child(
            _orchestration_request(),
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Child",
                task="Inspect",
                start_immediately=False,
            ),
        )
    assert run_git("count-objects", "-v", cwd=foreign) == before
    assert (
        db.execute(
            "SELECT COUNT(*) FROM thread_delegations WHERE child_session_id IS NOT NULL"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("storage", ["objects", "worktrees", "packed-refs"])
async def test_redirected_git_storage_retains_owned_artifacts(db, git_repo, tmp_path, storage):
    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    first = await service.spawn_child(
        request,
        parent_session_id="parent-session",
        body=ThreadChildCreate(
            idempotency_key=str(uuid.uuid4()),
            title="First",
            task="Inspect",
            start_immediately=False,
        ),
    )
    common = Path(git_repo, ".git")
    if storage == "packed-refs":
        run_git("pack-refs", "--all", cwd=git_repo)
    original = common / storage
    foreign = tmp_path / f"foreign-{storage}"
    original.rename(foreign)
    original.symlink_to(foreign, target_is_directory=storage != "packed-refs")
    if storage == "worktrees":
        for metadata in foreign.iterdir():
            (metadata / "commondir").write_text(f"{common}\n")
    before = (
        foreign.read_bytes()
        if foreign.is_file()
        else sorted(str(path.relative_to(foreign)) for path in foreign.rglob("*"))
    )
    if storage == "packed-refs":
        worktree = _force_pre_attach(db, first)
        cancelled = await service.cancel_child(request, thread_id=first.delegation_id)
        assert cancelled.status == "cancelled"
        assert Path(worktree).exists()
    else:
        Path(git_repo, "new-content.txt").write_text("This must not enter foreign storage\n")
        with pytest.raises(YinshiError):
            await service.spawn_child(
                request,
                parent_session_id="parent-session",
                body=ThreadChildCreate(
                    idempotency_key=str(uuid.uuid4()),
                    title="Second",
                    task="Inspect",
                    start_immediately=False,
                ),
            )
    assert (
        db.execute(
            "SELECT git_artifacts_claimed FROM thread_delegations WHERE id = ?",
            (first.delegation_id,),
        ).fetchone()[0]
        == 1
    )
    after = (
        foreign.read_bytes()
        if foreign.is_file()
        else sorted(str(path.relative_to(foreign)) for path in foreign.rglob("*"))
    )
    assert after == before
    assert original.is_symlink()
