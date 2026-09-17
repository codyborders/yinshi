"""Workspace lifecycle management."""

import logging
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from yinshi.config import get_settings
from yinshi.exceptions import (
    GitError,
    GitHubAccessError,
    GitHubAppError,
    RepoNotFoundError,
    WorkspaceHasDelegatedThreads,
    WorkspaceNotFoundError,
)
from yinshi.services.git import (
    _run_git,
    clone_local_repo,
    clone_repo,
    create_worktree,
    delete_worktree,
    ensure_remote_url,
    generate_branch_name,
    get_remote_url,
    resolve_remote_base_ref,
    restore_worktree,
    run_git_bytes,
    validate_local_repo,
)
from yinshi.services.github_app import normalize_github_remote, resolve_github_clone_access
from yinshi.services.repository_lifecycle import (
    repository_lifecycle,
    repository_lifecycle_root,
)
from yinshi.services.sidecar_runtime import (
    delete_local_pi_session_file,
    delete_workspace_runtime_home,
)
from yinshi.services.workspace_files import ensure_secret_guardrails
from yinshi.tenant import TenantContext
from yinshi.utils.paths import is_path_inside

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkspaceCheckoutState:
    """Database snapshot needed to prepare one tenant workspace checkout."""

    workspace_id: str
    repo_id: str
    repo_path: str
    remote_url: str | None
    installation_id: int | None
    workspaces: tuple[tuple[str, str], ...]
    workspace_paths: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class WorkspaceDeletionTarget:
    """Authorized database snapshot for public workspace cleanup."""

    workspace_id: str
    repo_id: str
    repo_path: str
    workspace_path: str
    branch: str
    session_ids: tuple[str, ...]
    lock_root: str
    delegation_id: str | None
    snapshot_ref: str | None
    snapshot_commit: str | None
    result_ref: str | None
    result_commit: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceCheckoutPreparation:
    """Deterministic database updates produced by filesystem preparation."""

    workspace_id: str
    repo_id: str
    repo_path: str
    remote_url: str | None
    installation_id: int | None
    workspace_paths: tuple[tuple[str, str], ...]
    update_repo_metadata: bool
    repaired_repo: bool


def _fetch_repo(db: sqlite3.Connection, repo_id: str) -> sqlite3.Row:
    """Load a repo row or raise RepoNotFoundError."""
    assert repo_id, "repo_id must not be empty"
    repo = db.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if not repo:
        raise RepoNotFoundError(f"Repo {repo_id} not found")
    return cast(sqlite3.Row, repo)


def _fetch_workspace(db: sqlite3.Connection, workspace_id: str) -> sqlite3.Row:
    """Load a workspace row or raise WorkspaceNotFoundError."""
    assert workspace_id, "workspace_id must not be empty"
    workspace = db.execute(
        "SELECT * FROM workspaces WHERE id = ?",
        (workspace_id,),
    ).fetchone()
    if not workspace:
        raise WorkspaceNotFoundError(f"Workspace {workspace_id} not found")
    return cast(sqlite3.Row, workspace)


def _tenant_path_is_trusted(tenant: TenantContext, path: str) -> bool:
    """Return whether a tenant path is inside tenant-managed storage."""
    assert tenant.data_dir, "tenant.data_dir must not be empty"
    assert path, "path must not be empty"
    if is_path_inside(path, tenant.data_dir):
        return True

    settings = get_settings()
    if settings.container_enabled:
        return False
    if settings.allowed_repo_base and is_path_inside(path, settings.allowed_repo_base):
        return True
    return False


def _tenant_repo_path(tenant: TenantContext, repo_id: str) -> str:
    """Return the per-tenant repair target for a repo checkout."""
    assert tenant.data_dir, "tenant.data_dir must not be empty"
    assert repo_id, "repo_id must not be empty"
    return os.path.join(tenant.data_dir, "repos", repo_id)


def _workspace_path(repo_path: str, branch: str) -> str:
    """Build the canonical on-disk path for a worktree branch."""
    assert repo_path, "repo_path must not be empty"
    assert branch, "branch must not be empty"
    return os.path.join(repo_path, ".worktrees", branch)


async def _materialize_repo_checkout(
    source_path: str,
    target_path: str,
    remote_url: str | None,
    access_token: str | None = None,
) -> None:
    """Create or reuse a repaired repo checkout inside tenant storage."""
    assert target_path, "target_path must not be empty"

    if await validate_local_repo(target_path):
        return

    if source_path and await validate_local_repo(source_path):
        await clone_local_repo(source_path, target_path, remote_url=remote_url)
        return

    if remote_url:
        await clone_repo(remote_url, target_path, access_token=access_token)
        return

    raise RepoNotFoundError("Repo checkout is missing and cannot be repaired")


async def _sync_repo_checkout_remote(
    repo_path: str,
    remote_url: str | None,
) -> bool:
    """Ensure one valid checkout points at the canonical remote URL."""
    if not await validate_local_repo(repo_path):
        return False
    if remote_url is None:
        return False
    return await ensure_remote_url(repo_path, remote_url)


async def _resolve_remote_checkout(
    tenant: TenantContext,
    remote_url: str | None,
) -> tuple[str | None, str | None, int | None]:
    """Resolve a canonical remote URL plus any GitHub token for repairs."""
    if remote_url is None:
        return None, None, None

    try:
        clone_access = await resolve_github_clone_access(tenant.user_id, remote_url)
    except GitHubAccessError as exc:
        raise GitError(str(exc)) from exc
    except GitHubAppError as exc:
        raise GitError(str(exc)) from exc

    if clone_access is None:
        return remote_url, None, None

    return (
        clone_access.clone_url,
        clone_access.access_token,
        clone_access.installation_id,
    )


async def _refresh_repo_remote_metadata(
    tenant: TenantContext,
    repo_path: str,
    remote_url: str | None,
    installation_id: int | None,
) -> tuple[str | None, str | None, int | None]:
    """Refresh canonical remote metadata without breaking local-only recovery."""
    source_repo_is_available = await validate_local_repo(repo_path)
    access_token = None
    refreshed_remote_url = remote_url
    refreshed_installation_id = installation_id

    if remote_url:
        try:
            (
                refreshed_remote_url,
                access_token,
                resolved_installation_id,
            ) = await _resolve_remote_checkout(tenant, remote_url)
        except GitError:
            if not source_repo_is_available:
                raise
            logger.warning("Refreshing repository from local checkout after remote auth failure")
        else:
            if resolved_installation_id is not None:
                refreshed_installation_id = resolved_installation_id

    return refreshed_remote_url, access_token, refreshed_installation_id


async def _trusted_repo_needs_refresh(
    repo_path: str,
    remote_url: str | None,
    installation_id: int | None,
) -> bool:
    """Return whether a trusted repo should refresh remote metadata."""
    if not await validate_local_repo(repo_path):
        return True
    if remote_url is None:
        return False
    if installation_id is None:
        return True

    normalized_remote = normalize_github_remote(remote_url)
    if normalized_remote is None:
        return False

    current_remote_url = await get_remote_url(repo_path)
    if current_remote_url is None:
        return True
    return current_remote_url.rstrip("/") != normalized_remote.clone_url.rstrip("/")


def ensure_workspace_has_no_delegated_children(
    db: sqlite3.Connection,
    workspace_id: str,
) -> None:
    """Apply the deletion policy: workspace sessions must not parent children."""
    delegation_row = db.execute(
        """SELECT count(*) AS child_count
           FROM thread_delegations d
           JOIN sessions s ON s.id = d.parent_session_id
           WHERE s.workspace_id = ?""",
        (workspace_id,),
    ).fetchone()
    assert delegation_row is not None
    if int(delegation_row["child_count"]) > 0:
        raise WorkspaceHasDelegatedThreads("Workspace sessions parent delegated child threads")


def load_workspace_checkout_state(
    db: sqlite3.Connection,
    workspace_id: str,
) -> WorkspaceCheckoutState:
    """Load immutable checkout inputs in one database operation."""
    workspace = _fetch_workspace(db, workspace_id)
    repo_id = workspace["repo_id"]
    assert repo_id, "workspace repo_id must not be empty"
    repo = _fetch_repo(db, repo_id)
    repo_path = repo["root_path"]
    assert repo_path, "repo root_path must not be empty"
    workspaces = db.execute(
        "SELECT id, branch, path FROM workspaces WHERE repo_id = ? ORDER BY created_at ASC",
        (repo_id,),
    ).fetchall()
    workspace_branches: list[tuple[str, str]] = []
    workspace_paths: list[tuple[str, str]] = []
    for row in workspaces:
        branch = row["branch"]
        if not branch:
            raise WorkspaceNotFoundError(f"Workspace {row['id']} is missing its branch name")
        workspace_branches.append((row["id"], branch))
        workspace_paths.append((row["id"], str(row["path"])))
    repo_keys = repo.keys()
    return WorkspaceCheckoutState(
        workspace_id=workspace_id,
        repo_id=repo_id,
        repo_path=repo_path,
        remote_url=repo["remote_url"],
        installation_id=(repo["installation_id"] if "installation_id" in repo_keys else None),
        workspaces=tuple(workspace_branches),
        workspace_paths=tuple(workspace_paths),
    )


async def prepare_workspace_checkout_for_tenant(
    tenant: TenantContext,
    state: WorkspaceCheckoutState,
) -> WorkspaceCheckoutPreparation:
    """Prepare idempotent filesystem state without holding a database connection."""
    source_repo_is_available = await validate_local_repo(state.repo_path)
    if _tenant_path_is_trusted(tenant, state.repo_path):
        if source_repo_is_available and not await _trusted_repo_needs_refresh(
            state.repo_path,
            state.remote_url,
            state.installation_id,
        ):
            return WorkspaceCheckoutPreparation(
                workspace_id=state.workspace_id,
                repo_id=state.repo_id,
                repo_path=state.repo_path,
                remote_url=state.remote_url,
                installation_id=state.installation_id,
                workspace_paths=(),
                update_repo_metadata=False,
                repaired_repo=False,
            )

        remote_url, _, installation_id = await _refresh_repo_remote_metadata(
            tenant,
            state.repo_path,
            state.remote_url,
            state.installation_id,
        )
        remote_was_updated = await _sync_repo_checkout_remote(state.repo_path, remote_url)
        metadata_changed = (
            remote_url != state.remote_url or installation_id != state.installation_id
        )
        return WorkspaceCheckoutPreparation(
            workspace_id=state.workspace_id,
            repo_id=state.repo_id,
            repo_path=state.repo_path,
            remote_url=remote_url,
            installation_id=installation_id,
            workspace_paths=(),
            update_repo_metadata=remote_was_updated or metadata_changed,
            repaired_repo=False,
        )

    target_repo_path = _tenant_repo_path(tenant, state.repo_id)
    remote_url, access_token, installation_id = await _refresh_repo_remote_metadata(
        tenant,
        state.repo_path,
        state.remote_url,
        state.installation_id,
    )
    await _materialize_repo_checkout(
        state.repo_path,
        target_repo_path,
        remote_url,
        access_token=access_token,
    )
    workspace_paths: list[tuple[str, str]] = []
    for workspace_id, branch in state.workspaces:
        target_workspace_path = _workspace_path(target_repo_path, branch)
        await restore_worktree(target_repo_path, target_workspace_path, branch)
        workspace_paths.append((workspace_id, target_workspace_path))
    return WorkspaceCheckoutPreparation(
        workspace_id=state.workspace_id,
        repo_id=state.repo_id,
        repo_path=target_repo_path,
        remote_url=remote_url,
        installation_id=installation_id,
        workspace_paths=tuple(workspace_paths),
        update_repo_metadata=True,
        repaired_repo=True,
    )


def apply_workspace_checkout_preparation(
    db: sqlite3.Connection,
    preparation: WorkspaceCheckoutPreparation,
) -> dict[str, Any]:
    """Apply deterministic checkout metadata updates in one transaction."""
    if preparation.update_repo_metadata:
        for workspace_id, workspace_path in preparation.workspace_paths:
            db.execute(
                "UPDATE workspaces SET path = ? WHERE id = ?",
                (workspace_path, workspace_id),
            )
        db.execute(
            """UPDATE repos
               SET root_path = ?, remote_url = ?, installation_id = ? WHERE id = ?""",
            (
                preparation.repo_path,
                preparation.remote_url,
                preparation.installation_id,
                preparation.repo_id,
            ),
        )
        db.commit()
        if preparation.repaired_repo:
            logger.info("Repaired repository into tenant storage")
    return dict(_fetch_workspace(db, preparation.workspace_id))


async def ensure_repo_checkout_for_tenant(
    db: sqlite3.Connection,
    tenant: TenantContext,
    repo_id: str,
) -> dict[str, Any]:
    """Repair migrated tenant repo/workspace paths into the tenant data directory.

    Legacy migrations copied root_path and worktree paths into the per-user DB
    without relocating them. This lazily repairs those records the first time
    the repo is used after migration.
    """
    repo = _fetch_repo(db, repo_id)
    repo_path = repo["root_path"]
    assert repo_path, "repo root_path must not be empty"
    remote_url = repo["remote_url"]
    installation_id = repo["installation_id"] if "installation_id" in repo.keys() else None
    source_repo_is_available = await validate_local_repo(repo_path)

    if _tenant_path_is_trusted(tenant, repo_path):
        if source_repo_is_available and not await _trusted_repo_needs_refresh(
            repo_path,
            remote_url,
            installation_id,
        ):
            return dict(repo)

        refreshed_remote_url, _, refreshed_installation_id = await _refresh_repo_remote_metadata(
            tenant,
            repo_path,
            remote_url,
            installation_id,
        )
        remote_was_updated = await _sync_repo_checkout_remote(repo_path, refreshed_remote_url)
        metadata_changed = (
            refreshed_remote_url != remote_url or refreshed_installation_id != installation_id
        )
        if not remote_was_updated and not metadata_changed:
            return dict(repo)

        db.execute(
            "UPDATE repos SET remote_url = ?, installation_id = ? WHERE id = ?",
            (refreshed_remote_url, refreshed_installation_id, repo_id),
        )
        db.commit()
        return dict(_fetch_repo(db, repo_id))

    target_repo_path = _tenant_repo_path(tenant, repo_id)
    remote_url, access_token, installation_id = await _refresh_repo_remote_metadata(
        tenant,
        repo_path,
        remote_url,
        installation_id,
    )
    await _materialize_repo_checkout(
        repo_path,
        target_repo_path,
        remote_url,
        access_token=access_token,
    )

    workspaces = db.execute(
        "SELECT * FROM workspaces WHERE repo_id = ? ORDER BY created_at ASC",
        (repo_id,),
    ).fetchall()
    for workspace in workspaces:
        branch = workspace["branch"]
        if not branch:
            raise WorkspaceNotFoundError(f"Workspace {workspace['id']} is missing its branch name")
        target_workspace_path = _workspace_path(target_repo_path, branch)
        await restore_worktree(target_repo_path, target_workspace_path, branch)
        db.execute(
            "UPDATE workspaces SET path = ? WHERE id = ?",
            (target_workspace_path, workspace["id"]),
        )

    db.execute(
        "UPDATE repos SET root_path = ?, remote_url = ?, installation_id = ? WHERE id = ?",
        (target_repo_path, remote_url, installation_id, repo_id),
    )
    db.commit()
    logger.info("Repaired repository into tenant storage")

    updated_repo = _fetch_repo(db, repo_id)
    return dict(updated_repo)


async def relink_github_repos_for_tenant(
    db: sqlite3.Connection,
    tenant: TenantContext,
    owner_login: str,
) -> int:
    """Refresh existing tenant repos after one GitHub App installation is connected."""
    if not owner_login:
        raise ValueError("owner_login must not be empty")
    refreshed_repo_count = 0
    repos = db.execute(
        "SELECT id FROM repos WHERE remote_url IS NOT NULL ORDER BY created_at ASC"
    ).fetchall()

    for repo_row in repos:
        repo = _fetch_repo(db, repo_row["id"])
        remote_url = repo["remote_url"]
        if not isinstance(remote_url, str) or not remote_url:
            continue
        github_remote = normalize_github_remote(remote_url)
        if github_remote is None:
            continue
        if github_remote.owner.lower() != owner_login.lower():
            continue

        refreshed_repo = await ensure_repo_checkout_for_tenant(db, tenant, repo["id"])
        if refreshed_repo["remote_url"] != repo["remote_url"]:
            refreshed_repo_count += 1
            continue
        if refreshed_repo["installation_id"] != repo["installation_id"]:
            refreshed_repo_count += 1

    return refreshed_repo_count


async def create_workspace_for_repo(
    db: sqlite3.Connection,
    repo_id: str,
    name: str | None = None,
    username: str | None = None,
    tenant: TenantContext | None = None,
) -> dict[str, Any]:
    """Create a new worktree while holding its repository lifecycle lock."""
    lock_root = repository_lifecycle_root(db, tenant)
    async with repository_lifecycle(repo_id, lock_root):
        return await _create_workspace_for_repo_unlocked(
            db,
            repo_id,
            name=name,
            username=username,
            tenant=tenant,
        )


async def _create_workspace_for_repo_unlocked(
    db: sqlite3.Connection,
    repo_id: str,
    name: str | None = None,
    username: str | None = None,
    tenant: TenantContext | None = None,
) -> dict[str, Any]:
    """Create a new worktree after repository lifecycle serialization."""
    if tenant is not None:
        await ensure_repo_checkout_for_tenant(db, tenant, repo_id)

    repo = _fetch_repo(db, repo_id)

    branch = generate_branch_name(username=username)
    if not name:
        name = branch

    repo_path = repo["root_path"]
    assert repo_path, "repo_path must not be empty"
    worktree_dir = _workspace_path(repo_path, branch)
    base_ref: str | None = None

    remote_url = repo["remote_url"]
    if remote_url:
        access_token = None
        try:
            if tenant is not None:
                _, access_token, _ = await _resolve_remote_checkout(tenant, remote_url)
            base_ref = await resolve_remote_base_ref(repo_path, access_token=access_token)
        except GitError:
            logger.warning("Creating worktree from local HEAD after remote sync failure")
            base_ref = None

    await create_worktree(repo_path, worktree_dir, branch, base_ref=base_ref)
    ensure_secret_guardrails(repo_path)

    cursor = db.execute(
        """INSERT INTO workspaces (repo_id, name, branch, path, state)
           VALUES (?, ?, ?, ?, 'ready')""",
        (repo_id, name, branch, worktree_dir),
    )
    db.commit()

    row = db.execute("SELECT * FROM workspaces WHERE rowid = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def _workspace_matches_deletion_target(
    workspace: sqlite3.Row,
    target: WorkspaceDeletionTarget,
) -> bool:
    return (
        str(workspace["repo_id"]) == target.repo_id
        and str(workspace["path"]) == target.workspace_path
        and str(workspace["branch"]) == target.branch
    )


def _validate_delegated_deletion_refs(target: WorkspaceDeletionTarget) -> None:
    if target.delegation_id is None:
        return
    refs = (
        (target.snapshot_ref, f"refs/yinshi/snapshots/{target.delegation_id}"),
        (target.result_ref, f"refs/yinshi/results/{target.delegation_id}"),
    )
    if any(ref is not None and ref != expected for ref, expected in refs):
        raise GitError("delegated workspace ref ownership is invalid")


def prepare_workspace_deletion(
    db: sqlite3.Connection,
    workspace_id: str,
    *,
    tenant: TenantContext | None = None,
) -> WorkspaceDeletionTarget:
    """Authorize cleanup and return immutable values needed outside SQLite."""
    workspace = _fetch_workspace(db, workspace_id)
    ensure_workspace_has_no_delegated_children(db, workspace_id)
    repo = _fetch_repo(db, workspace["repo_id"])
    session_rows = db.execute(
        "SELECT id FROM sessions WHERE workspace_id = ? ORDER BY id",
        (workspace_id,),
    ).fetchall()
    delegated_rows = db.execute(
        "SELECT d.id, d.snapshot_ref, d.base_commit AS snapshot_commit, "
        "r.result_ref, r.result_commit FROM thread_delegations d "
        "JOIN sessions s ON s.id = d.child_session_id "
        "LEFT JOIN thread_results r ON r.delegation_id = d.id "
        "WHERE s.workspace_id = ?",
        (workspace_id,),
    ).fetchall()
    if len(delegated_rows) > 1:
        raise WorkspaceHasDelegatedThreads("Workspace has conflicting delegated ownership")
    delegated = delegated_rows[0] if delegated_rows else None
    target = WorkspaceDeletionTarget(
        workspace_id=workspace_id,
        repo_id=str(workspace["repo_id"]),
        repo_path=str(repo["root_path"]),
        workspace_path=str(workspace["path"]),
        branch=str(workspace["branch"]),
        session_ids=tuple(str(row["id"]) for row in session_rows),
        lock_root=str(repository_lifecycle_root(db, tenant)),
        delegation_id=str(delegated["id"]) if delegated is not None else None,
        snapshot_ref=(
            str(delegated["snapshot_ref"])
            if delegated is not None and delegated["snapshot_ref"] is not None
            else None
        ),
        snapshot_commit=(
            str(delegated["snapshot_commit"])
            if delegated is not None and delegated["snapshot_commit"] is not None
            else None
        ),
        result_ref=(
            str(delegated["result_ref"])
            if delegated is not None and delegated["result_ref"] is not None
            else None
        ),
        result_commit=(
            str(delegated["result_commit"])
            if delegated is not None and delegated["result_commit"] is not None
            else None
        ),
    )
    _validate_delegated_deletion_refs(target)
    return target


def transition_workspace_state(
    db: sqlite3.Connection,
    workspace_id: str,
    state: str,
) -> bool:
    """Change public workspace state unless cleanup already owns it."""
    cursor = db.execute(
        "UPDATE workspaces SET state = ? WHERE id = ? AND state != 'deleting'",
        (state, workspace_id),
    )
    db.commit()
    return cursor.rowcount == 1


def claim_workspace_deletion(
    db: sqlite3.Connection,
    target: WorkspaceDeletionTarget,
) -> None:
    """Block new child reservations before external cleanup starts."""
    db.execute("BEGIN IMMEDIATE")
    try:
        workspace = _fetch_workspace(db, target.workspace_id)
        if not _workspace_matches_deletion_target(workspace, target):
            raise WorkspaceNotFoundError(target.workspace_id)
        _validate_delegated_deletion_refs(target)
        ensure_workspace_has_no_delegated_children(db, target.workspace_id)
        if workspace["state"] == "deleting":
            db.commit()
            return
        if workspace["state"] != "ready":
            raise WorkspaceNotFoundError(target.workspace_id)
        updated = db.execute(
            "UPDATE workspaces SET state = 'deleting' WHERE id = ? AND state = 'ready'",
            (target.workspace_id,),
        ).rowcount
        if updated != 1:
            raise WorkspaceNotFoundError(target.workspace_id)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def release_workspace_deletion(
    db: sqlite3.Connection,
    target: WorkspaceDeletionTarget,
) -> None:
    """Release a cleanup claim when no Git removal has started."""
    db.execute("BEGIN IMMEDIATE")
    try:
        workspace = db.execute(
            "SELECT * FROM workspaces WHERE id = ?",
            (target.workspace_id,),
        ).fetchone()
        if (
            workspace is not None
            and _workspace_matches_deletion_target(workspace, target)
            and workspace["state"] == "deleting"
        ):
            db.execute(
                "UPDATE workspaces SET state = 'ready' WHERE id = ? AND state = 'deleting'",
                (target.workspace_id,),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


@asynccontextmanager
async def workspace_deletion_lifecycle(
    target: WorkspaceDeletionTarget,
) -> AsyncIterator[None]:
    """Serialize one cleanup owner across database and external effects."""
    async with repository_lifecycle(target.repo_id, Path(target.lock_root)):
        yield


async def _delete_owned_ref_if_matches(
    repo_path: str,
    ref: str,
    expected_oid: str,
) -> None:
    current = (
        await run_git_bytes(
            ["for-each-ref", "--count=1", "--format=%(objectname)", ref],
            cwd=repo_path,
        )
    ).strip()
    if not current:
        return
    if current.decode("ascii", errors="strict") != expected_oid:
        raise GitError("delegated workspace ref ownership changed")
    await _run_git(
        ["update-ref", "--no-deref", "-d", ref, expected_oid],
        cwd=repo_path,
    )


async def apply_workspace_deletion(
    target: WorkspaceDeletionTarget,
    *,
    tenant: TenantContext | None = None,
) -> None:
    """Remove one workspace while its deletion lifecycle is held."""
    await delete_worktree(target.repo_path, target.workspace_path)
    if target.delegation_id is not None:
        refs = (
            (target.snapshot_ref, target.snapshot_commit),
            (target.result_ref, target.result_commit),
        )
        for ref, expected_oid in refs:
            if ref is not None and expected_oid is not None:
                await _delete_owned_ref_if_matches(
                    target.repo_path,
                    ref,
                    expected_oid,
                )
    if tenant is not None:
        delete_workspace_runtime_home(tenant, target.workspace_id)
    else:
        for session_id in target.session_ids:
            try:
                delete_local_pi_session_file(session_id)
            except OSError:
                logger.warning("Failed to delete Pi session file")


def finalize_workspace_deletion(
    db: sqlite3.Connection,
    target: WorkspaceDeletionTarget,
) -> None:
    """Reauthorize the exact workspace row before removing database state."""
    workspace = _fetch_workspace(db, target.workspace_id)
    if (
        not _workspace_matches_deletion_target(workspace, target)
        or workspace["state"] != "deleting"
    ):
        raise WorkspaceNotFoundError(target.workspace_id)
    ensure_workspace_has_no_delegated_children(db, target.workspace_id)
    db.execute("DELETE FROM workspaces WHERE id = ?", (target.workspace_id,))
    db.commit()


async def delete_workspace(
    db: sqlite3.Connection,
    workspace_id: str,
    *,
    tenant: TenantContext | None = None,
) -> None:
    """Delete a workspace while holding its repository lifecycle lock."""
    workspace = _fetch_workspace(db, workspace_id)
    repo_id = str(workspace["repo_id"])
    lock_root = repository_lifecycle_root(db, tenant)
    async with repository_lifecycle(repo_id, lock_root):
        await _delete_workspace_unlocked(db, workspace_id, tenant=tenant)


async def _delete_workspace_unlocked(
    db: sqlite3.Connection,
    workspace_id: str,
    *,
    tenant: TenantContext | None = None,
) -> None:
    """Delete one workspace after repository lifecycle serialization."""
    workspace = _fetch_workspace(db, workspace_id)
    ensure_workspace_has_no_delegated_children(db, workspace_id)

    session_rows = []
    if tenant is None:
        session_rows = db.execute(
            "SELECT id FROM sessions WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchall()

    repo = _fetch_repo(db, workspace["repo_id"])
    await delete_worktree(repo["root_path"], workspace["path"])

    if tenant is not None:
        delete_workspace_runtime_home(tenant, workspace_id)
    else:
        for session_row in session_rows:
            try:
                delete_local_pi_session_file(session_row["id"])
            except OSError:
                logger.warning("Failed to delete Pi session file")

    db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
    db.commit()
