"""Snapshot publication keeps durable intent across Git faults."""

import sqlite3
import uuid
from pathlib import Path

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from tests.test_thread_workspaces import run_git
from yinshi.exceptions import GitError, YinshiError
from yinshi.models import ThreadChildCreate
from yinshi.services import thread_workspaces
from yinshi.services.thread_orchestration import ThreadOrchestrationService


@pytest.mark.parametrize("published", [False, True])
async def test_snapshot_intent_survives_faults_at_publication(db, git_repo, monkeypatch, published):
    seed_parent_stack(db, git_repo)
    Path(git_repo, "dirty.txt").write_text("Uncommitted parent content\n")
    database_path = db.execute("PRAGMA database_list").fetchone()[2]
    original_git = tuple(
        run_git(*args, cwd=git_repo)
        for args in (
            ("rev-parse", "HEAD"),
            ("status", "--porcelain=v1"),
            ("ls-files", "--stage"),
        )
    )
    real_git = thread_workspaces._run_git
    recorded = []

    async def publication_fault(args, **kwargs):
        if args[0] == "update-ref" and args[-3].startswith("refs/yinshi/snapshots/"):
            independent = sqlite3.connect(database_path, timeout=0)
            try:
                independent.execute("BEGIN IMMEDIATE")
                row = independent.execute(
                    "SELECT snapshot_ref, base_commit, base_kind, status, child_session_id, child_workspace_id "
                    "FROM thread_delegations WHERE id = ?",
                    (args[-3].rsplit("/", 1)[1],),
                ).fetchone()
                assert row == (args[-3], args[-2], "snapshot", "provisioning", None, None)
                recorded.append(row)
                independent.rollback()
            finally:
                independent.close()
            if published:
                await real_git(args, **kwargs)
            raise GitError("Injected publication interruption")
        return await real_git(args, **kwargs)

    monkeypatch.setattr(thread_workspaces, "_run_git", publication_fault)
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
    assert len(recorded) == 1
    row = db.execute("SELECT * FROM thread_delegations").fetchone()
    assert row["status"] == "failed"
    assert row["snapshot_ref"] == recorded[0][0]
    assert row["base_commit"] == recorded[0][1]
    assert row["git_artifacts_claimed"] == 0
    assert run_git("for-each-ref", "--format=%(refname)", "refs/yinshi", cwd=git_repo) == ""
    assert (
        tuple(
            run_git(*args, cwd=git_repo)
            for args in (
                ("rev-parse", "HEAD"),
                ("status", "--porcelain=v1"),
                ("ls-files", "--stage"),
            )
        )
        == original_git
    )
