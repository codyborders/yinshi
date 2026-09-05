"""Stale provisioning reconciliation for Phase 3 orchestration writes.

A spawn process that dies between reserving a delegation and attaching its
child leaves the reservation in ``provisioning`` forever. Before any Phase 3
write the orchestration layer reconciles the request's database: every
provisioning reservation untouched for longer than the configured threshold
is claimed for one requesting writer with a single atomic CAS into
``interrupted``, and only the winner removes the reservation's staged Git
artifacts. The claim doubles as attach prevention: a late attach requires the
``provisioning`` status and loses once the row is interrupted.

Reconciliation is deliberately request-scoped. Each call touches only the
request's own database (the tenant database in multi-tenant mode), runs no
background loop, and is safe to repeat: a second call finds no claimable rows
because ``interrupted`` is terminal.
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import Request

from yinshi.api.deps import get_tenant, run_db_operation_for_request
from yinshi.config import get_settings
from yinshi.services.thread_lifecycle import (
    DELEGATION_STATUS_INTERRUPTED,
    DELEGATION_STATUS_PROVISIONING,
)
from yinshi.services.thread_workspaces import (
    ThreadParentGitContext,
    ThreadStagedChildGit,
    ThreadWorkspaceService,
)

logger = logging.getLogger(__name__)

_PROVISIONING_STALE_ERROR_CODE = "provisioning_stale"
_PROVISIONING_STALE_SAFE_DETAIL = "stale provisioning was interrupted before completion"

# An interrupted reservation owns every artifact named by its delegation ID,
# so cleanup deletes the staged worktree, child branch, and published snapshot
# ref while result refs stay untouched.
_STALE_PROVISIONING_STAGED = ThreadStagedChildGit(
    base_kind="head",
    base_commit="",
    snapshot_ref=None,
    snapshot_published=True,
)


def _claim_stale_provisioning(
    db: sqlite3.Connection,
    stale_seconds: int,
) -> list[str]:
    """CAS every stale provisioning reservation to interrupted in one transaction.

    Returns the claimed delegation IDs in stable order. The conditional
    update keeps the decision atomic with the staleness observation, so
    exactly one concurrent reconciler can win each row and a row that
    advanced before the transaction opened is never claimed.
    """
    threshold = f"-{int(stale_seconds)} seconds"
    db.execute("BEGIN IMMEDIATE")
    try:
        rows = db.execute(
            """SELECT id FROM thread_delegations
               WHERE status = ? AND child_session_id IS NULL
                 AND updated_at < datetime('now', ?)
               ORDER BY created_at, id""",
            (DELEGATION_STATUS_PROVISIONING, threshold),
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


def _load_stale_contexts(
    db: sqlite3.Connection,
    request: Request,
    delegation_ids: list[str],
) -> dict[str, ThreadParentGitContext | None]:
    """Reopen the database only to confirm each claim and resolve its context.

    The interrupted status and safe code are re-read so cleanup runs only for
    rows this reconciler actually claimed. A missing parent session or a
    failed context load leaves nothing safely cleanable and is skipped.
    """
    workspace_service = ThreadWorkspaceService()
    contexts: dict[str, ThreadParentGitContext | None] = {}
    for delegation_id in delegation_ids:
        row = db.execute(
            """SELECT status, error_code, parent_session_id, child_session_id
               FROM thread_delegations WHERE id = ?""",
            (delegation_id,),
        ).fetchone()
        if (
            row is None
            or str(row["status"]) != DELEGATION_STATUS_INTERRUPTED
            or str(row["error_code"]) != _PROVISIONING_STALE_ERROR_CODE
            or row["child_session_id"] is not None
        ):
            contexts[delegation_id] = None
            continue
        parent = db.execute(
            "SELECT workspace_id FROM sessions WHERE id = ?",
            (str(row["parent_session_id"]),),
        ).fetchone()
        if parent is None:
            contexts[delegation_id] = None
            continue
        try:
            contexts[delegation_id] = workspace_service.load_parent_context(
                db,
                get_tenant(request),
                parent_workspace_id=str(parent["workspace_id"]),
                delegation_id=delegation_id,
            )
        except Exception as context_error:
            logger.warning(
                "Stale provisioning context load failed for delegation %s with %s",
                delegation_id,
                type(context_error).__name__,
            )
            contexts[delegation_id] = None
    return contexts


async def reconcile_stale_provisioning(request: Request) -> None:
    """Reconcile stale provisioning reservations for the request's database.

    The claim transaction closes before any Git subprocess runs, matching the
    no-database-during-Git invariant; the database reopens only to confirm
    each claim and resolve its parent Git context, then closes again for the
    connection-free Phase 2 cleanup. Artifact cleanup failures are logged for
    later maintenance and never propagate: the interrupted claim is the
    durable decision, and the triggering Phase 3 write must proceed.
    """
    stale_seconds = get_settings().thread_provisioning_stale_seconds
    claimed = await run_db_operation_for_request(
        request,
        lambda db: _claim_stale_provisioning(db, stale_seconds),
    )
    if not claimed:
        return
    contexts = await run_db_operation_for_request(
        request,
        lambda db: _load_stale_contexts(db, request, claimed),
    )
    workspace_service = ThreadWorkspaceService()
    for delegation_id, context in contexts.items():
        if context is None:
            continue
        try:
            await workspace_service.discard_staged_child_git_artifacts(
                context,
                _STALE_PROVISIONING_STAGED,
            )
        except Exception as cleanup_error:
            logger.warning(
                "Stale provisioning cleanup failed for delegation %s with %s",
                delegation_id,
                type(cleanup_error).__name__,
            )
    logger.info(
        "Reconciled %d stale provisioning delegation(s): %s",
        len(claimed),
        ",".join(claimed),
    )
