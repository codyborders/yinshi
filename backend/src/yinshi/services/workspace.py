"""Workspace lifecycle management."""

import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, cast

from yinshi.config import get_settings
from yinshi.exceptions import (
    GitError,
    GitHubAccessError,
    GitHubAppError,
    RepoNotFoundError,
    WorkspaceNotFoundError,
)
from yinshi.services.git import (
    clone_local_repo,
    clone_repo,
    create_worktree,
    delete_worktree,
    ensure_remote_url,
    generate_branch_name,
    get_remote_url,
    resolve_remote_base_ref,
    restore_worktree,
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
        "SELECT id, branch FROM workspaces WHERE repo_id = ? ORDER BY created_at ASC",
        (repo_id,),
    ).fetchall()
    workspace_branches: list[tuple[str, str]] = []
    for row in workspaces:
        branch = row["branch"]
        if not branch:
            raise WorkspaceNotFoundError(f"Workspace {row['id']} is missing its branch name")
        workspace_branches.append((row["id"], branch))
    return WorkspaceCheckoutState(
        workspace_id=workspace_id,
        repo_id=repo_id,
        repo_path=repo_path,
        remote_url=repo["remote_url"],
        installation_id=(repo["installation_id"] if "installation_id" in repo.keys() else None),
        workspaces=tuple(workspace_branches),
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


async def ensure_workspace_checkout_for_tenant(
    db: sqlite3.Connection,
    tenant: TenantContext,
    workspace_id: str,
) -> dict[str, Any]:
    """Repair the repo backing a workspace and return the updated workspace row."""
    workspace = _fetch_workspace(db, workspace_id)
    repo_id = workspace["repo_id"]
    assert repo_id, "workspace repo_id must not be empty"

    await ensure_repo_checkout_for_tenant(db, tenant, repo_id)
    updated_workspace = _fetch_workspace(db, workspace_id)
    return dict(updated_workspace)


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
