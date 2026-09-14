"""Workspace cleanup validates ownership before external effects."""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.exceptions import GitError, WorkspaceHasDelegatedThreads
from yinshi.models import ThreadChildCreate
from yinshi.services import workspace
from yinshi.services.thread_orchestration import ThreadNotFoundError, ThreadOrchestrationService


def _git(*args: str, cwd: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def _create_child(db, git_repo: str, *, parent_session_id: str = "parent-session"):
    if parent_session_id == "parent-session":
        seed_parent_stack(db, git_repo)
    outcome = await ThreadOrchestrationService().spawn_child(
        _orchestration_request(),
        parent_session_id=parent_session_id,
        body=ThreadChildCreate(
            idempotency_key=str(uuid.uuid4()),
            title="Child",
            task="Inspect",
            start_immediately=False,
        ),
    )
    return db.execute(
        "SELECT d.*, s.workspace_id, w.path AS workspace_path "
        "FROM thread_delegations d "
        "JOIN sessions s ON s.id = d.child_session_id "
        "JOIN workspaces w ON w.id = s.workspace_id WHERE d.id = ?",
        (outcome.delegation_id,),
    ).fetchone()


async def test_cleanup_rejects_invalid_owned_ref_before_removing_worktree(db, git_repo) -> None:
    child = await _create_child(db, git_repo)
    child_path = Path(child["workspace_path"])
    refs_before = _git(
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/yinshi",
        cwd=git_repo,
    )
    db.execute(
        "UPDATE thread_delegations SET snapshot_ref = 'refs/heads/main' WHERE id = ?",
        (child["id"],),
    )
    db.commit()

    with pytest.raises(GitError, match="ownership"):
        workspace.prepare_workspace_deletion(db, child["workspace_id"])

    assert child_path.exists()
    assert (
        _git(
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/yinshi",
            cwd=git_repo,
        )
        == refs_before
    )


async def test_cleanup_claim_rejects_new_descendant_before_external_effects(
    db,
    git_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THREAD_MAX_DEPTH", "3")
    from yinshi.config import get_settings

    get_settings.cache_clear()
    child = await _create_child(db, git_repo)
    target = workspace.prepare_workspace_deletion(db, child["workspace_id"])
    child_path = Path(child["workspace_path"])
    await _create_child(db, git_repo, parent_session_id=child["child_session_id"])

    with pytest.raises(WorkspaceHasDelegatedThreads, match="child"):
        workspace.claim_workspace_deletion(db, target)

    assert child_path.exists()
    assert (
        db.execute(
            "SELECT state FROM workspaces WHERE id = ?",
            (child["workspace_id"],),
        ).fetchone()[0]
        == "ready"
    )


async def test_cleanup_claim_blocks_later_child_reservations(
    db,
    git_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THREAD_MAX_DEPTH", "3")
    from yinshi.config import get_settings

    get_settings.cache_clear()
    child = await _create_child(db, git_repo)
    target = workspace.prepare_workspace_deletion(db, child["workspace_id"])
    workspace.claim_workspace_deletion(db, target)

    delegation_count = db.execute("SELECT COUNT(*) FROM thread_delegations").fetchone()[0]
    with pytest.raises(ThreadNotFoundError):
        await _create_child(db, git_repo, parent_session_id=child["child_session_id"])

    assert Path(child["workspace_path"]).exists()
    assert db.execute("SELECT COUNT(*) FROM thread_delegations").fetchone()[0] == delegation_count


async def test_cleanup_lifecycle_serializes_claim_release_and_retry(db, git_repo) -> None:
    lifecycle = workspace.workspace_deletion_lifecycle
    child = await _create_child(db, git_repo)
    first_target = workspace.prepare_workspace_deletion(db, child["workspace_id"])
    second_target = workspace.prepare_workspace_deletion(db, child["workspace_id"])
    first_entered = asyncio.Event()
    allow_release = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_cleanup() -> None:
        async with lifecycle(first_target):
            workspace.claim_workspace_deletion(db, first_target)
            first_entered.set()
            await allow_release.wait()
            workspace.release_workspace_deletion(db, first_target)

    async def second_cleanup() -> None:
        async with lifecycle(second_target):
            second_entered.set()
            workspace.claim_workspace_deletion(db, second_target)

    first_task = asyncio.create_task(first_cleanup())
    await first_entered.wait()
    second_task = asyncio.create_task(second_cleanup())
    await asyncio.sleep(0.05)
    assert not second_entered.is_set()
    allow_release.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set()


async def test_cleanup_retry_accepts_an_already_absent_owned_ref(db, git_repo) -> None:
    from dataclasses import replace

    child = await _create_child(db, git_repo)
    target = workspace.prepare_workspace_deletion(db, child["workspace_id"])
    assert target.delegation_id is not None
    assert target.snapshot_commit is not None
    snapshot_ref = f"refs/yinshi/snapshots/{target.delegation_id}"
    result_ref = f"refs/yinshi/results/{target.delegation_id}"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "update-ref", snapshot_ref, target.snapshot_commit],
        cwd=git_repo,
        check=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        ["git", "update-ref", result_ref, target.snapshot_commit],
        cwd=git_repo,
        check=True,
    )
    target = replace(
        target,
        snapshot_ref=snapshot_ref,
        result_ref=result_ref,
        result_commit=target.snapshot_commit,
    )
    async with workspace.workspace_deletion_lifecycle(target):
        workspace.claim_workspace_deletion(db, target)
        await asyncio.to_thread(
            subprocess.run,
            ["git", "update-ref", "-d", target.snapshot_ref, target.snapshot_commit],
            cwd=git_repo,
            check=True,
        )
        await workspace.apply_workspace_deletion(target)

    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            f"refs/yinshi/*/{target.delegation_id}",
            cwd=git_repo,
        )
        == ""
    )
