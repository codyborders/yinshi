"""CRUD endpoints for repositories."""

import logging
import sqlite3
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from yinshi.api.deps import check_owner, get_db_for_request, get_tenant, get_user_email
from yinshi.config import get_settings
from yinshi.exceptions import (
    GitError,
    GitHubAccessError,
    GitHubAccessNotGrantedError,
    GitHubAppError,
    GitHubConnectRequiredError,
)
from yinshi.models import RepoCreate, RepoOut, RepoUpdate
from yinshi.rate_limit import limiter
from yinshi.services.git import (
    cleanup_repository_worktrees,
    clone_local_repo,
    clone_repo,
    validate_local_repo,
)
from yinshi.services.github_app import GitHubCloneAccess, resolve_github_clone_access
from yinshi.services.repository_lifecycle import (
    ManagedPathQuarantine,
    repository_lifecycle,
    repository_lifecycle_root,
)
from yinshi.services.run_coordinator import get_run_coordinator
from yinshi.services.sidecar import release_sessions
from yinshi.services.sidecar_runtime import (
    _workspace_home_expected_path,
    local_pi_session_file,
)
from yinshi.tenant import TenantContext, validate_user_path
from yinshi.utils.paths import is_path_inside

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/repos", tags=["repos"])

# Only these columns can be updated via PATCH
_UPDATABLE_COLUMNS = {"name", "custom_prompt", "agents_md"}
_RUNTIME_BUSY_DETAIL = "Workspace is still stopping; deletion can be retried"


def _validate_local_path(path_str: str) -> str:
    """Validate and resolve a local path, checking against allowed base.

    Fail-closed: if ``allowed_repo_base`` is not configured, all local
    imports are rejected.
    """
    settings = get_settings()
    if not settings.allowed_repo_base:
        raise HTTPException(
            status_code=400,
            detail="Local repo imports are disabled (allowed_repo_base not set)",
        )
    resolved = str(Path(path_str).resolve())
    if not is_path_inside(resolved, settings.allowed_repo_base):
        raise HTTPException(status_code=400, detail="Path not in allowed directory")
    return resolved


def _check_repo_owner(row: sqlite3.Row, request: Request) -> None:
    """In legacy mode, verify the authenticated user owns the repo."""
    tenant = get_tenant(request)
    if not tenant:
        check_owner(row["owner_email"], get_user_email(request))


def _github_connect_url(request: Request) -> str | None:
    """Return the GitHub connect URL when the feature is usable for this request."""
    settings = get_settings()
    if not settings.github_app_slug:
        return None
    if get_tenant(request) is None:
        return None
    return "/auth/github/install"


def _github_http_exception(error: GitHubAccessError) -> HTTPException:
    """Convert a GitHub access error into a structured HTTP error."""
    detail = {
        "code": error.code,
        "message": str(error),
        "connect_url": error.connect_url,
        "manage_url": error.manage_url,
    }
    return HTTPException(status_code=400, detail=detail)


async def _resolve_clone_access(
    request: Request,
    remote_url: str,
) -> GitHubCloneAccess | None:
    """Resolve GitHub clone credentials for a remote, if applicable."""
    tenant = get_tenant(request)
    user_id = tenant.user_id if tenant else None
    try:
        return await resolve_github_clone_access(user_id, remote_url)
    except GitHubAccessError as error:
        raise _github_http_exception(error)
    except GitHubAppError as error:
        logger.error("GitHub integration failed during repository credential resolution")
        raise HTTPException(status_code=502, detail=str(error))


def _github_clone_failure(
    request: Request,
    clone_access: GitHubCloneAccess,
) -> HTTPException:
    """Translate an anonymous GitHub clone failure into an actionable error."""
    assert clone_access.access_token is None, "clone failure helper expects anonymous access"
    if clone_access.manage_url:
        return _github_http_exception(
            GitHubAccessNotGrantedError(
                "Grant this repository to the connected GitHub installation and try again.",
                manage_url=clone_access.manage_url,
            )
        )
    return _github_http_exception(
        GitHubConnectRequiredError(
            "Connect GitHub to import this private repository.",
            connect_url=_github_connect_url(request),
        )
    )


@router.get("", response_model=list[RepoOut])
def list_repos(request: Request) -> list[dict[str, Any]]:
    """List all imported repositories."""
    tenant = get_tenant(request)
    email = None if tenant else get_user_email(request)

    with get_db_for_request(request) as db:
        if email:
            rows = db.execute(
                "SELECT * FROM repos WHERE owner_email = ? OR owner_email IS NULL "
                "ORDER BY created_at DESC",
                (email,),
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM repos ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


@router.post("", response_model=RepoOut, status_code=201)
@limiter.limit("10/hour")
async def import_repo(body: RepoCreate, request: Request) -> dict[str, Any]:
    """Import a repository (clone from URL or register local path)."""
    tenant = get_tenant(request)
    settings = get_settings()
    normalized_remote_url = body.remote_url
    clone_access: GitHubCloneAccess | None = None
    repo_id: str | None = None

    if body.local_path:
        resolved = _validate_local_path(body.local_path)
        if not Path(resolved).is_dir():
            raise HTTPException(status_code=400, detail="Path does not exist")
        is_repo = await validate_local_repo(resolved)
        if not is_repo:
            raise HTTPException(status_code=400, detail="Not a valid git repository")
        if tenant and settings.container_enabled:
            repo_id = uuid.uuid4().hex
            root_path = str(Path(tenant.data_dir) / "repos" / repo_id)
            try:
                await clone_local_repo(resolved, root_path)
            except GitError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        else:
            root_path = resolved
    elif body.remote_url:
        clone_access = await _resolve_clone_access(request, body.remote_url)
        access_token = None
        if clone_access is not None:
            normalized_remote_url = clone_access.clone_url
            access_token = clone_access.access_token

        # Sanitize name to prevent path traversal.
        safe_name = Path(body.name).name
        if not safe_name or safe_name != body.name or ".." in body.name:
            raise HTTPException(
                status_code=400,
                detail="Invalid repository name (must be a simple directory name)",
            )

        if tenant:
            clone_dir = str(Path(tenant.data_dir) / "repos" / safe_name)
        else:
            clone_dir = str(Path.home() / ".yinshi" / "repos" / safe_name)
        try:
            root_path = await clone_repo(
                normalized_remote_url or body.remote_url,
                clone_dir,
                access_token=access_token,
            )
        except GitError as e:
            if clone_access is not None and clone_access.access_token is None:
                if clone_access.repository_installation_id is not None:
                    raise _github_clone_failure(request, clone_access)
                if clone_access.manage_url is not None:
                    raise _github_clone_failure(request, clone_access)
            else:
                assert clone_access is None or clone_access.access_token is not None
            raise HTTPException(status_code=400, detail=str(e))
    else:
        raise HTTPException(status_code=400, detail="Either remote_url or local_path is required")

    installation_id = None
    if clone_access is not None and clone_access.access_token is not None:
        installation_id = clone_access.installation_id
    with get_db_for_request(request) as db:
        if tenant:
            if repo_id is None:
                cursor = db.execute(
                    """INSERT INTO repos (name, remote_url, root_path, custom_prompt, agents_md, installation_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        body.name,
                        normalized_remote_url,
                        root_path,
                        body.custom_prompt,
                        body.agents_md,
                        installation_id,
                    ),
                )
            else:
                cursor = db.execute(
                    """INSERT INTO repos (id, name, remote_url, root_path, custom_prompt, agents_md, installation_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        repo_id,
                        body.name,
                        normalized_remote_url,
                        root_path,
                        body.custom_prompt,
                        body.agents_md,
                        installation_id,
                    ),
                )
        else:
            email = get_user_email(request)
            cursor = db.execute(
                """INSERT INTO repos (name, remote_url, root_path, custom_prompt, agents_md, owner_email, installation_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    body.name,
                    normalized_remote_url,
                    root_path,
                    body.custom_prompt,
                    body.agents_md,
                    email,
                    installation_id,
                ),
            )
        db.commit()
        row = db.execute("SELECT * FROM repos WHERE rowid = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)


@router.get("/{repo_id}", response_model=RepoOut)
def get_repo(repo_id: str, request: Request) -> dict[str, Any]:
    """Get a single repository by ID."""
    with get_db_for_request(request) as db:
        row = db.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Repo not found")
        _check_repo_owner(row, request)
        return dict(row)


@router.patch("/{repo_id}", response_model=RepoOut)
def update_repo(
    repo_id: str,
    body: RepoUpdate,
    request: Request,
) -> dict[str, Any]:
    """Update a repository."""
    with get_db_for_request(request) as db:
        row = db.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Repo not found")
        _check_repo_owner(row, request)

        updates = {
            k: v for k, v in body.model_dump(exclude_unset=True).items() if k in _UPDATABLE_COLUMNS
        }
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [repo_id]
            db.execute(f"UPDATE repos SET {set_clause} WHERE id = ?", values)
            db.commit()
        row = db.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        return dict(row)


def _workspace_path_plans(
    repo: sqlite3.Row,
    workspaces: list[sqlite3.Row],
) -> list[tuple[Path, Path, str]]:
    """Return managed worktrees after checking their repository ownership."""
    repo_root = Path(str(repo["root_path"]))
    worktree_root = repo_root / ".worktrees"
    allowed_repo_base = Path(get_settings().allowed_repo_base)
    plans: list[tuple[Path, Path, str]] = []
    for workspace in workspaces:
        workspace_path = Path(str(workspace["path"]))
        if workspace_path.exists() and not _workspace_belongs_to_repo(
            repo_root,
            workspace_path,
        ):
            raise ValueError("workspace is not owned by its repository")
        branch = str(workspace["branch"])
        if is_path_inside(str(workspace_path), str(worktree_root)):
            plans.append((workspace_path, worktree_root, branch))
        elif get_settings().allowed_repo_base and is_path_inside(
            str(workspace_path),
            str(allowed_repo_base),
        ):
            plans.append((workspace_path, allowed_repo_base, branch))
        else:
            raise ValueError("workspace path is outside its managed root")
    return plans


def _workspace_belongs_to_repo(repo_root: Path, workspace_path: Path) -> bool:
    """Check a linked worktree's gitdir points into the selected repository."""
    git_file = workspace_path / ".git"
    if git_file.is_symlink() or not git_file.is_file():
        return False
    try:
        if git_file.stat().st_size > 4096:
            return False
        content = git_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return False
    prefix = "gitdir: "
    if not content.startswith(prefix):
        return False
    git_directory = Path(content[len(prefix) :])
    if not git_directory.is_absolute():
        git_directory = workspace_path / git_directory
    return is_path_inside(
        str(git_directory),
        str(repo_root / ".git" / "worktrees"),
    )


def _managed_repo_plan(
    repo: sqlite3.Row,
    tenant: TenantContext | None,
) -> tuple[Path, Path] | None:
    """Return a Yinshi-owned checkout and its trusted root."""
    root_path = Path(str(repo["root_path"]))
    if tenant is not None:
        if not is_path_inside(str(root_path), tenant.data_dir):
            return None
        validate_user_path(tenant, str(root_path))
        return root_path, Path(tenant.data_dir)

    legacy_repo_directory = Path.home() / ".yinshi" / "repos"
    if bool(repo["remote_url"]) and is_path_inside(
        str(root_path),
        str(legacy_repo_directory),
    ):
        return root_path, legacy_repo_directory
    return None


@router.delete("/{repo_id}", status_code=204)
async def delete_repo(repo_id: str, request: Request) -> None:
    """Delete a repository while holding its lifecycle lock."""
    tenant = get_tenant(request)
    with get_db_for_request(request) as db:
        lock_root = repository_lifecycle_root(db, tenant)
        async with repository_lifecycle(repo_id, lock_root):
            await _delete_repo_locked(repo_id, request, db)


async def _delete_repo_locked(
    repo_id: str,
    request: Request,
    database: sqlite3.Connection,
) -> None:
    """Delete a repository after lifecycle serialization."""
    with nullcontext(database) as db:
        row = db.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Repo not found")
        _check_repo_owner(row, request)
        tenant = get_tenant(request)
        workspace_rows = db.execute(
            "SELECT * FROM workspaces WHERE repo_id = ? ORDER BY rowid ASC",
            (repo_id,),
        ).fetchall()
        session_ids: list[str] = []
        try:
            coordinator = get_run_coordinator()
            for workspace in workspace_rows:
                workspace_id = str(workspace["id"])
                session_rows = db.execute(
                    "SELECT id FROM sessions WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchall()
                for session_row in session_rows:
                    session_id = str(session_row["id"])
                    session_ids.append(session_id)
                    await coordinator.request_cancel(session_id)

            busy_workspace_found = False
            if request.app.state.mode == "desktop":
                await release_sessions(get_settings().sidecar_socket_path, session_ids)
            elif tenant is not None:
                container_manager = getattr(request.app.state, "container_manager", None)
                if container_manager is None and get_settings().container_enabled:
                    raise RuntimeError("container manager is unavailable")
                if container_manager is not None:
                    for workspace in workspace_rows:
                        workspace_id = str(workspace["id"])
                        container_removed = await container_manager.destroy_container(
                            tenant.user_id,
                            runtime_id=workspace_id,
                        )
                        if not container_removed:
                            busy_workspace_found = True
        except (OSError, RuntimeError, ValueError):
            logger.error("Failed to stop workspace runtime while deleting repository")
            raise HTTPException(
                status_code=500,
                detail="Repository cleanup failed; deletion can be retried",
            ) from None

        if busy_workspace_found:
            raise HTTPException(status_code=409, detail=_RUNTIME_BUSY_DETAIL)

        quarantine = ManagedPathQuarantine()
        try:
            workspace_plans = _workspace_path_plans(row, list(workspace_rows))
            runtime_root = Path(tenant.data_dir) if tenant is not None else None
            runtime_paths = (
                [
                    _workspace_home_expected_path(tenant, str(workspace["id"]))
                    for workspace in workspace_rows
                ]
                if tenant is not None
                else []
            )
            session_paths = (
                [Path(local_pi_session_file(session_id)) for session_id in session_ids]
                if tenant is None
                else []
            )
            managed_repo = _managed_repo_plan(row, tenant)
            cleanup_repo_root = Path(str(row["root_path"]))
            git_worktrees = [
                (str(workspace_path), branch)
                for workspace_path, _workspace_root, branch in workspace_plans
            ]
            if runtime_root is not None:
                for runtime_path in runtime_paths:
                    quarantine.validate(runtime_path, runtime_root)
            for session_path in session_paths:
                quarantine.validate(session_path, session_path.parent)
            for workspace_path, workspace_root, _branch in workspace_plans:
                quarantine.validate(workspace_path, workspace_root)
            if managed_repo is not None:
                quarantine.validate(*managed_repo)
            if runtime_root is not None:
                for runtime_path in runtime_paths:
                    quarantine.move(runtime_path, runtime_root)
            for session_path in session_paths:
                quarantine.move(session_path, session_path.parent)
            for workspace_path, workspace_root, _branch in workspace_plans:
                quarantine.move(workspace_path, workspace_root)
            if managed_repo is not None:
                quarantine.move(*managed_repo)
                for entry in reversed(quarantine.entries):
                    if entry.source == managed_repo[0]:
                        cleanup_repo_root = entry.target
                        break
            db.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
            db.commit()
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            db.rollback()
            try:
                quarantine.restore()
            except (OSError, RuntimeError, ValueError):
                logger.error("Repository path restoration failed")
            logger.error("Repository deletion failed before database commit")
            raise HTTPException(
                status_code=500,
                detail="Repository cleanup failed; deletion can be retried",
            ) from None

        try:
            await cleanup_repository_worktrees(
                str(cleanup_repo_root),
                git_worktrees,
            )
        except (GitError, OSError, ValueError):
            logger.error("Repository Git cleanup failed")

        try:
            quarantine.discard()
        except (OSError, RuntimeError, ValueError):
            logger.error("Repository durable cleanup failed")
