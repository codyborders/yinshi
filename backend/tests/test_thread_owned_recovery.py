"""Owned cleanup preserves selections and makes bounded retry progress."""

import asyncio
import uuid
from pathlib import Path

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from tests.test_thread_provisioning_cancel import _force_pre_attach
from yinshi.models import ThreadChildCreate
from yinshi.services.thread_orchestration import ThreadOrchestrationService


@pytest.mark.parametrize("stale", [False, True])
async def test_selected_recovery_preserves_unselected_owned_artifacts(db, git_repo, stale):
    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    children = [
        await service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Child",
                task="Inspect",
                start_immediately=False,
            ),
        )
        for _ in range(2)
    ]
    paths = [_force_pre_attach(db, child) for child in children]
    db.execute(
        "UPDATE thread_delegations SET status = ?, updated_at = '2000-01-01 00:00:00'",
        ("provisioning" if stale else "cancelled",),
    )
    db.commit()
    before = [tuple(row) for row in db.execute("SELECT * FROM thread_delegations ORDER BY id")]
    await service.reconcile(request, delegation_ids=[])
    assert [
        tuple(row) for row in db.execute("SELECT * FROM thread_delegations ORDER BY id")
    ] == before
    assert all(Path(path).is_dir() for path in paths)
    untouched = tuple(
        db.execute(
            "SELECT * FROM thread_delegations WHERE id = ?", (children[1].delegation_id,)
        ).fetchone()
    )
    await service.reconcile(request, delegation_ids=[children[0].delegation_id])
    assert not Path(paths[0]).exists()
    assert Path(paths[1]).is_dir()
    assert (
        db.execute(
            "SELECT git_artifacts_claimed FROM thread_delegations WHERE id = ?",
            (children[0].delegation_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        tuple(
            db.execute(
                "SELECT * FROM thread_delegations WHERE id = ?", (children[1].delegation_id,)
            ).fetchone()
        )
        == untouched
    )


async def test_retry_progress_passes_a_permanently_failing_page(db, git_repo):
    seed_parent_stack(db, git_repo)
    identifiers = [uuid.uuid4().hex for _ in range(130)]
    for index, identifier in enumerate(identifiers):
        db.execute(
            "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, title, task, requested_model, status, git_artifacts_claimed, git_artifact_namespace, updated_at) "
            "VALUES (?, 'parent-session', ?, 'user', 'Child', 'Inspect', 'model', 'interrupted', 1, ?, '2000-01-01 00:00:00')",
            (identifier, identifier, f"{index:064x}"),
        )
    db.commit()
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    # Missing physical ownership must remain a failure, not permission to adopt.
    await service.reconcile(request)
    assert (
        db.execute(
            "SELECT COUNT(*) FROM thread_delegations WHERE updated_at != '2000-01-01 00:00:00'"
        ).fetchone()[0]
        == 128
    )
    await service.reconcile(request)
    assert (
        db.execute(
            "SELECT COUNT(*) FROM thread_delegations WHERE updated_at = '2000-01-01 00:00:00'"
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM thread_delegations WHERE status = 'interrupted' AND git_artifacts_claimed = 1"
        ).fetchone()[0]
        == 130
    )
    assert not Path(git_repo, ".git", ".yinshi-thread-owner-v1.json").exists()


async def test_cancelling_recovery_while_physical_lock_is_held_retains_ownership(db, git_repo):
    from yinshi.services.repository_lifecycle import repository_lifecycle

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
    path = _force_pre_attach(db, child)
    db.execute(
        "UPDATE thread_delegations SET status = 'cancelled', updated_at = '2000-01-01 00:00:00'"
    )
    db.commit()
    async with repository_lifecycle("yinshi-thread-git", Path(git_repo, ".git").resolve()):
        recovery = asyncio.create_task(service.reconcile(request))
        try:
            async with asyncio.timeout(3):
                while (
                    db.execute("SELECT updated_at FROM thread_delegations").fetchone()[0]
                    == "2000-01-01 00:00:00"
                ):
                    await asyncio.sleep(0.01)
            assert not recovery.done()
            recovery.cancel()
            with pytest.raises(asyncio.CancelledError):
                await recovery
        finally:
            if not recovery.done():
                recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)
    assert Path(path).is_dir()
    assert db.execute("SELECT git_artifacts_claimed FROM thread_delegations").fetchone()[0] == 1
    await ThreadOrchestrationService().reconcile(request)
    assert not Path(path).exists()
    assert tuple(
        db.execute("SELECT status, git_artifacts_claimed FROM thread_delegations").fetchone()
    ) == ("cancelled", 0)
