"""Shared API dependency helpers (tenant extraction, DB context, legacy auth)."""

import asyncio
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Final, TypeVar, cast

from fastapi import HTTPException, Request

from yinshi.config import get_settings
from yinshi.db import get_db
from yinshi.services.github_app import GitHubCloneAccessResolver
from yinshi.tenant import (
    TenantContext,
    TenantDatabaseTemporarilyUnavailable,
    get_user_db,
    is_temporary_tenant_database_error,
)

_T = TypeVar("_T")
_TENANT_DB_RETRY_DEADLINE_SECONDS: Final[float] = 20.0
_TENANT_DB_REQUEST_RETRY_BUDGET_SECONDS: Final[float] = 18.0
_TENANT_DB_RETRY_DELAY_SECONDS: Final[float] = 0.05
_TENANT_DB_RETRY_DELAY_MAX_SECONDS: Final[float] = 1.0


def get_github_clone_access_resolver(
    request: Request,
) -> GitHubCloneAccessResolver | None:
    """Return the configured runtime GitHub clone access resolver."""
    resolver = getattr(request.app.state, "github_clone_access_resolver", None)
    if resolver is None:
        return None
    if not callable(resolver):
        raise RuntimeError("github_clone_access_resolver must be callable")
    return cast(GitHubCloneAccessResolver, resolver)


def get_tenant(request: Request) -> TenantContext | None:
    """Get the TenantContext from request state, or None if auth is disabled."""
    return getattr(request.state, "tenant", None)


def require_tenant(request: Request) -> TenantContext:
    """Get the TenantContext from request state, raising 401 if missing.

    Use this in endpoints that always require authentication.
    """
    tenant = get_tenant(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return tenant


def request_database_identity(request: Request) -> str:
    """Normalize the selected database identity without traversing the filesystem.

    Desktop mode selects the legacy database even when a tenant is present.
    Distinct path aliases fail closed rather than probing filesystem identity.
    """
    tenant = get_tenant(request)
    mode = getattr(request.app.state, "mode", None)
    path = tenant.db_path if tenant is not None and mode != "desktop" else get_settings().db_path
    if not isinstance(path, str) or not path:
        raise RuntimeError("Request database is unavailable")
    return os.path.normcase(os.path.abspath(path))


@contextmanager
def get_db_for_request(request: Request) -> Iterator[sqlite3.Connection]:
    """Return the correct DB connection for the current request.

    If a tenant is present (multi-tenant mode), returns the user's
    per-tenant database. Otherwise falls back to the shared legacy DB.
    """
    tenant = get_tenant(request)
    application_mode = getattr(request.app.state, "mode", None)
    if tenant and application_mode != "desktop":
        with get_user_db(tenant) as db:
            yield db
    else:
        with get_db() as db:
            yield db


def _run_db_operation_attempt(
    request: Request,
    operation: Callable[[sqlite3.Connection], _T],
) -> _T:
    """Run one blocking operation and discard any connection that observes failure."""
    with get_db_for_request(request) as database:
        try:
            return operation(database)
        except Exception:
            with suppress(Exception):
                database.rollback()
            raise


async def _drain_task_after_cancellation(attempt: asyncio.Task[_T]) -> None:
    """Wait through repeated cancellation until one shielded task finishes."""
    while not attempt.done():
        try:
            await asyncio.shield(attempt)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if not attempt.cancelled():
        with suppress(BaseException):
            attempt.result()


async def _await_database_attempt(attempt: asyncio.Task[_T]) -> _T:
    """Wait for a thread attempt to finish before propagating caller cancellation."""
    try:
        return await asyncio.shield(attempt)
    except asyncio.CancelledError:
        await _drain_task_after_cancellation(attempt)
        raise


def _database_retry_deadline(
    request: Request,
    loop: asyncio.AbstractEventLoop,
    *,
    shared_request_budget: bool,
) -> float:
    """Return one absolute retry deadline shared by foreground request operations."""
    if not shared_request_budget or not hasattr(request, "state"):
        return loop.time() + _TENANT_DB_RETRY_DEADLINE_SECONDS
    deadline = getattr(request.state, "tenant_database_retry_deadline", None)
    if isinstance(deadline, float):
        return deadline
    deadline = loop.time() + _TENANT_DB_REQUEST_RETRY_BUDGET_SECONDS
    request.state.tenant_database_retry_deadline = deadline
    return deadline


async def run_db_operation_for_request(
    request: Request,
    operation: Callable[[sqlite3.Connection], _T],
    *,
    shared_request_budget: bool = True,
) -> _T:
    """Retry exact temporary tenant storage failures on fresh connections."""
    if not callable(operation):
        raise TypeError("database operation must be callable")
    loop = asyncio.get_running_loop()
    deadline = _database_retry_deadline(
        request,
        loop,
        shared_request_budget=shared_request_budget,
    )
    delay = _TENANT_DB_RETRY_DELAY_SECONDS
    while True:
        attempt = asyncio.create_task(
            asyncio.to_thread(_run_db_operation_attempt, request, operation)
        )
        try:
            return await _await_database_attempt(attempt)
        except Exception as exc:
            if not is_temporary_tenant_database_error(exc):
                raise
            remaining = deadline - loop.time()
            if remaining <= 0:
                cause = (
                    exc.__cause__
                    if isinstance(exc, TenantDatabaseTemporarilyUnavailable)
                    and exc.__cause__ is not None
                    else exc
                )
                raise TenantDatabaseTemporarilyUnavailable(
                    "Tenant database storage is temporarily unavailable"
                ) from cause
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, _TENANT_DB_RETRY_DELAY_MAX_SECONDS)


# --- Legacy helpers (kept for backward compatibility during migration) ---


def get_user_email(request: Request) -> str | None:
    """Get authenticated user email, or None if auth is disabled."""
    return getattr(request.state, "user_email", None)


def check_owner(owner_email: str | None, user_email: str | None) -> None:
    """Raise 403 if authenticated user doesn't own the resource.

    Access is allowed when:
    - Auth is disabled (user_email is None)
    - Resource has no owner (owner_email is None, e.g. pre-migration data)
    - Owner matches the authenticated user
    """
    if user_email and owner_email and owner_email != user_email:
        raise HTTPException(status_code=403, detail="Not authorized")


def check_workspace_owner(
    db: sqlite3.Connection,
    workspace_id: str,
    request: Request,
) -> None:
    """In legacy mode, verify the authenticated user owns the workspace's repo."""
    if get_tenant(request):
        return
    ws = db.execute(
        "SELECT w.id, r.owner_email FROM workspaces w "
        "JOIN repos r ON w.repo_id = r.id WHERE w.id = ?",
        (workspace_id,),
    ).fetchone()
    if ws:
        check_owner(ws["owner_email"], get_user_email(request))
    else:
        raise HTTPException(status_code=404, detail="Workspace not found")


def check_session_owner(
    db: sqlite3.Connection,
    session_id: str,
    request: Request,
) -> None:
    """In legacy mode, verify the authenticated user owns the session's repo."""
    if get_tenant(request):
        return
    row = db.execute(
        "SELECT s.id, r.owner_email FROM sessions s "
        "JOIN workspaces w ON s.workspace_id = w.id "
        "JOIN repos r ON w.repo_id = r.id "
        "WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if row:
        check_owner(row["owner_email"], get_user_email(request))
    else:
        raise HTTPException(status_code=404, detail="Session not found")
