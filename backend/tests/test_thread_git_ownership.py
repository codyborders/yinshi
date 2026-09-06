"""Check durable ownership of Git artifacts created by child spawning."""

import asyncio
import uuid
from pathlib import Path

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.models import ThreadChildCreate
from yinshi.services.thread_orchestration import ThreadOrchestrationService


@pytest.mark.parametrize("operation", ["cancel", "reconcile"])
async def test_copied_database_cannot_clean_original_database_artifacts(
    db, git_repo, tmp_path, monkeypatch, operation
):
    import sqlite3

    from tests.test_thread_provisioning_cancel import _force_pre_attach
    from yinshi.config import get_settings
    from yinshi.exceptions import YinshiError

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    request = _orchestration_request()
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
    worktree_path = _force_pre_attach(db, child)
    if operation == "reconcile":
        db.execute(
            "UPDATE thread_delegations SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
            (child.delegation_id,),
        )
        db.commit()
    original = tuple(
        db.execute(
            "SELECT * FROM thread_delegations WHERE id = ?", (child.delegation_id,)
        ).fetchone()
    )
    other_path = tmp_path / "copied.db"
    target = sqlite3.connect(other_path)
    try:
        db.backup(target)
    finally:
        target.close()
    monkeypatch.setenv("DB_PATH", str(other_path))
    get_settings.cache_clear()
    try:
        try:
            if operation == "cancel":
                await service.cancel_child(request, thread_id=child.delegation_id)
            else:
                await service.reconcile(request)
        except YinshiError:
            pass
        assert Path(worktree_path).is_dir()
        assert (
            tuple(
                db.execute(
                    "SELECT * FROM thread_delegations WHERE id = ?", (child.delegation_id,)
                ).fetchone()
            )
            == original
        )
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("operation", ["cancel", "reconcile"])
async def test_cancel_preserves_unclaimed_preexisting_git_artifacts(db, git_repo, operation):
    from yinshi.services.thread_workspaces import ThreadWorkspaceService

    seed_parent_stack(db, git_repo)
    identifier = uuid.uuid4().hex
    workspaces = ThreadWorkspaceService()
    context = workspaces.load_parent_context(
        db, None, parent_workspace_id="parent-ws", delegation_id=identifier
    )
    await workspaces.create_child_git_artifacts(context)
    db.execute(
        "INSERT INTO thread_delegations (id, parent_session_id, idempotency_key, initiator, title, task, requested_model, status) VALUES (?, 'parent-session', 'unclaimed', 'user', 'Child', 'Inspect', 'model', 'provisioning')",
        (identifier,),
    )
    db.commit()
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    if operation == "cancel":
        outcome = await service.cancel_child(request, thread_id=identifier)
        assert outcome.status == "cancelled"
    else:
        db.execute(
            "UPDATE thread_delegations SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
            (identifier,),
        )
        db.commit()
        await service.reconcile(request)
        assert (
            db.execute(
                "SELECT status FROM thread_delegations WHERE id = ?", (identifier,)
            ).fetchone()[0]
            == "interrupted"
        )
    assert Path(context.worktree_path).is_dir()
    assert (
        db.execute(
            "SELECT git_artifacts_claimed FROM thread_delegations WHERE id = ?", (identifier,)
        ).fetchone()[0]
        == 0
    )


async def test_other_database_cannot_claim_the_same_physical_repository(
    db, git_repo, tmp_path, monkeypatch
):
    import sqlite3
    import subprocess

    import pytest

    from yinshi.config import get_settings
    from yinshi.exceptions import YinshiError

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    request = _orchestration_request()
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
    original_rows = [
        tuple(row) for row in db.execute("SELECT * FROM thread_delegations ORDER BY id")
    ]

    def git_state():
        return tuple(
            subprocess.run(
                ["git", "-C", git_repo, *arguments], check=True, capture_output=True
            ).stdout
            for arguments in (("worktree", "list", "--porcelain"), ("for-each-ref",))
        )

    original_git = git_state()
    other_path = tmp_path / "other.db"
    target = sqlite3.connect(other_path)
    try:
        db.backup(target)
    finally:
        target.close()
    monkeypatch.setenv("DB_PATH", str(other_path))
    get_settings.cache_clear()
    try:
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
        assert git_state() == original_git
        assert [
            tuple(row) for row in db.execute("SELECT * FROM thread_delegations ORDER BY id")
        ] == original_rows
        assert (
            db.execute(
                "SELECT status FROM thread_delegations WHERE id = ?", (first.delegation_id,)
            ).fetchone()[0]
            == "queued"
        )
    finally:
        get_settings.cache_clear()


async def test_cancel_preserves_artifacts_when_claim_namespace_does_not_match(db, git_repo):
    from tests.test_thread_provisioning_cancel import _force_pre_attach
    from yinshi.exceptions import YinshiError

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    request = _orchestration_request()
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
    worktree_path = _force_pre_attach(db, child)
    db.execute(
        "UPDATE thread_delegations SET git_artifact_namespace = ? WHERE id = ?",
        ("f" * 64, child.delegation_id),
    )
    db.commit()
    try:
        await service.cancel_child(request, thread_id=child.delegation_id)
    except YinshiError:
        pass
    assert Path(worktree_path).is_dir()
    assert (
        db.execute(
            "SELECT git_artifacts_claimed FROM thread_delegations WHERE id = ?",
            (child.delegation_id,),
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("symbolic", [False, True])
async def test_cancel_preserves_competing_snapshot_ref_and_pending_claim(db, git_repo, symbolic):
    import subprocess

    from tests.test_thread_provisioning_cancel import _force_pre_attach
    from yinshi.exceptions import YinshiError

    seed_parent_stack(db, git_repo)
    (Path(git_repo) / "snapshot-input.txt").write_text("Uncommitted parent content\n")
    service = ThreadOrchestrationService()
    request = _orchestration_request()
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
    saved = db.execute(
        "SELECT snapshot_ref, base_commit, base_kind FROM thread_delegations WHERE id = ?",
        (child.delegation_id,),
    ).fetchone()
    assert saved["snapshot_ref"] is not None
    _force_pre_attach(db, child)
    db.execute(
        "UPDATE thread_delegations SET snapshot_ref = ?, base_commit = ?, base_kind = ? WHERE id = ?",
        (*tuple(saved), child.delegation_id),
    )
    db.commit()
    competing_oid = (
        await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", git_repo, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    ).stdout.strip()
    assert competing_oid != saved["base_commit"]
    if symbolic:
        foreign_ref = "refs/heads/foreign-snapshot"
        await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", git_repo, "update-ref", foreign_ref, competing_oid],
            check=True,
            capture_output=True,
        )
        await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", git_repo, "symbolic-ref", saved["snapshot_ref"], foreign_ref],
            check=True,
            capture_output=True,
        )
    else:
        await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", git_repo, "update-ref", saved["snapshot_ref"], competing_oid],
            check=True,
            capture_output=True,
        )
    try:
        await service.cancel_child(request, thread_id=child.delegation_id)
    except YinshiError:
        pass
    remaining_ref = await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", git_repo, "rev-parse", "--verify", saved["snapshot_ref"]],
        check=False,
        capture_output=True,
        text=True,
    )
    assert remaining_ref.returncode == 0
    assert remaining_ref.stdout.strip() == competing_oid
    if symbolic:
        target = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", git_repo, "symbolic-ref", saved["snapshot_ref"]],
            check=False,
            capture_output=True,
            text=True,
        )
        assert target.returncode == 0
        assert target.stdout.strip() == foreign_ref
    assert (
        db.execute(
            "SELECT git_artifacts_claimed FROM thread_delegations WHERE id = ?",
            (child.delegation_id,),
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("damage", ["malformed", "writable", "symlink"])
async def test_spawn_rejects_unsafe_physical_owner_records(db, git_repo, tmp_path, damage):
    from yinshi.exceptions import YinshiError

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    request = _orchestration_request()
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
    record = Path(git_repo, ".git", ".yinshi-thread-owner-v1.json")
    if damage == "malformed":
        record.write_text("{invalid")
    elif damage == "writable":
        record.chmod(0o666)
    else:
        target = tmp_path / "foreign-owner.json"
        target.write_bytes(record.read_bytes())
        record.unlink()
        record.symlink_to(target)
    contents = record.read_bytes()
    before = tuple(
        db.execute(
            "SELECT * FROM thread_delegations WHERE id = ?", (first.delegation_id,)
        ).fetchone()
    )
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
        tuple(
            db.execute(
                "SELECT * FROM thread_delegations WHERE id = ?", (first.delegation_id,)
            ).fetchone()
        )
        == before
    )
    assert record.read_bytes() == contents
    assert record.is_symlink() == (damage == "symlink")
    assert (
        db.execute(
            "SELECT COUNT(*) FROM thread_delegations WHERE git_artifacts_claimed = 1"
        ).fetchone()[0]
        == 1
    )


async def test_spawn_records_durable_artifact_ownership(db, git_repo):
    seed_parent_stack(db, git_repo)
    child = await ThreadOrchestrationService().spawn_child(
        _orchestration_request(),
        parent_session_id="parent-session",
        body=ThreadChildCreate(
            idempotency_key=str(uuid.uuid4()),
            title="Child",
            task="Inspect",
            start_immediately=False,
        ),
    )
    row = db.execute(
        "SELECT d.*, w.path AS workspace_path FROM thread_delegations d "
        "JOIN sessions s ON s.id = d.child_session_id "
        "JOIN workspaces w ON w.id = s.workspace_id WHERE d.id = ?",
        (child.delegation_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == "queued"
    assert Path(row["workspace_path"]).is_dir()
    assert row["git_artifacts_claimed"] == 1
    assert len(row["git_artifact_namespace"]) == 64
