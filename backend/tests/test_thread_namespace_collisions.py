"""Physical claims prevent short-name collisions through repository aliases."""

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from tests.test_thread_provisioning_cancel import _force_pre_attach
from tests.test_thread_workspaces import run_git
from yinshi.exceptions import YinshiError
from yinshi.models import ThreadChildCreate
from yinshi.services.thread_orchestration import ThreadOrchestrationService


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("before_git", [False, True])
async def test_short_namespace_is_exclusive_across_repository_aliases(
    db, git_repo, tmp_path, linked, before_git
):
    seed_parent_stack(db, git_repo)
    first_id = "12345678" + "a" * 24
    second_id = "12345678" + "b" * 24
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    body = ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()), title="First", task="Inspect", start_immediately=False
    )
    with patch("yinshi.services.thread_orchestration.uuid.uuid4", return_value=uuid.UUID(first_id)):
        first = await service.spawn_child(request, parent_session_id="parent-session", body=body)
    owner_file = Path(git_repo, ".git", ".yinshi-thread-owner-v1.json")
    owner = owner_file.read_bytes()
    if before_git:
        worktree = _force_pre_attach(db, first)
        oid = run_git("rev-parse", f"refs/heads/yinshi/thread-{first_id[:8]}", cwd=git_repo)
        run_git("worktree", "remove", "--force", worktree, cwd=git_repo)
        run_git(
            "update-ref",
            "--no-deref",
            "-d",
            f"refs/heads/yinshi/thread-{first_id[:8]}",
            oid,
            cwd=git_repo,
        )
    alias = git_repo
    if linked:
        alias = str(tmp_path / "linked")
        run_git("worktree", "add", "-b", "alias-parent", alias, "HEAD", cwd=git_repo)
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('alias-repo', 'Alias', ?)", (alias,)
    )
    db.execute(
        "INSERT INTO workspaces (id, repo_id, name, branch, path) VALUES ('alias-workspace', 'alias-repo', 'Alias', 'alias-parent', ?)",
        (alias,),
    )
    db.execute(
        "INSERT INTO sessions (id, workspace_id, title) VALUES ('alias-parent', 'alias-workspace', 'Alias')"
    )
    db.commit()
    before_row = tuple(
        db.execute("SELECT * FROM thread_delegations WHERE id = ?", (first_id,)).fetchone()
    )
    refs = run_git("for-each-ref", cwd=git_repo)
    body = ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()), title="Second", task="Inspect", start_immediately=False
    )
    with (
        patch("yinshi.services.thread_orchestration.uuid.uuid4", return_value=uuid.UUID(second_id)),
        pytest.raises(YinshiError),
    ):
        await service.spawn_child(request, parent_session_id="alias-parent", body=body)
    assert (
        tuple(db.execute("SELECT * FROM thread_delegations WHERE id = ?", (first_id,)).fetchone())
        == before_row
    )
    assert (
        db.execute(
            "SELECT git_artifacts_claimed FROM thread_delegations WHERE id = ?", (second_id,)
        ).fetchone()[0]
        == 0
    )
    assert run_git("for-each-ref", cwd=git_repo) == refs
    assert owner_file.read_bytes() == owner
