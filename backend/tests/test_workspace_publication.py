"""Private checkout publication never exposes an unmarked partial target.

Tests exercise the platform no-replace primitive and linked-worktree backlink
rewrites against real filesystem and Git metadata.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from yinshi.services.workspace import (
    WorkspaceCheckoutPreparation,
    apply_workspace_checkout_preparation,
    load_workspace_checkout_state,
)
from yinshi.services.workspace_publication import (
    WorkspaceCheckoutPublication,
    WorkspacePublicationCollisionError,
    WorkspacePublicationError,
    atomic_rename_no_replace,
    publish_workspace_checkout_for_tenant,
    rewrite_linked_worktree_backlinks,
)
from yinshi.tenant import TenantContext


@pytest.fixture(autouse=True)
def _workspace_publication_schema(db: sqlite3.Connection) -> None:
    """Provide schemas that remain intentionally absent from production startup."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS workspace_checkout_repairs ("
        "operation_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, repo_id TEXT NOT NULL, "
        "authority_hash TEXT NOT NULL, selection_hash TEXT NOT NULL, owner_token TEXT NOT NULL, "
        "execution_operation_id TEXT, database_identity TEXT NOT NULL, tenant_id TEXT NOT NULL, "
        "runtime_id TEXT, state TEXT NOT NULL, source_repo_path TEXT NOT NULL, "
        "final_repo_path TEXT NOT NULL, remote_url TEXT, installation_id INTEGER, "
        "workspace_paths_json TEXT, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS workspace_checkout_publications ("
        "operation_id TEXT NOT NULL, generation INTEGER NOT NULL, object_kind TEXT NOT NULL, "
        "workspace_id TEXT, state TEXT NOT NULL, staging_path TEXT NOT NULL, "
        "final_path TEXT NOT NULL, owner_token TEXT NOT NULL, staging_identity_json TEXT, "
        "final_identity_json TEXT, staging_parent_identity_json TEXT NOT NULL, "
        "final_parent_identity_json TEXT NOT NULL, marker_directory TEXT, marker_key TEXT, "
        "marker_authority_hash TEXT, marker_operation_id TEXT, marker_physical_identity_json TEXT, "
        "backlink_json TEXT NOT NULL DEFAULT '{}', backlinks_validated INTEGER NOT NULL DEFAULT 0, "
        "rename_started INTEGER NOT NULL DEFAULT 0, rename_completed INTEGER NOT NULL DEFAULT 0, "
        "directory_synced INTEGER NOT NULL DEFAULT 0, cleanup_state TEXT NOT NULL DEFAULT 'pending', "
        "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (operation_id, generation))"
    )
    db.commit()


def _run_git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_atomic_no_replace_publication_preserves_competing_target(tmp_path: Path) -> None:
    """A no-replace loser must preserve both its stage and the competing winner."""
    staging_path = tmp_path / ".private-stage"
    final_path = tmp_path / "checkout"
    staging_path.mkdir()
    final_path.mkdir()
    (staging_path / "owner").write_text("ours", encoding="utf-8")
    (final_path / "owner").write_text("winner", encoding="utf-8")

    with pytest.raises(WorkspacePublicationCollisionError):
        atomic_rename_no_replace(staging_path, final_path)

    assert (staging_path / "owner").read_text(encoding="utf-8") == "ours"
    assert (final_path / "owner").read_text(encoding="utf-8") == "winner"


def test_atomic_no_replace_publication_keeps_directory_identity(tmp_path: Path) -> None:
    """Successful publication must preserve the staged directory device and inode."""
    staging_path = tmp_path / ".private-stage"
    final_path = tmp_path / "checkout"
    staging_path.mkdir()
    staging_identity = os.lstat(staging_path)

    atomic_rename_no_replace(staging_path, final_path)

    final_identity = os.lstat(final_path)
    assert not staging_path.exists()
    assert (final_identity.st_dev, final_identity.st_ino) == (
        staging_identity.st_dev,
        staging_identity.st_ino,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_after_rename", [False, True])
async def test_repaired_repository_recovers_private_marker_before_publication(
    db,
    git_repo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_after_rename: bool,
) -> None:
    """Restart recovery must retain private state and publish its exact held marker."""
    from yinshi.services import workspace_publication

    source_repo = Path(git_repo)
    source_workspace = source_repo / ".worktrees" / "feature"
    _run_git("worktree", "add", "-b", "feature", str(source_workspace), cwd=source_repo)
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo-private', 'repo', ?)",
        (str(source_repo),),
    )
    db.execute(
        "INSERT INTO workspaces (id, repo_id, name, branch, path) "
        "VALUES ('workspace-private', 'repo-private', 'feature', 'feature', ?)",
        (str(source_workspace),),
    )
    db.commit()
    tenant_root = tmp_path / "tenant"
    tenant_root.mkdir(mode=0o700)
    tenant = TenantContext(
        user_id="tenant-private",
        email="tenant@example.com",
        data_dir=str(tenant_root),
        db_path=str(tmp_path / "tenant.db"),
    )
    state = load_workspace_checkout_state(db, "workspace-private")

    async def run_database(operation):
        return operation(db)

    def authorize(database):
        return load_workspace_checkout_state(database, "workspace-private")

    monkeypatch.setattr(workspace_publication, "_tenant_path_is_trusted", lambda *_args: False)
    original_rename = workspace_publication.atomic_rename_no_replace
    original_synchronization = workspace_publication._require_git_worktree_synchronization
    observed_publication: list[bool] = []
    synchronized_publications: list[tuple[Path, ...]] = []

    async def record_synchronization(
        repo_path: Path,
        workspaces: tuple[Path, ...],
        **kwargs,
    ) -> None:
        synchronized_publications.append(workspaces)
        await original_synchronization(repo_path, workspaces, **kwargs)

    def observe_marker_before_rename(source: Path, target: Path, **kwargs) -> None:
        marker_directory = source / ".git" / ".yinshi-workspace-admission-v1"
        assert marker_directory.is_dir()
        assert list(marker_directory.glob("*.json"))
        assert not target.exists()
        observed_publication.append(True)
        if len(observed_publication) == 1:
            if interrupt_after_rename:
                original_rename(source, target, **kwargs)
            raise OSError("injected publication interruption")
        original_rename(source, target, **kwargs)

    monkeypatch.setattr(
        workspace_publication,
        "atomic_rename_no_replace",
        observe_marker_before_rename,
    )
    monkeypatch.setattr(
        workspace_publication,
        "_require_git_worktree_synchronization",
        record_synchronization,
    )
    with pytest.raises(OSError, match="publication interruption"):
        await publish_workspace_checkout_for_tenant(
            tenant,
            state,
            run_database_operation=run_database,
            authorize=authorize,
            authority_hash="a" * 64,
            database_identity=str(tmp_path / "tenant.db"),
        )
    assert (tenant_root / "repos" / "repo-private").exists() is interrupt_after_rename
    assert (
        db.execute("SELECT state FROM workspace_checkout_publications").fetchone()[0]
        == "publishing"
    )

    if interrupt_after_rename:
        original_parent_synchronization = workspace_publication._sync_publication_parents

        def fail_parent_synchronization(*_args, **_kwargs) -> None:
            raise OSError("injected parent synchronization failure")

        monkeypatch.setattr(
            workspace_publication,
            "_sync_publication_parents",
            fail_parent_synchronization,
        )
        with pytest.raises(OSError, match="parent synchronization failure"):
            await publish_workspace_checkout_for_tenant(
                tenant,
                state,
                run_database_operation=run_database,
                authorize=authorize,
                authority_hash="a" * 64,
                database_identity=str(tmp_path / "tenant.db"),
            )
        interrupted_receipt = db.execute(
            "SELECT state, rename_completed, directory_synced FROM workspace_checkout_publications"
        ).fetchone()
        assert tuple(interrupted_receipt) == ("publishing", 1, 0)
        monkeypatch.setattr(
            workspace_publication,
            "_sync_publication_parents",
            original_parent_synchronization,
        )

    published = await publish_workspace_checkout_for_tenant(
        tenant,
        state,
        run_database_operation=run_database,
        authorize=authorize,
        authority_hash="a" * 64,
        database_identity=str(tmp_path / "tenant.db"),
    )

    assert isinstance(published, WorkspaceCheckoutPublication)
    expected_observations = [True] if interrupt_after_rename else [True, True]
    assert observed_publication == expected_observations
    final_repo = tenant_root / "repos" / "repo-private"
    final_workspace = final_repo / ".worktrees" / "feature"
    assert final_repo.is_dir()
    assert final_workspace.is_dir()
    assert published.selected_lease.common == final_repo / ".git"
    marker = (
        final_repo
        / ".git"
        / ".yinshi-workspace-admission-v1"
        / f"{published.selected_lease.key}.json"
    )
    assert marker.is_file()
    git_file = (final_workspace / ".git").read_text(encoding="utf-8")
    registration = Path(git_file.removeprefix("gitdir: ").strip())
    assert registration.is_relative_to(final_repo / ".git" / "worktrees")
    assert (registration / "gitdir").read_text(encoding="utf-8") == (
        f"{final_workspace / '.git'}\n"
    )
    receipt = db.execute(
        "SELECT state, rename_started, rename_completed, directory_synced, "
        "backlinks_validated FROM workspace_checkout_publications "
        "WHERE operation_id = ?",
        (published.operation_id,),
    ).fetchone()
    assert tuple(receipt) == ("published", 1, 1, 1, 1)
    assert synchronized_publications == [(final_workspace,)]

    apply_workspace_checkout_preparation(db, published.preparation)
    published.selected_lease.close()
    rebound_state = load_workspace_checkout_state(db, "workspace-private")
    rebound = await publish_workspace_checkout_for_tenant(
        tenant,
        rebound_state,
        run_database_operation=run_database,
        authorize=authorize,
        authority_hash="a" * 64,
        database_identity=str(tmp_path / "tenant.db"),
    )
    assert isinstance(rebound, WorkspaceCheckoutPublication)
    await rebound.complete(
        run_database_operation=run_database,
        authorize=authorize,
        release_marker=True,
    )
    assert not marker.exists()
    assert (
        db.execute(
            "SELECT state FROM workspace_checkout_repairs WHERE operation_id = ?",
            (published.operation_id,),
        ).fetchone()[0]
        == "bound"
    )


@pytest.mark.asyncio
async def test_existing_repository_publishes_missing_selected_worktree_privately(
    db, git_repo: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing worktree must sync its private checkout and Git registration."""
    from yinshi.services import workspace_publication

    source_repo = Path(git_repo)
    _run_git("branch", "feature-missing", cwd=source_repo)
    tenant_root = tmp_path / "tenant-existing"
    tenant_root.mkdir(mode=0o700)
    final_repo = tenant_root / "repos" / "repo-existing"
    final_repo.parent.mkdir(mode=0o700)
    _run_git("clone", "--no-hardlinks", str(source_repo), str(final_repo), cwd=tmp_path)
    final_workspace = final_repo / ".worktrees" / "feature-missing"
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo-existing', 'repo', ?)",
        (str(final_repo),),
    )
    db.execute(
        "INSERT INTO workspaces (id, repo_id, name, branch, path) "
        "VALUES ('workspace-existing', 'repo-existing', 'feature', 'feature-missing', ?)",
        (str(final_workspace),),
    )
    db.commit()
    tenant = TenantContext(
        user_id="tenant-existing",
        email="tenant@example.com",
        data_dir=str(tenant_root),
        db_path=str(tmp_path / "tenant-existing.db"),
    )
    state = load_workspace_checkout_state(db, "workspace-existing")

    async def run_database(operation):
        return operation(db)

    synchronized: list[Path] = []
    git_commands: list[list[str]] = []
    original_sync = workspace_publication._sync_private_tree
    original_git = workspace_publication._run_git

    async def record_git(arguments, **kwargs):
        git_commands.append(arguments)
        return await original_git(arguments, **kwargs)

    def record_sync(path: Path, **kwargs) -> None:
        synchronized.append(path)
        original_sync(path, **kwargs)

    monkeypatch.setattr(workspace_publication, "_sync_private_tree", record_sync)
    monkeypatch.setattr(workspace_publication, "_run_git", record_git)
    published = await publish_workspace_checkout_for_tenant(
        tenant,
        state,
        run_database_operation=run_database,
        authorize=lambda database: load_workspace_checkout_state(database, "workspace-existing"),
        authority_hash="b" * 64,
        database_identity="tenant:existing",
    )

    assert isinstance(published, WorkspaceCheckoutPublication)
    assert final_workspace.is_dir()
    assert any(path.parent.parent == final_repo / ".git" for path in synchronized)
    assert ["worktree", "list", "--porcelain"] in git_commands
    assert published.preparation.workspace_paths == (("workspace-existing", str(final_workspace)),)
    object_row = db.execute(
        "SELECT object_kind, workspace_id, state FROM workspace_checkout_publications "
        "WHERE operation_id = ?",
        (published.operation_id,),
    ).fetchone()
    assert tuple(object_row) == ("worktree", "workspace-existing", "published")
    apply_workspace_checkout_preparation(db, published.preparation)
    published.selected_lease.close()
    rebound = await publish_workspace_checkout_for_tenant(
        tenant,
        load_workspace_checkout_state(db, "workspace-existing"),
        run_database_operation=run_database,
        authorize=lambda database: load_workspace_checkout_state(database, "workspace-existing"),
        authority_hash="b" * 64,
        database_identity="tenant:existing",
    )
    assert isinstance(rebound, WorkspaceCheckoutPublication)
    await rebound.complete(
        run_database_operation=run_database,
        authorize=lambda database: load_workspace_checkout_state(database, "workspace-existing"),
        release_marker=True,
    )


@pytest.mark.asyncio
async def test_publication_owner_closes_untransferred_marker_when_apply_fails(
    db, git_repo: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-execution database failure must unlock, but preserve, its durable marker."""
    from types import SimpleNamespace

    from yinshi.services import workspace_publication

    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo-owner-fail', 'repo', ?)",
        (git_repo,),
    )
    db.execute(
        "INSERT INTO workspaces (id, repo_id, name, branch, path) "
        "VALUES ('workspace-owner-fail', 'repo-owner-fail', 'main', 'main', ?)",
        (git_repo,),
    )
    db.commit()
    state = load_workspace_checkout_state(db, "workspace-owner-fail")
    preparation = WorkspaceCheckoutPreparation(
        workspace_id=state.workspace_id,
        repo_id=state.repo_id,
        repo_path=state.repo_path,
        remote_url=state.remote_url,
        installation_id=state.installation_id,
        workspace_paths=state.workspace_paths,
        update_repo_metadata=False,
        repaired_repo=False,
    )
    closed: list[bool] = []
    publication = WorkspaceCheckoutPublication(
        preparation=preparation,
        selected_lease=SimpleNamespace(close=lambda: closed.append(True)),
        selected_identity="identity",
        operation_id="a" * 32,
        owner_token="b" * 64,
        final_repo_identity="repo-identity",
    )

    async def publish(*_args, **_kwargs):
        return publication

    async def run_database(operation):
        return operation(db)

    monkeypatch.setattr(workspace_publication, "publish_workspace_checkout_for_tenant", publish)
    with pytest.raises(RuntimeError, match="apply failed"):
        await workspace_publication.publish_apply_workspace_checkout_for_tenant(
            TenantContext("tenant", "tenant@example.com", str(tmp_path), str(tmp_path / "db")),
            state,
            run_database_operation=run_database,
            authorize=lambda _database: state,
            authority_hash="c" * 64,
            database_identity="db",
            apply=lambda _database, _preparation: (_ for _ in ()).throw(
                RuntimeError("apply failed")
            ),
        )

    assert closed == [True]


@pytest.mark.asyncio
async def test_publication_owner_applies_and_completes_repair_in_one_call(
    db, git_repo: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-execution callers delegate the complete repair lifecycle to one owner."""
    from yinshi.services import workspace_publication

    source_repo = Path(git_repo)
    source_workspace = source_repo / ".worktrees" / "owner-api"
    _run_git("worktree", "add", "-b", "owner-api", str(source_workspace), cwd=source_repo)
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo-owner-api', 'repo', ?)",
        (str(source_repo),),
    )
    db.execute(
        "INSERT INTO workspaces (id, repo_id, name, branch, path) "
        "VALUES ('workspace-owner-api', 'repo-owner-api', 'feature', 'owner-api', ?)",
        (str(source_workspace),),
    )
    db.commit()
    tenant_root = tmp_path / "tenant-owner-api"
    tenant_root.mkdir(mode=0o700)
    tenant = TenantContext(
        user_id="tenant-owner-api",
        email="tenant@example.com",
        data_dir=str(tenant_root),
        db_path=str(tmp_path / "tenant-owner-api.db"),
    )
    state = load_workspace_checkout_state(db, "workspace-owner-api")

    async def run_database(operation):
        return operation(db)

    monkeypatch.setattr(workspace_publication, "_tenant_path_is_trusted", lambda *_args: False)
    preparation, result = await workspace_publication.publish_apply_workspace_checkout_for_tenant(
        tenant,
        state,
        run_database_operation=run_database,
        authorize=lambda database: load_workspace_checkout_state(database, "workspace-owner-api"),
        authority_hash="f" * 64,
        database_identity="tenant:owner-api",
        apply=lambda database, value: apply_workspace_checkout_preparation(database, value),
    )

    assert result["path"] == dict(preparation.workspace_paths)["workspace-owner-api"]
    assert db.execute("SELECT state FROM workspace_checkout_repairs").fetchone()[0] == "bound"


@pytest.mark.asyncio
async def test_absent_managed_repository_uses_logical_repair_without_execution_identity(
    db, git_repo: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent source gets no physical receipt before repository publication completes."""
    from yinshi.services import workspace_publication

    tenant_root = tmp_path / "tenant-absent"
    tenant_root.mkdir(mode=0o700)
    tenant = TenantContext(
        user_id="tenant-absent",
        email="tenant@example.com",
        data_dir=str(tenant_root),
        db_path=str(tmp_path / "tenant-absent.db"),
    )
    final_repo = tenant_root / "repos" / "repo-absent"
    final_workspace = final_repo / ".worktrees" / "feature-absent"
    db.execute(
        "INSERT INTO repos (id, name, root_path, remote_url) "
        "VALUES ('repo-absent', 'repo', ?, 'https://github.com/acme/repo')",
        (str(final_repo),),
    )
    db.execute(
        "INSERT INTO workspaces (id, repo_id, name, branch, path) "
        "VALUES ('workspace-absent', 'repo-absent', 'feature', 'feature-absent', ?)",
        (str(final_workspace),),
    )
    db.commit()
    state = load_workspace_checkout_state(db, "workspace-absent")

    async def run_database(operation):
        return operation(db)

    async def materialize(_source, target, _remote, access_token=None, staging_identity=None):
        del access_token, staging_identity
        _run_git("clone", "--no-hardlinks", git_repo, ".", cwd=Path(target))
        _run_git("branch", "feature-absent", cwd=Path(target))

    monkeypatch.setattr(workspace_publication, "_materialize_owned_repo_checkout", materialize)
    publication = await publish_workspace_checkout_for_tenant(
        tenant,
        state,
        run_database_operation=run_database,
        authorize=lambda database: load_workspace_checkout_state(database, "workspace-absent"),
        authority_hash="e" * 64,
        database_identity="tenant:absent",
    )

    assert isinstance(publication, WorkspaceCheckoutPublication)
    assert (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'workspace_execution_receipts'"
        ).fetchone()
        is None
    )
    assert (
        db.execute("SELECT execution_operation_id FROM workspace_checkout_repairs").fetchone()[0]
        is None
    )
    await publication.selected_lease.release()


@pytest.mark.asyncio
async def test_repair_cannot_start_after_workspace_deletion_claim(
    db, git_repo: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deletion ownership must exclude a later terminal or file repair."""
    from yinshi.services import workspace_publication

    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo-deleting', 'repo', ?)",
        (git_repo,),
    )
    db.execute(
        "INSERT INTO workspaces (id, repo_id, name, branch, path, state) "
        "VALUES ('workspace-deleting', 'repo-deleting', 'main', 'main', ?, 'deleting')",
        (git_repo,),
    )
    db.execute(
        "CREATE TABLE workspace_deletions ("
        "operation_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, repo_id TEXT NOT NULL, "
        "repo_path TEXT NOT NULL, workspace_path TEXT NOT NULL, branch TEXT NOT NULL, "
        "authority_hash TEXT NOT NULL, state TEXT NOT NULL)"
    )
    db.execute(
        "INSERT INTO workspace_deletions "
        "(operation_id, workspace_id, repo_id, repo_path, workspace_path, branch, "
        "authority_hash, state) VALUES (?, 'workspace-deleting', 'repo-deleting', ?, ?, "
        "'main', ?, 'claiming')",
        ("d" * 32, git_repo, git_repo, "d" * 64),
    )
    db.commit()
    tenant_root = tmp_path / "tenant-deleting"
    tenant_root.mkdir(mode=0o700)
    tenant = TenantContext(
        user_id="tenant-deleting",
        email="tenant@example.com",
        data_dir=str(tenant_root),
        db_path=str(tmp_path / "tenant-deleting.db"),
    )
    state = load_workspace_checkout_state(db, "workspace-deleting")

    async def run_database(operation):
        return operation(db)

    monkeypatch.setattr(workspace_publication, "_tenant_path_is_trusted", lambda *_args: False)
    with pytest.raises(WorkspacePublicationError, match="deletion"):
        await publish_workspace_checkout_for_tenant(
            tenant,
            state,
            run_database_operation=run_database,
            authorize=lambda database: load_workspace_checkout_state(
                database, "workspace-deleting"
            ),
            authority_hash="d" * 64,
            database_identity="tenant:deleting",
        )

    assert list((tenant_root / "repos").glob(".yinshi-publication-*")) == []


@pytest.mark.asyncio
async def test_owned_repository_materializer_does_not_adopt_through_general_helper(
    git_repo: str, tmp_path: Path
) -> None:
    """Publication materialization must use its exact owned empty stage directly."""
    from yinshi.services import workspace_publication

    target = tmp_path / "owned-stage"
    target.mkdir(mode=0o700)
    identity = workspace_publication._directory_identity_json(target)

    await workspace_publication._materialize_owned_repo_checkout(
        git_repo,
        str(target),
        None,
        staging_identity=identity,
        access_token=None,
    )
    assert (target / ".git").is_dir()


@pytest.mark.asyncio
async def test_owned_repository_materializer_clones_remote_without_general_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent source must clone its authorized remote into the exact owned stage."""
    from yinshi.services import workspace_publication

    target = tmp_path / "owned-remote-stage"
    target.mkdir(mode=0o700)
    identity = workspace_publication._directory_identity_json(target)
    calls: list[tuple[list[str], str | None]] = []

    async def invalid_repo(_path):
        return False

    async def run_git(arguments, cwd=None, env=None):
        del env
        calls.append((arguments, cwd))
        (target / ".git").mkdir()
        return ""

    monkeypatch.setattr(workspace_publication, "validate_local_repo", invalid_repo)
    monkeypatch.setattr(workspace_publication, "_run_git", run_git)
    await workspace_publication._materialize_owned_repo_checkout(
        str(tmp_path / "missing-source"),
        str(target),
        "https://github.com/acme/repo",
        staging_identity=identity,
        access_token="secret-token",
    )

    assert calls == [(["clone", "https://github.com/acme/repo", "."], str(target))]


@pytest.mark.asyncio
async def test_repository_materialization_rejects_replaced_private_stage(
    db, git_repo: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign stage replacement must remain untouched and never become public."""
    from yinshi.services import workspace_publication

    source_repo = Path(git_repo)
    source_workspace = source_repo / ".worktrees" / "foreign-stage"
    _run_git("worktree", "add", "-b", "foreign-stage", str(source_workspace), cwd=source_repo)
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES ('repo-stage', 'repo', ?)",
        (str(source_repo),),
    )
    db.execute(
        "INSERT INTO workspaces (id, repo_id, name, branch, path) "
        "VALUES ('workspace-stage', 'repo-stage', 'feature', 'foreign-stage', ?)",
        (str(source_workspace),),
    )
    db.commit()
    tenant_root = tmp_path / "tenant-stage"
    tenant_root.mkdir(mode=0o700)
    tenant = TenantContext(
        user_id="tenant-stage",
        email="tenant@example.com",
        data_dir=str(tenant_root),
        db_path=str(tmp_path / "tenant-stage.db"),
    )
    state = load_workspace_checkout_state(db, "workspace-stage")

    async def run_database(operation):
        return operation(db)

    original = workspace_publication._materialize_owned_repo_checkout
    foreign = tmp_path / "foreign"
    _run_git("clone", "--no-hardlinks", str(source_repo), str(foreign), cwd=tmp_path)
    sentinel = foreign / "foreign-sentinel"
    sentinel.write_text("foreign\n", encoding="utf-8")

    replaced_targets: list[Path] = []

    async def replace_stage(source_path, target_path, remote_url, access_token=None, **kwargs):
        target = Path(target_path)
        if target.exists():
            target.rmdir()
        os.rename(foreign, target)
        replaced_targets.append(target)
        await original(
            source_path,
            target_path,
            remote_url,
            access_token=access_token,
            **kwargs,
        )

    monkeypatch.setattr(workspace_publication, "_materialize_owned_repo_checkout", replace_stage)
    monkeypatch.setattr(workspace_publication, "_tenant_path_is_trusted", lambda *_args: False)

    with pytest.raises(WorkspacePublicationError):
        await publish_workspace_checkout_for_tenant(
            tenant,
            state,
            run_database_operation=run_database,
            authorize=lambda database: load_workspace_checkout_state(database, "workspace-stage"),
            authority_hash="c" * 64,
            database_identity="tenant:stage",
        )

    assert (replaced_targets[0] / sentinel.name).read_text(encoding="utf-8") == "foreign\n"
    assert not (tenant_root / "repos" / "repo-stage").exists()


def test_private_tree_sync_rejects_a_missing_root(tmp_path: Path) -> None:
    """A missing recovered object cannot receive a durability acknowledgment."""
    from yinshi.services import workspace_publication

    with pytest.raises(WorkspacePublicationError, match="unavailable"):
        workspace_publication._sync_private_tree(tmp_path / "missing")


def test_recovered_publication_rejects_changed_parent_identity(tmp_path: Path) -> None:
    """Recovery cannot synchronize a replacement entry-owning parent."""
    from yinshi.services import workspace_publication

    staging_parent = tmp_path / "staging-parent"
    final_parent = tmp_path / "final-parent"
    staging_parent.mkdir()
    final_parent.mkdir()
    staging_identity = workspace_publication._directory_identity_json(staging_parent)
    final_identity = workspace_publication._directory_identity_json(final_parent)
    replaced_parent = tmp_path / "replaced-parent"
    final_parent.rename(replaced_parent)
    final_parent.mkdir()
    sentinel = final_parent / "foreign"
    sentinel.write_text("keep\n", encoding="utf-8")

    with pytest.raises(WorkspacePublicationError, match="parent changed"):
        workspace_publication._sync_publication_parents(
            staging_parent / "stage",
            final_parent / "final",
            staging_parent_identity=staging_identity,
            final_parent_identity=final_identity,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_backlinks_name_final_paths_before_publication(tmp_path: Path) -> None:
    """Both linked-worktree path files must name final paths while still private."""
    staging_repo = tmp_path / ".private-repo"
    final_repo = tmp_path / "repo"
    staging_worktree = staging_repo / ".worktrees" / "private-branch"
    final_worktree = final_repo / ".worktrees" / "feature"
    staging_repo.mkdir()
    _run_git("init", "-b", "main", cwd=staging_repo)
    _run_git("config", "user.email", "test@example.com", cwd=staging_repo)
    _run_git("config", "user.name", "Test", cwd=staging_repo)
    (staging_repo / "README.md").write_text("test\n", encoding="utf-8")
    _run_git("add", "README.md", cwd=staging_repo)
    _run_git("commit", "-m", "initial", cwd=staging_repo)
    _run_git("worktree", "add", "-b", "feature", str(staging_worktree), cwd=staging_repo)

    backlink_json = rewrite_linked_worktree_backlinks(
        staging_repo_path=staging_repo,
        staging_workspace_path=staging_worktree,
        final_repo_path=final_repo,
        final_workspace_path=final_worktree,
    )

    backlink = json.loads(backlink_json)
    registration_path = Path(backlink["registration_path_staging"])
    assert (staging_worktree / ".git").read_text(encoding="utf-8") == (
        f"gitdir: {backlink['registration_path_final']}\n"
    )
    assert (registration_path / "gitdir").read_text(encoding="utf-8") == (
        f"{final_worktree / '.git'}\n"
    )
    assert backlink["workspace_git_final"] == str(final_worktree / ".git")


def test_private_tree_sync_rejects_replacement_before_effect(tmp_path: Path) -> None:
    """Synchronization must not adopt a replacement for the accepted private tree."""
    from yinshi.services import workspace_publication

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    expected = workspace_publication._directory_identity_json(checkout)
    accepted = tmp_path / "accepted-checkout"
    checkout.rename(accepted)
    checkout.mkdir()
    foreign = checkout / "foreign"
    foreign.write_text("keep\n", encoding="utf-8")

    with pytest.raises(WorkspacePublicationError, match="changed"):
        workspace_publication._sync_private_tree(checkout, expected_identity=expected)

    assert foreign.read_text(encoding="utf-8") == "keep\n"


def test_owned_chmod_rejects_replacement_before_effect(tmp_path: Path) -> None:
    """Permission repair must not chmod a replacement for the accepted stage."""
    from yinshi.services import workspace_publication

    checkout = tmp_path / "checkout"
    checkout.mkdir(mode=0o700)
    expected = workspace_publication._directory_identity_json(checkout)
    accepted = tmp_path / "accepted-checkout"
    checkout.rename(accepted)
    checkout.mkdir(mode=0o755)

    with pytest.raises(WorkspacePublicationError, match="changed"):
        workspace_publication._chmod_owned_directory(
            checkout,
            0o700,
            expected_identity=expected,
        )

    assert stat.S_IMODE(checkout.stat().st_mode) == 0o755


def test_atomic_publication_rejects_replaced_parent_identity(tmp_path: Path) -> None:
    """Publication must not rename within a replacement entry-owning parent."""
    from yinshi.services import workspace_publication

    parent = tmp_path / "publication-parent"
    parent.mkdir()
    staging = parent / ".stage"
    staging.mkdir()
    parent_identity = workspace_publication._directory_identity_json(parent)
    accepted_parent = tmp_path / "accepted-parent"
    parent.rename(accepted_parent)
    parent.mkdir()
    foreign_stage = parent / ".stage"
    foreign_stage.mkdir()
    (foreign_stage / "foreign").write_text("keep\n", encoding="utf-8")

    with pytest.raises(WorkspacePublicationError, match="parent changed"):
        atomic_rename_no_replace(
            staging,
            parent / "final",
            source_parent_identity=parent_identity,
            target_parent_identity=parent_identity,
        )

    assert (foreign_stage / "foreign").read_text(encoding="utf-8") == "keep\n"
    assert not (parent / "final").exists()


def test_backlink_rewrite_rejects_replaced_repository_root(tmp_path: Path) -> None:
    """Backlink writes must not adopt a replacement for the accepted repository."""
    from yinshi.services import workspace_publication

    repo = tmp_path / "repo"
    repo.mkdir()
    expected = workspace_publication._directory_identity_json(repo)
    accepted = tmp_path / "accepted-repo"
    repo.rename(accepted)
    repo.mkdir()
    sentinel = repo / "foreign"
    sentinel.write_text("keep\n", encoding="utf-8")

    with pytest.raises(WorkspacePublicationError, match="changed"):
        rewrite_linked_worktree_backlinks(
            staging_repo_path=repo,
            staging_workspace_path=repo / ".worktrees" / "feature",
            final_repo_path=tmp_path / "final-repo",
            final_workspace_path=tmp_path / "final-repo" / ".worktrees" / "feature",
            staging_repo_identity=expected,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_registration_sync_rejects_replaced_registration(tmp_path: Path) -> None:
    """Durability sync must not traverse a replacement worktree registration."""
    from yinshi.services import workspace_publication

    registration = tmp_path / "repo" / ".git" / "worktrees" / "feature"
    registration.mkdir(parents=True)
    identity = workspace_publication._directory_identity_json(registration)
    accepted = tmp_path / "accepted-registration"
    registration.rename(accepted)
    registration.mkdir()
    sentinel = registration / "foreign"
    sentinel.write_text("keep\n", encoding="utf-8")
    payload = json.dumps(
        {
            "registration_path_staging": str(registration),
            "registration_identity": identity,
        }
    )

    with pytest.raises(WorkspacePublicationError, match="changed"):
        workspace_publication._sync_worktree_registration(payload)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_atomic_publication_requires_exclusive_parent_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publication must fail before rename when either parent namespace is shared."""
    from yinshi.services import workspace_publication

    parent = tmp_path / "publication-parent"
    parent.mkdir(mode=0o700)
    staging = parent / ".stage"
    staging.mkdir(mode=0o700)
    parent.chmod(0o733)
    called = False

    def forbidden_rename(*_args, **_kwargs) -> None:
        nonlocal called
        called = True
        raise AssertionError("native rename reached")

    monkeypatch.setattr(workspace_publication.sys, "platform", "linux")
    monkeypatch.setattr(workspace_publication, "require_atomic_no_replace_support", lambda: None)
    monkeypatch.setattr(workspace_publication, "_linux_rename_no_replace", forbidden_rename)

    with pytest.raises(WorkspacePublicationError, match="parent changed"):
        atomic_rename_no_replace(staging, parent / "final")

    assert called is False
    assert staging.is_dir()
    assert not (parent / "final").exists()


def test_atomic_publication_uses_retained_parent_at_native_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement source namespace must never redirect native publication."""
    from yinshi.services import workspace_publication

    source_parent = tmp_path / "source"
    target_parent = tmp_path / "target"
    source_parent.mkdir(mode=0o700)
    target_parent.mkdir(mode=0o700)
    staging = source_parent / ".stage"
    staging.mkdir(mode=0o700)
    (staging / "owner").write_text("ours\n", encoding="utf-8")
    moved_parent = tmp_path / "accepted-source"

    def replace_parent_then_rename(
        source_descriptor, source_name, target_descriptor, target_name
    ) -> None:
        source_parent.rename(moved_parent)
        source_parent.mkdir(mode=0o700)
        foreign = source_parent / ".stage"
        foreign.mkdir(mode=0o700)
        (foreign / "owner").write_text("foreign\n", encoding="utf-8")
        os.rename(
            source_name,
            target_name,
            src_dir_fd=source_descriptor,
            dst_dir_fd=target_descriptor,
        )

    monkeypatch.setattr(workspace_publication.sys, "platform", "linux")
    monkeypatch.setattr(workspace_publication, "require_atomic_no_replace_support", lambda: None)
    monkeypatch.setattr(
        workspace_publication,
        "_linux_rename_no_replace",
        replace_parent_then_rename,
    )

    atomic_rename_no_replace(staging, target_parent / "final")

    assert (target_parent / "final" / "owner").read_text(encoding="utf-8") == "ours\n"
    assert (source_parent / ".stage" / "owner").read_text(encoding="utf-8") == "foreign\n"


def test_private_stage_retains_parent_and_stage_during_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage creation must stay in its retained parent after a name replacement."""
    from yinshi.services import workspace_publication

    parent = tmp_path / "publication-parent"
    parent.mkdir(mode=0o700)
    parent_identity = workspace_publication._directory_identity_json(parent)
    stage = parent / ".stage"
    moved_parent = tmp_path / "accepted-parent"
    real_mkdir = workspace_publication.os.mkdir

    def replace_parent_after_mkdir(name, mode=0o777, *, dir_fd=None):
        real_mkdir(name, mode=mode, dir_fd=dir_fd)
        if name == os.fsencode(stage.name):
            parent.rename(moved_parent)
            parent.mkdir(mode=0o700)

    monkeypatch.setattr(workspace_publication.os, "mkdir", replace_parent_after_mkdir)

    retained = workspace_publication._create_private_stage(
        stage,
        parent_identity=parent_identity,
    )
    try:
        observed = os.fstat(retained.directory.descriptor)
        staged = (moved_parent / stage.name).stat()
        assert (observed.st_dev, observed.st_ino) == (staged.st_dev, staged.st_ino)
        assert not (parent / stage.name).exists()
    finally:
        retained.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_name", [".worktree-stage", ".repository-stage"])
async def test_cancelled_private_stage_creation_closes_and_removes_stage(
    tmp_path: Path,
    stage_name: str,
) -> None:
    """Cancelled stage creation must retain identity until empty cleanup finishes."""
    from yinshi.services import workspace_publication

    parent = tmp_path / "publication-parent"
    parent.mkdir(mode=0o700)
    stage = parent / stage_name
    parent_identity = workspace_publication._directory_identity_json(parent)
    started = threading.Event()
    release = threading.Event()
    created: list[workspace_publication._RetainedPrivateStage] = []

    def create_stage() -> workspace_publication._RetainedPrivateStage:
        retained = workspace_publication._create_private_stage(
            stage,
            parent_identity=parent_identity,
        )
        created.append(retained)
        started.set()
        release.wait(timeout=1)
        return retained

    task = asyncio.create_task(
        workspace_publication._await_local_effect(
            create_stage,
            cancel_result=workspace_publication._remove_cancelled_private_stage,
        )
    )
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(created) == 1
    assert created[0]._closed is True
    assert not stage.exists()
    retry = workspace_publication._create_private_stage(
        stage,
        parent_identity=parent_identity,
    )
    try:
        assert stage.is_dir()
    finally:
        workspace_publication._remove_cancelled_private_stage(retry)


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_succeeds", [True, False])
async def test_stage_persistence_finishes_or_removes_before_cancellation(
    tmp_path: Path,
    persist_succeeds: bool,
) -> None:
    """Cancellation must leave either a recorded stage or no stage."""
    from yinshi.services import workspace_publication

    parent = tmp_path / "publication-parent"
    parent.mkdir(mode=0o700)
    stage = parent / ".private-stage"
    retained = workspace_publication._create_private_stage(
        stage,
        parent_identity=workspace_publication._directory_identity_json(parent),
    )
    started = asyncio.Event()
    release = asyncio.Event()
    recorded: list[str] = []

    async def persist() -> None:
        started.set()
        await release.wait()
        if not persist_succeeds:
            raise RuntimeError("injected persistence failure")
        recorded.append(retained.identity)

    task = asyncio.create_task(
        workspace_publication._persist_created_private_stage(retained, persist)
    )
    await started.wait()
    task.cancel()
    release.set()
    if persist_succeeds:
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            retained.close()
        assert recorded == [retained.identity]
        assert stage.exists()
        reopened = workspace_publication._reopen_private_stage(
            stage,
            parent_identity=workspace_publication._directory_identity_json(parent),
            stage_identity=retained.identity,
        )
        workspace_publication._remove_cancelled_private_stage(reopened)
    else:
        with pytest.raises(RuntimeError, match="persistence failure"):
            await task
        assert retained._closed is True
        assert not stage.exists()


def test_private_stage_creation_failure_removes_created_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-mkdir creation failure must remove the unreturned stage."""
    from yinshi.services import workspace_publication

    parent = tmp_path / "publication-parent"
    parent.mkdir(mode=0o700)
    stage = parent / ".private-stage"
    parent_identity = workspace_publication._directory_identity_json(parent)

    def fail_sync(_descriptor: int) -> None:
        raise OSError("injected sync failure")

    monkeypatch.setattr(workspace_publication.os, "fsync", fail_sync)
    with pytest.raises(
        workspace_publication.WorkspacePublicationError,
        match="creation cleanup failed",
    ):
        workspace_publication._create_private_stage(
            stage,
            parent_identity=parent_identity,
        )
    assert not stage.exists()


@pytest.mark.asyncio
async def test_materializing_repository_retry_reuses_valid_clone(
    git_repo: str,
    tmp_path: Path,
) -> None:
    """A valid materializing clone must resume without cloning into it again."""
    from yinshi.services import workspace_publication

    parent = tmp_path / "publication-parent"
    parent.mkdir(mode=0o700)
    stage = parent / ".repository-stage"
    retained = workspace_publication._create_private_stage(
        stage,
        parent_identity=workspace_publication._directory_identity_json(parent),
    )
    try:
        for _ in range(2):
            await workspace_publication._materialize_owned_repo_checkout(
                git_repo,
                retained.directory,
                None,
                staging_identity=retained.identity,
                access_token=None,
            )
        assert await workspace_publication.validate_local_repo(retained.directory)
    finally:
        retained.close()


def test_materializing_transition_requires_exact_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unmatched materializing update must not accept an unrecorded stage."""
    from yinshi.services import workspace_publication

    class MissingCursor:
        rowcount = 0

    class MissingDatabase:
        def execute(self, *_args, **_kwargs):
            return MissingCursor()

        def commit(self) -> None:
            raise AssertionError("unmatched transition committed")

    monkeypatch.setattr(workspace_publication, "_authorize_checkout", lambda *_args: None)
    with pytest.raises(
        workspace_publication.WorkspacePublicationError,
        match="materialization transition",
    ):
        workspace_publication._persist_materializing(
            MissingDatabase(),
            authorize=lambda _database: None,
            expected=object(),
            operation_id="a" * 32,
            owner_token="b" * 64,
            staging_identity="{}",
        )


@pytest.mark.asyncio
async def test_worktree_restore_retains_staging_repository_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repository replacement must not redirect worktree restoration effects."""
    from yinshi.services import workspace_publication

    repository = tmp_path / "repo"
    repository.mkdir(mode=0o700)
    repository_identity = workspace_publication._directory_identity_json(repository)
    worktree = tmp_path / "worktree"
    worktree.mkdir(mode=0o700)
    worktree_identity = workspace_publication._directory_identity_json(worktree)
    moved_repository = tmp_path / "accepted-repo"
    moved_worktree = tmp_path / "accepted-worktree"

    async def replace_names(repo_path, worktree_path, branch):
        assert branch == "feature"
        assert isinstance(repo_path, workspace_publication._StableDirectory)
        assert isinstance(worktree_path, workspace_publication._StableDirectory)
        repository.rename(moved_repository)
        repository.mkdir(mode=0o700)
        worktree.rename(moved_worktree)
        worktree.mkdir(mode=0o700)
        repo_effect = os.open(
            "repo-effect",
            os.O_WRONLY | os.O_CREAT,
            0o600,
            dir_fd=repo_path.descriptor,
        )
        os.close(repo_effect)
        target_effect = os.open(
            "target-effect",
            os.O_WRONLY | os.O_CREAT,
            0o600,
            dir_fd=worktree_path.descriptor,
        )
        os.close(target_effect)
        return str(worktree_path)

    monkeypatch.setattr(workspace_publication, "restore_worktree", replace_names)

    await workspace_publication._restore_exact_worktree(
        repository,
        worktree,
        "feature",
        repo_identity=repository_identity,
        worktree_identity=worktree_identity,
    )

    assert (moved_repository / "repo-effect").is_file()
    assert (moved_worktree / "target-effect").is_file()
    assert not (repository / "repo-effect").exists()
    assert not (worktree / "target-effect").exists()


def test_backlink_replacement_wins_a_last_moment_name_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained backlink replacement must remain final after a target-name swap."""
    from yinshi.services import workspace_publication

    parent = tmp_path / "registration"
    parent.mkdir(mode=0o700)
    backlink = parent / "gitdir"
    backlink.write_bytes(b"staged\n")
    displaced = parent / "displaced"
    real_replace = workspace_publication.os.replace
    replaced = False

    def swap_then_replace(source, target, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal replaced
        if not replaced:
            replaced = True
            os.rename(target, displaced, src_dir_fd=dst_dir_fd, dst_dir_fd=dst_dir_fd)
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
            os.write(descriptor, b"foreign\n")
            os.close(descriptor)
        real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(workspace_publication.os, "replace", swap_then_replace)
    descriptor = os.open(parent, workspace_publication._directory_open_flags())
    try:
        workspace_publication._replace_owned_regular_file(
            descriptor,
            "gitdir",
            b"staged\n",
            b"final\n",
        )
    finally:
        os.close(descriptor)

    assert backlink.read_bytes() == b"final\n"
    assert displaced.read_bytes() == b"staged\n"
