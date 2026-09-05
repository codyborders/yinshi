"""seal_result service behavior for terminal delegated child threads."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from tests.test_thread_workspaces import seed_parent_stack
from yinshi.services.thread_workspaces import ThreadWorkspaceService

DELEGATION_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
RESULT_REF = f"refs/yinshi/results/{DELEGATION_ID}"


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


def _orchestration_request() -> object:
    """Build one minimal request carrying no tenant, like report tests."""
    from types import SimpleNamespace

    from fastapi import Request

    app = SimpleNamespace(state=SimpleNamespace())
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
            "app": app,
            "state": {},
        }
    )


def seed_seal_child(db, git_repo: str, status: str = "completed") -> str:
    """Provision one real child worktree and seed its delegation plus draft."""
    seed_parent_stack(db, git_repo)
    provisioned = asyncio.run(
        ThreadWorkspaceService().provision_child(
            db,
            None,
            parent_workspace_id="parent-ws",
            delegation_id=DELEGATION_ID,
        )
    )
    child_path = provisioned.path
    Path(child_path, "work.txt").write_text("child work\n", encoding="utf-8")
    run_git("add", "work.txt", cwd=child_path)
    run_git(
        "-c",
        "user.name=Child",
        "-c",
        "user.email=child@t",
        "commit",
        "-m",
        "child commit",
        cwd=child_path,
    )
    db.execute(
        """INSERT INTO sessions (id, workspace_id) VALUES ('child-session', ?)""",
        (provisioned.workspace_id,),
    )
    db.execute(
        """INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, child_workspace_id,
               idempotency_key, initiator, title, task, requested_model, status,
               base_commit
           ) VALUES (
               ?, 'parent-session', 'child-session', ?,
               'k1', 'user', 'Child', 'task', 'm', ?, ?
           )""",
        (DELEGATION_ID, provisioned.workspace_id, status, provisioned.base_commit),
    )
    db.execute(
        """INSERT INTO thread_results (
               delegation_id, version, source, summary, tests_json, warnings_json
           ) VALUES (?, 2, 'reported', 'draft summary', '[]', '[]')""",
        (DELEGATION_ID,),
    )
    db.commit()
    return str(child_path)


def test_seal_creates_synthetic_commit_and_changed_files(db, git_repo) -> None:
    """Sealing publishes the result ref and stores the sealed projection."""
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    child_path = seed_seal_child(db, git_repo)
    Path(child_path, "loose.txt").write_text("loose\n", encoding="utf-8")
    service = ThreadOrchestrationService()

    outcome = asyncio.run(
        service.seal_result(_orchestration_request(), child_session_id="child-session")
    )

    assert outcome["delegation_id"] == DELEGATION_ID
    assert outcome["version"] == 2
    assert outcome["sealed"] is True
    assert outcome["result_ref"] == RESULT_REF
    assert outcome["base_commit"]
    assert outcome["result_commit"]
    ref_commit = run_git("rev-parse", RESULT_REF, cwd=git_repo)
    assert ref_commit == outcome["result_commit"]
    result_parent = run_git("rev-parse", f"{outcome['result_commit']}^", cwd=git_repo)
    assert result_parent == outcome["base_commit"]
    changed = outcome["changed_files"]
    names = [entry["path"] for entry in changed]
    assert "work.txt" in names
    assert "loose.txt" in names
    stored = db.execute(
        "SELECT * FROM thread_results WHERE delegation_id = ?", (DELEGATION_ID,)
    ).fetchone()
    assert stored is not None
    assert stored["sealed"] == 1
    assert stored["sealed_at"] is not None
    assert stored["base_commit"] == outcome["base_commit"]
    assert stored["result_commit"] == outcome["result_commit"]
    assert stored["result_ref"] == RESULT_REF
    stored_changed = json.loads(str(stored["changed_files_json"]))
    assert {entry["path"] for entry in stored_changed} == {"work.txt", "loose.txt"}


def test_repeated_seal_returns_same_stored_result(db, git_repo) -> None:
    """A second seal reuses the ref and returns the same stored result."""
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_seal_child(db, git_repo)
    service = ThreadOrchestrationService()

    first = asyncio.run(
        service.seal_result(_orchestration_request(), child_session_id="child-session")
    )
    second = asyncio.run(
        service.seal_result(_orchestration_request(), child_session_id="child-session")
    )

    assert second["result_commit"] == first["result_commit"]
    assert second["result_ref"] == first["result_ref"]
    assert second["base_commit"] == first["base_commit"]
    assert second["sealed"] is True
    refs = run_git(
        "for-each-ref",
        "--format=%(refname)",
        "refs/yinshi/results",
        cwd=git_repo,
    )
    assert refs.count(RESULT_REF) == 1


def test_seal_rejects_nonterminal_delegation(db, git_repo) -> None:
    """A running delegation raises the typed nonterminal conflict."""
    import pytest

    from yinshi.services.thread_orchestration import (
        ThreadOrchestrationService,
        ThreadResultNotSealableError,
    )

    seed_seal_child(db, git_repo, status="running")
    service = ThreadOrchestrationService()

    with pytest.raises(ThreadResultNotSealableError) as excinfo:
        asyncio.run(service.seal_result(_orchestration_request(), child_session_id="child-session"))

    assert excinfo.value.code == "result_not_terminal"
    row = db.execute(
        "SELECT sealed FROM thread_results WHERE delegation_id = ?", (DELEGATION_ID,)
    ).fetchone()
    assert row is not None
    assert row["sealed"] == 0


def test_seal_requires_existing_reported_draft(db, git_repo) -> None:
    """Sealing without a stored reported draft raises the typed conflict."""
    import pytest

    from yinshi.services.thread_orchestration import (
        ThreadOrchestrationService,
        ThreadResultDraftMissingError,
    )

    seed_seal_child(db, git_repo)
    db.execute("DELETE FROM thread_results WHERE delegation_id = ?", (DELEGATION_ID,))
    db.commit()
    service = ThreadOrchestrationService()

    with pytest.raises(ThreadResultDraftMissingError) as excinfo:
        asyncio.run(service.seal_result(_orchestration_request(), child_session_id="child-session"))

    assert excinfo.value.code == "result_draft_missing"


def test_seal_git_failure_preserves_draft(db, git_repo, monkeypatch) -> None:
    """A Git bounds failure leaves the draft unsealed and status untouched."""
    from yinshi.config import get_settings
    from yinshi.services.thread_orchestration import ThreadOrchestrationService
    from yinshi.services.thread_workspaces import ThreadSnapshotLimitError

    child_path = seed_seal_child(db, git_repo)
    Path(child_path, "extra.txt").write_text("extra\n", encoding="utf-8")
    monkeypatch.setenv("THREAD_SNAPSHOT_MAX_FILES", "1")
    get_settings.cache_clear()
    service = ThreadOrchestrationService()

    with pytest.raises(ThreadSnapshotLimitError):
        asyncio.run(service.seal_result(_orchestration_request(), child_session_id="child-session"))
    get_settings.cache_clear()

    row = db.execute(
        "SELECT sealed FROM thread_results WHERE delegation_id = ?", (DELEGATION_ID,)
    ).fetchone()
    assert row is not None
    assert row["sealed"] == 0
    delegation = db.execute(
        "SELECT status FROM thread_delegations WHERE id = ?", (DELEGATION_ID,)
    ).fetchone()
    assert delegation is not None
    assert delegation["status"] == "completed"
    output = run_git(
        "for-each-ref",
        "--format=%(refname)",
        "refs/yinshi/results",
        cwd=git_repo,
    )
    assert RESULT_REF not in output


def test_seal_repeated_with_divergent_git_metadata_conflicts(db, git_repo) -> None:
    """A repeated seal whose stored commit diverges raises the typed conflict."""
    from yinshi.services.thread_orchestration import (
        ThreadOrchestrationService,
        ThreadResultSealConflictError,
    )

    seed_seal_child(db, git_repo)
    service = ThreadOrchestrationService()
    asyncio.run(service.seal_result(_orchestration_request(), child_session_id="child-session"))
    db.execute(
        "UPDATE thread_results SET result_commit = ? WHERE delegation_id = ?",
        ("0" * 40, DELEGATION_ID),
    )
    db.commit()

    with pytest.raises(ThreadResultSealConflictError) as excinfo:
        asyncio.run(service.seal_result(_orchestration_request(), child_session_id="child-session"))

    assert excinfo.value.code == "result_seal_conflict"


def test_seal_hides_unknown_foreign_and_root_sessions(db, git_repo) -> None:
    """Unknown, foreign, and root sessions raise hidden not-found errors."""
    from yinshi.models import ThreadResultReportCreate  # noqa: F401
    from yinshi.services.thread_orchestration import (
        ThreadNotFoundError,
        ThreadOrchestrationService,
        ThreadParentNotAuthorizedError,
    )

    seed_seal_child(db, git_repo)
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('root-session', 'parent-ws')")
    db.execute("UPDATE repos SET owner_email = 'a@example.com' WHERE id = 'repo1'")
    db.commit()
    service = ThreadOrchestrationService()

    with pytest.raises(ThreadNotFoundError):
        asyncio.run(service.seal_result(_orchestration_request(), child_session_id="missing"))
    with pytest.raises(ThreadNotFoundError):
        asyncio.run(service.seal_result(_orchestration_request(), child_session_id="root-session"))
    foreign_request = _orchestration_request()
    foreign_request.state.user_email = "b@example.com"
    with pytest.raises(ThreadParentNotAuthorizedError):
        asyncio.run(service.seal_result(foreign_request, child_session_id="child-session"))
