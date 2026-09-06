"""Core sealing cannot mutate artifacts without physical Git ownership."""

import sqlite3
import uuid
from pathlib import Path

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from tests.test_thread_workspaces import run_git
from yinshi.config import get_settings
from yinshi.exceptions import YinshiError
from yinshi.models import ThreadChildCreate
from yinshi.services.thread_orchestration import ThreadOrchestrationService


async def test_sealing_rejects_a_child_gitfile_redirected_to_another_repository(
    db, git_repo, tmp_path
):
    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    child = await service.spawn_child(
        request,
        parent_session_id="parent-session",
        body=ThreadChildCreate(
            idempotency_key=str(uuid.uuid4()),
            title="Child",
            task="Inspect",
            start_immediately=False,
        ),
    )
    path = db.execute(
        "SELECT w.path FROM workspaces w JOIN thread_delegations d ON d.child_workspace_id = w.id WHERE d.id = ?",
        (child.delegation_id,),
    ).fetchone()[0]
    foreign = tmp_path / "foreign"
    run_git("clone", "--no-hardlinks", git_repo, str(foreign), cwd=tmp_path)
    Path(path, ".git").write_text(f"gitdir: {foreign / '.git'}\n")
    Path(path, "private-output.txt").write_text("Output belongs only to the original repository\n")
    db.execute(
        "UPDATE thread_delegations SET status = 'completed' WHERE id = ?", (child.delegation_id,)
    )
    db.execute(
        "INSERT INTO thread_results (delegation_id, version, source, summary) VALUES (?, 1, 'reported', 'Done')",
        (child.delegation_id,),
    )
    db.commit()
    foreign_objects = run_git("count-objects", "-v", cwd=foreign)
    foreign_refs = run_git("for-each-ref", cwd=foreign)
    with pytest.raises(YinshiError):
        await service.seal_result(request, child_session_id=child.child_session_id)
    assert run_git("count-objects", "-v", cwd=foreign) == foreign_objects
    assert run_git("for-each-ref", cwd=foreign) == foreign_refs
    assert db.execute("SELECT sealed FROM thread_results").fetchone()[0] == 0
    assert db.execute("SELECT status FROM thread_delegations").fetchone()[0] == "completed"


@pytest.mark.parametrize("invalid", ["missing", "namespace", "copied"])
async def test_sealing_requires_current_physical_ownership(
    db, git_repo, tmp_path, monkeypatch, invalid
):
    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    child = await service.spawn_child(
        request,
        parent_session_id="parent-session",
        body=ThreadChildCreate(
            idempotency_key=str(uuid.uuid4()),
            title="Child",
            task="Inspect",
            start_immediately=False,
        ),
    )
    path = db.execute(
        "SELECT w.path FROM workspaces w JOIN thread_delegations d ON d.child_workspace_id = w.id WHERE d.id = ?",
        (child.delegation_id,),
    ).fetchone()[0]
    Path(path, "output.txt").write_text("Child output\n")
    db.execute(
        "UPDATE thread_delegations SET status = 'completed' WHERE id = ?", (child.delegation_id,)
    )
    db.execute(
        "INSERT INTO thread_results (delegation_id, version, source, summary, tests_json, warnings_json) VALUES (?, 1, 'reported', 'Done', '[]', '[]')",
        (child.delegation_id,),
    )
    if invalid == "missing":
        db.execute(
            "UPDATE thread_delegations SET git_artifacts_claimed = 0 WHERE id = ?",
            (child.delegation_id,),
        )
    elif invalid == "namespace":
        db.execute(
            "UPDATE thread_delegations SET git_artifact_namespace = ? WHERE id = ?",
            ("f" * 64, child.delegation_id),
        )
    db.commit()
    original = [tuple(row) for row in db.execute("SELECT * FROM thread_delegations")]
    git_state = run_git("for-each-ref", cwd=git_repo)
    if invalid == "copied":
        other_path = tmp_path / "copy.db"
        other = sqlite3.connect(other_path)
        try:
            db.backup(other)
        finally:
            other.close()
        monkeypatch.setenv("DB_PATH", str(other_path))
        get_settings.cache_clear()
    try:
        with pytest.raises(YinshiError):
            await service.seal_result(request, child_session_id=child.child_session_id)
        assert run_git("for-each-ref", cwd=git_repo) == git_state
        assert Path(path, "output.txt").read_text() == "Child output\n"
        assert [tuple(row) for row in db.execute("SELECT * FROM thread_delegations")] == original
        assert db.execute("SELECT sealed FROM thread_results").fetchone()[0] == 0
    finally:
        get_settings.cache_clear()
