"""Interrupt stale reservations and retry cleanup of recorded Git ownership.

Claims prevent late attachment. Cleanup checks durable ownership under the
physical repository lock. Incomplete cleanup retains its claim for later
request-scoped retries. No database connection spans Git operations.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from fastapi import Request

from yinshi.api.deps import (
    get_tenant,
    get_user_email,
    request_database_identity,
    run_db_operation_for_request,
)
from yinshi.config import get_settings
from yinshi.services.thread_git_ownership import (
    ThreadGitCleanup,
    ThreadGitOwnershipError,
)
from yinshi.services.thread_lifecycle import (
    DELEGATION_STATUS_INTERRUPTED,
    DELEGATION_STATUS_PROVISIONING,
)
from yinshi.services.thread_workspaces import (
    ThreadStagedChildGit,
    ThreadWorkspaceService,
)

logger = logging.getLogger(__name__)

_PROVISIONING_STALE_ERROR_CODE = "provisioning_stale"
_PROVISIONING_STALE_SAFE_DETAIL = "stale provisioning was interrupted before completion"

_OWNED_CLEANUP_FROM = (
    "FROM thread_delegations d JOIN sessions s ON s.id = d.parent_session_id "
    "JOIN workspaces w ON w.id = s.workspace_id JOIN repos r ON r.id = w.repo_id "
    "WHERE d.git_artifacts_claimed = 1 "
    "AND d.child_session_id IS NULL AND d.child_workspace_id IS NULL "
    "AND d.status IN ('completed', 'failed', 'cancelled', 'interrupted') "
    "AND NOT EXISTS (SELECT 1 FROM thread_results result WHERE result.delegation_id = d.id) "
)
_CLEANUP_MATCH_FIELDS = (
    "git_artifact_namespace",
    "status",
    "parent_session_id",
    "cleanup_parent_workspace",
    "cleanup_repo_id",
    "cleanup_parent_path",
    "cleanup_repo_path",
    "snapshot_ref",
    "base_commit",
    "base_kind",
)


def _cleanup_owner_filter(request: Request) -> tuple[str, tuple[str, ...]]:
    owner = None if get_tenant(request) is not None else get_user_email(request) or None
    if owner is None:
        return "", ()
    return " AND (r.owner_email IS NULL OR r.owner_email = ?) ", (owner,)


def _cleanup_selection(delegation_ids: tuple[str, ...] | None) -> tuple[str, tuple[str, ...]]:
    if delegation_ids is None:
        return "", ()
    if len(delegation_ids) > 500 or len(set(delegation_ids)) != len(delegation_ids):
        raise ValueError("cleanup selection must contain at most 500 distinct delegation IDs")
    if not delegation_ids:
        return " AND 0", ()
    return " AND d.id IN (" + ",".join("?" for _ in delegation_ids) + ")", delegation_ids


def _owned_cleanup_row(
    db: sqlite3.Connection,
    request: Request,
    delegation_id: str,
) -> sqlite3.Row | None:
    owner_filter, owner_parameters = _cleanup_owner_filter(request)
    row: sqlite3.Row | None = db.execute(
        "SELECT d.*, s.workspace_id AS cleanup_parent_workspace, w.repo_id AS cleanup_repo_id, "
        "w.path AS cleanup_parent_path, r.root_path AS cleanup_repo_path "
        + _OWNED_CLEANUP_FROM
        + owner_filter
        + "AND d.id = ?",
        (*owner_parameters, delegation_id),
    ).fetchone()
    return row


def _pending_owned_cleanup_page(
    db: sqlite3.Connection,
    request: Request,
    parent_session_id: str | None,
    delegation_ids: tuple[str, ...] | None,
) -> list[tuple[str, float]]:
    owner_filter, owner_parameters = _cleanup_owner_filter(request)
    selection, identifiers = _cleanup_selection(delegation_ids)
    rows = db.execute(
        "SELECT d.id, MAX(COALESCE(julianday(d.updated_at), 0)) OVER () AS retry_after "
        + _OWNED_CLEANUP_FROM
        + owner_filter
        + "AND (? IS NULL OR d.parent_session_id = ?) "
        + selection
        + " ORDER BY COALESCE(julianday(d.updated_at), 0), d.id LIMIT 128",
        (*owner_parameters, parent_session_id, parent_session_id, *identifiers),
    ).fetchall()
    return [(str(row["id"]), float(row["retry_after"])) for row in rows]


async def cleanup_provisioning_artifacts(
    request: Request,
    delegation_id: str,
    *,
    authorization_guard: Callable[[sqlite3.Connection], None] | None = None,
    retry_after: float = 0.0,
) -> bool:
    """Clean recorded terminal ownership and retain incomplete cleanup for retry."""
    workspace_service = ThreadWorkspaceService()
    try:
        loaded = await run_db_operation_for_request(
            request,
            lambda db: _owned_cleanup_row(db, request, delegation_id),
        )
        if loaded is None:
            return True
        original = loaded
        namespace = str(original["git_artifact_namespace"])

        def matches(row: sqlite3.Row | None) -> bool:
            return row is not None and all(
                row[key] == original[key] for key in _CLEANUP_MATCH_FIELDS
            )

        def record_attempt(db: sqlite3.Connection) -> bool:
            db.execute("BEGIN IMMEDIATE")
            try:
                if authorization_guard is not None:
                    authorization_guard(db)
                if not matches(_owned_cleanup_row(db, request, delegation_id)):
                    db.rollback()
                    return False
                db.execute(
                    "UPDATE thread_delegations SET updated_at = strftime('%Y-%m-%d %H:%M:%f', "
                    "MAX(julianday('now'), COALESCE(julianday(updated_at), 0), ?) + 1.0 / 86400000.0) "
                    "WHERE id = ? AND git_artifact_namespace = ? AND git_artifacts_claimed = 1",
                    (retry_after, delegation_id, namespace),
                )
                db.commit()
                return True
            except BaseException:
                db.rollback()
                raise

        if not await run_db_operation_for_request(request, record_attempt):
            return True
        context = await run_db_operation_for_request(
            request,
            lambda db: workspace_service.load_parent_context(
                db,
                get_tenant(request),
                parent_workspace_id=str(original["cleanup_parent_workspace"]),
                delegation_id=delegation_id,
            ),
        )

        def validate(db: sqlite3.Connection) -> bool:
            db.execute("BEGIN IMMEDIATE")
            try:
                if authorization_guard is not None:
                    authorization_guard(db)
                return matches(_owned_cleanup_row(db, request, delegation_id))
            finally:
                db.rollback()

        def release(db: sqlite3.Connection) -> None:
            db.execute("BEGIN IMMEDIATE")
            try:
                if authorization_guard is not None:
                    authorization_guard(db)
                if not matches(_owned_cleanup_row(db, request, delegation_id)):
                    raise ThreadGitOwnershipError()
                changed = db.execute(
                    "UPDATE thread_delegations SET git_artifacts_claimed = 0, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND git_artifact_namespace = ? AND git_artifacts_claimed = 1",
                    (delegation_id, namespace),
                )
                if changed.rowcount != 1:
                    raise ThreadGitOwnershipError()
                db.commit()
            except BaseException:
                db.rollback()
                raise

        async def validate_claim() -> bool:
            return await run_db_operation_for_request(request, validate)

        async def release_claim() -> None:
            await run_db_operation_for_request(request, release)

        ownership = ThreadGitCleanup(
            request_database_identity(request),
            namespace,
            validate_claim,
            release_claim,
        )
        staged = ThreadStagedChildGit(
            base_kind=str(original["base_kind"] or "head"),
            base_commit=str(original["base_commit"] or ""),
            snapshot_ref=(
                None if original["snapshot_ref"] is None else str(original["snapshot_ref"])
            ),
            snapshot_published=original["snapshot_ref"] is not None,
        )
        await workspace_service.discard_staged_child_git_artifacts(
            context, staged, ownership=ownership
        )
        return True
    except Exception as error:
        logger.warning("Thread artifact cleanup remains pending (%s)", type(error).__name__)
        return False


def _claim_stale_provisioning(
    db: sqlite3.Connection,
    stale_seconds: int,
    parent_session_id: str | None = None,
    authorization_guard: Callable[[sqlite3.Connection], None] | None = None,
    *,
    delegation_ids: tuple[str, ...] | None = None,
) -> list[str]:
    """Atomically interrupt selected stale reservations without reopening winners."""
    selection, identifiers = _cleanup_selection(delegation_ids)
    if delegation_ids is not None and not delegation_ids:
        return []
    threshold = f"-{int(stale_seconds)} seconds"
    db.execute("BEGIN IMMEDIATE")
    try:
        if authorization_guard is not None:
            authorization_guard(db)
        rows = db.execute(
            "SELECT d.id FROM thread_delegations d "
            "WHERE d.status = ? AND d.child_session_id IS NULL "
            "AND d.updated_at < datetime('now', ?) "
            "AND (? IS NULL OR d.parent_session_id = ?) "
            + selection
            + " ORDER BY d.created_at, d.id",
            (
                DELEGATION_STATUS_PROVISIONING,
                threshold,
                parent_session_id,
                parent_session_id,
                *identifiers,
            ),
        ).fetchall()
        claimed: list[str] = []
        for row in rows:
            delegation_id = str(row["id"])
            result = db.execute(
                """UPDATE thread_delegations
                   SET status = ?, completed_at = CURRENT_TIMESTAMP,
                       error_code = ?, error_detail_safe = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = ? AND child_session_id IS NULL
                     AND updated_at < datetime('now', ?)""",
                (
                    DELEGATION_STATUS_INTERRUPTED,
                    _PROVISIONING_STALE_ERROR_CODE,
                    _PROVISIONING_STALE_SAFE_DETAIL,
                    delegation_id,
                    DELEGATION_STATUS_PROVISIONING,
                    threshold,
                ),
            )
            if result.rowcount == 1:
                claimed.append(delegation_id)
        db.commit()
        return claimed
    except BaseException:
        db.rollback()
        raise


async def reconcile_stale_provisioning(
    request: Request,
    *,
    parent_session_id: str | None = None,
    authorization_guard: Callable[[sqlite3.Connection], None] | None = None,
    delegation_ids: tuple[str, ...] | None = None,
) -> None:
    """Interrupt eligible reservations and retry one scoped cleanup page.

    Explicit selections restrict interruption and cleanup. Empty selections do
    no work. Authorization callbacks run inside short writer transactions and
    must not await, perform external I/O, or retain the connection.
    """
    if delegation_ids is not None and not delegation_ids:
        return
    _cleanup_selection(delegation_ids)
    stale_seconds = get_settings().thread_provisioning_stale_seconds
    claimed = await run_db_operation_for_request(
        request,
        lambda db: _claim_stale_provisioning(
            db,
            stale_seconds,
            parent_session_id,
            authorization_guard,
            delegation_ids=delegation_ids,
        ),
    )
    pending = await run_db_operation_for_request(
        request,
        lambda db: _pending_owned_cleanup_page(db, request, parent_session_id, delegation_ids),
    )
    for delegation_id, retry_after in pending:
        await cleanup_provisioning_artifacts(
            request,
            delegation_id,
            authorization_guard=authorization_guard,
            retry_after=retry_after,
        )
    if claimed:
        logger.info(
            "Reconciled %d stale provisioning delegation(s): %s",
            len(claimed),
            ",".join(claimed),
        )
