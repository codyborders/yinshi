"""Write-side coordination for manual child thread creation.

Phase 3 of the thread orchestration plan. This service owns the spawn
reservation: one canonical idempotency key reserves exactly one
``provisioning`` delegation inside a ``BEGIN IMMEDIATE`` transaction, which
also atomically enforces parent authorization and the configured tree
limits. Child workspace provisioning, child session attachment, prompt
scheduling, cancellation, retry, reconciliation, and results arrive in
later passes, so a successful spawn currently ends at the queued
outcome and a replay returns the stored reservation unchanged.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast

from fastapi import Request

from yinshi.api.deps import (
    get_tenant,
    get_user_email,
    run_db_operation_for_request,
)
from yinshi.exceptions import YinshiError
from yinshi.model_catalog import normalize_model_ref
from yinshi.models import ThreadChildCreate, ThreadResultReportCreate, ThreadRetryCreate
from yinshi.services.prompt_journal import PromptJournal, PromptRun, PromptRunNotFoundError
from yinshi.services.thread_lifecycle import (
    DELEGATION_STATUS_CANCELLED,
    DELEGATION_STATUS_CANCELLING,
    DELEGATION_STATUS_COMPLETED,
    DELEGATION_STATUS_FAILED,
    DELEGATION_STATUS_INTERRUPTED,
    DELEGATION_STATUS_PROVISIONING,
    DELEGATION_STATUS_QUEUED,
    DELEGATION_STATUS_RUNNING,
    TERMINAL_DELEGATION_STATUSES,
    initial_run_idempotency_key,
)
from yinshi.services.thread_queries import (
    ThreadNotFoundError,
    get_thread,
    get_thread_limits,
)
from yinshi.services.thread_reconciliation import reconcile_stale_provisioning
from yinshi.services.thread_workspaces import (
    FinalizedThreadGitResult,
    ThreadParentGitContext,
    ThreadStagedChildGit,
    ThreadWorkspaceService,
)
from yinshi.services.workspace_files import ChangedFile

logger = logging.getLogger(__name__)

_PROVISION_FAILED_ERROR_CODE = "provision_failed"
_PROVISION_FAILED_SAFE_DETAIL = "child workspace provisioning failed"
_START_FAILED_ERROR_CODE = "start_failed"
_START_FAILED_SAFE_DETAIL = "initial prompt run failed to start"
_PROMPT_CHAR_BUDGET = 100_000


def _report_tests_json(body: ThreadResultReportCreate) -> str:
    """Serialize the report's strict test entries in canonical form."""
    tests = [
        {"command": test.command, "status": test.status, "summary": test.summary}
        for test in body.tests
    ]
    return json.dumps(tests, separators=(",", ":"))


def _report_warnings_json(body: ThreadResultReportCreate) -> str:
    """Serialize the report's bounded warning strings in canonical form."""
    return json.dumps(list(body.warnings), separators=(",", ":"))


def _report_incoming_payload(body: ThreadResultReportCreate) -> dict[str, Any]:
    """Return the normalized incoming payload for exact replay comparison."""
    return {
        "summary": body.summary.strip(),
        "tests": [
            {"command": test.command, "status": test.status, "summary": test.summary}
            for test in body.tests
        ],
        "warnings": list(body.warnings),
    }


def _report_stored_payload(row: sqlite3.Row) -> dict[str, Any]:
    """Return the stored row's payload in the same normalized shape."""
    tests = json.loads(str(row["tests_json"]))
    warnings = json.loads(str(row["warnings_json"]))
    return {
        "summary": "" if row["summary"] is None else str(row["summary"]).strip(),
        "tests": [
            {
                "command": str(test.get("command", "")),
                "status": str(test.get("status", "")),
                "summary": test.get("summary"),
            }
            for test in tests
        ],
        "warnings": [str(warning) for warning in warnings],
    }


def _report_canonical(payload: dict[str, Any]) -> str:
    """Return one deterministic JSON text for payload comparison."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _changed_files_json(changed_files: tuple[ChangedFile, ...]) -> str:
    """Serialize finalized changed files as one compact JSON list."""
    entries = [
        {
            "path": item.path,
            "status": item.status,
            "kind": item.kind,
            "original_path": item.original_path,
        }
        for item in changed_files
    ]
    return json.dumps(entries, separators=(",", ":"))


def _sealed_git_fields_match(
    row: sqlite3.Row,
    finalized: FinalizedThreadGitResult,
) -> bool:
    """Return whether the stored sealed Git fields match this attempt."""
    return (
        (row["base_commit"] or "") == finalized.base_commit
        and (row["result_commit"] or "") == finalized.result_commit
        and (row["result_ref"] or "") == finalized.result_ref
    )


def _project_result_row(row: sqlite3.Row) -> dict[str, Any]:
    """Project one stored thread_results row as the public result shape."""
    return {
        "delegation_id": str(row["delegation_id"]),
        "version": int(row["version"]),
        "source": str(row["source"]),
        "sealed": bool(row["sealed"]),
        "summary": row["summary"],
        "tests": json.loads(str(row["tests_json"])),
        "warnings": json.loads(str(row["warnings_json"])),
        "base_commit": row["base_commit"],
        "result_commit": row["result_commit"],
        "result_ref": row["result_ref"],
        "changed_files": json.loads(str(row["changed_files_json"])),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "sealed_at": row["sealed_at"],
    }


# Durable prompt-run terminal states adopt the delegation directly, where a
# completed run always beats a losing cancellation.
_RUN_STATUS_TO_DELEGATION_STATUS = {
    "completed": DELEGATION_STATUS_COMPLETED,
    "failed": DELEGATION_STATUS_FAILED,
    "cancelled": DELEGATION_STATUS_CANCELLED,
    "interrupted": DELEGATION_STATUS_INTERRUPTED,
}

# A cancelled reservation owns every artifact named by its delegation ID, so
# cleanup deletes the staged worktree, child branch, and published snapshot
# ref while result refs and attached winners stay untouched.
_CANCELLED_PROVISIONING_STAGED = ThreadStagedChildGit(
    base_kind="head",
    base_commit="",
    snapshot_ref=None,
    snapshot_published=True,
)


def build_initial_prompt(
    *,
    title: str,
    role: str,
    task: str,
    context: str | None,
) -> str:
    """Build one bounded deterministic manual prompt for a new child thread.

    Sections appear in a fixed order so replayed spawns and tests observe
    identical content. The result never exceeds the budget, matching the
    prompt route's 100,000-character input bound.
    """
    sections = [f"Role: {role}", "", f"# {title}", "", "## Task", task]
    if context:
        sections.extend(["", "## Context", context])
    return "\n".join(sections)[:_PROMPT_CHAR_BUDGET]


@dataclass(frozen=True, slots=True)
class ThreadSpawnOutcome:
    """Stable spawn result for one idempotency key.

    ``status`` mirrors the stored delegation status. ``child_session_id`` is
    set once the delegation owns a child session. ``error_code`` carries the
    stored safe failure code so a failed reservation replays stably.
    """

    delegation_id: str
    status: str
    child_session_id: str | None
    error_code: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ThreadSpawnOutcome:
        """Project one delegation row as a stable spawn outcome."""
        child_session_id = row["child_session_id"]
        error_code = row["error_code"]
        return cls(
            delegation_id=str(row["id"]),
            status=str(row["status"]),
            child_session_id=None if child_session_id is None else str(child_session_id),
            error_code=None if error_code is None else str(error_code),
        )


class ThreadOrchestrationError(YinshiError):
    """Base class for orchestration failures carrying one safe error code."""

    def __init__(self, code: str, message: str) -> None:
        assert code, "safe error code must not be empty"
        super().__init__(message)
        self.code = code


class ThreadHierarchyDisabledError(ThreadOrchestrationError):
    """Raised when the thread hierarchy feature flag is disabled."""

    def __init__(self, message: str = "thread hierarchy is disabled") -> None:
        super().__init__("thread_hierarchy_disabled", message)


class ThreadParentNotAuthorizedError(ThreadOrchestrationError):
    """Raised when the authenticated caller does not own the parent session."""

    def __init__(self, message: str = "parent session is not owned by this user") -> None:
        super().__init__("parent_not_authorized", message)


class ThreadDepthLimitError(ThreadOrchestrationError):
    """Raised when the spawn would exceed the configured maximum depth."""

    def __init__(self, message: str = "thread depth limit exceeded") -> None:
        super().__init__("depth_exceeded", message)


class ThreadChildLimitError(ThreadOrchestrationError):
    """Raised when the parent already holds the configured direct-child maximum."""

    def __init__(self, message: str = "thread direct-child limit exceeded") -> None:
        super().__init__("child_limit_exceeded", message)


class ThreadActiveDescendantsLimitError(ThreadOrchestrationError):
    """Raised when the root tree already holds the active-descendant maximum."""

    def __init__(self, message: str = "thread active-descendant limit exceeded") -> None:
        super().__init__("active_thread_limit_exceeded", message)


class ThreadTreeLimitError(ThreadOrchestrationError):
    """Raised when the root tree already holds the total-thread maximum."""

    def __init__(self, message: str = "thread tree size limit exceeded") -> None:
        super().__init__("tree_limit_exceeded", message)


class ThreadIdempotencyConflictError(ThreadOrchestrationError):
    """Raised when one idempotency key is reused with a different request."""

    def __init__(self, message: str = "idempotency key was already used") -> None:
        super().__init__("idempotency_key_conflict", message)


class ThreadRetryNotAllowedError(ThreadOrchestrationError):
    """Raised when the delegation status does not qualify for retry."""

    def __init__(self, message: str = "thread retry is not available for this status") -> None:
        super().__init__("retry_not_allowed", message)


class ThreadAttachConflictError(ThreadOrchestrationError):
    """Raised when the attach loses the reservation to a concurrent writer."""

    def __init__(self, message: str = "child attach lost the reservation") -> None:
        super().__init__("attach_conflict", message)


class ThreadPromptStartError(ThreadOrchestrationError):
    """Raised when the child's initial prompt run could not be accepted."""

    def __init__(self, message: str = "initial prompt run failed to start") -> None:
        super().__init__("start_failed", message)


class ThreadResultVersionConflictError(ThreadOrchestrationError):
    """Raised when one report carries a stale version with a changed payload."""

    def __init__(self, message: str = "thread result draft version conflict") -> None:
        super().__init__("result_version_conflict", message)


class ThreadResultSealedError(ThreadOrchestrationError):
    """Raised when a report targets a sealed, immutable result row."""

    def __init__(self, message: str = "thread result is sealed") -> None:
        super().__init__("result_sealed", message)


class ThreadResultNotSealableError(ThreadOrchestrationError):
    """Raised when the delegation is not in a terminal, sealable state."""

    def __init__(self, message: str = "thread result is not sealable yet") -> None:
        super().__init__("result_not_terminal", message)


class ThreadResultDraftMissingError(ThreadOrchestrationError):
    """Raised when sealing finds no existing reported draft."""

    def __init__(self, message: str = "no reported thread result draft exists") -> None:
        super().__init__("result_draft_missing", message)


class ThreadResultSealConflictError(ThreadOrchestrationError):
    """Raised when stored seal identity or Git metadata no longer matches."""

    def __init__(self, message: str = "thread result seal state conflicts") -> None:
        super().__init__("result_seal_conflict", message)


class ThreadOrchestrationService:
    """Coordinate the manual child-thread lifecycle."""

    async def spawn_child(
        self,
        request: Request,
        *,
        parent_session_id: str,
        body: ThreadChildCreate,
        retry_of_delegation_id: str | None = None,
    ) -> ThreadSpawnOutcome:
        """Reserve one manual child thread and attach its queued child.

        ``retry_of_delegation_id`` records retry lineage on the reservation
        atomically and is reserved for the retry flow; manual spawns omit it.
        """
        if not isinstance(parent_session_id, str) or not parent_session_id.strip():
            raise ValueError("parent_session_id must be a non-empty string")
        from yinshi.config import get_settings

        if not get_settings().thread_hierarchy_enabled:
            raise ThreadHierarchyDisabledError()
        # Reconciliation precedes every Phase 3 write. Repeated calls are
        # harmless: a second pass claims nothing because interrupted is
        # terminal. Retry re-enters here after its own reconciliation pass.
        await reconcile_stale_provisioning(request)
        idempotency_key = _canonical_idempotency_key(body.idempotency_key)
        workspace_service = ThreadWorkspaceService()
        reservation = await run_db_operation_for_request(
            request,
            lambda db: self._reserve(
                db,
                request,
                parent_session_id,
                idempotency_key,
                body,
                retry_of_delegation_id,
            ),
        )
        outcome = ThreadSpawnOutcome.from_row(reservation)
        if outcome.status != DELEGATION_STATUS_PROVISIONING:
            # Replay: never provision the same delegation a second time.
            return outcome

        def load_context(db: sqlite3.Connection) -> ThreadParentGitContext:
            row = db.execute(
                "SELECT workspace_id FROM sessions WHERE id = ?",
                (parent_session_id,),
            ).fetchone()
            if row is None:
                raise ThreadNotFoundError(parent_session_id)
            return workspace_service.load_parent_context(
                db,
                get_tenant(request),
                parent_workspace_id=str(row["workspace_id"]),
                delegation_id=outcome.delegation_id,
            )

        context = await run_db_operation_for_request(request, load_context)
        try:
            staged = await workspace_service.create_child_git_artifacts(context)
        except Exception:
            # Git creation failed. Mark the reservation failed in one short
            # transaction with sanitized fields, then surface the original
            # domain/Git error unchanged as the service contract requires.
            await self._fail_reservation_best_effort(request, outcome.delegation_id)
            raise

        def attach(db: sqlite3.Connection) -> sqlite3.Row:
            db.execute("BEGIN IMMEDIATE")
            try:
                workspace_cursor = db.execute(
                    """INSERT INTO workspaces (repo_id, name, branch, path, state, kind,
                                               parent_workspace_id)
                       VALUES (?, ?, ?, ?, 'ready', 'delegated', ?)""",
                    (
                        context.repo_id,
                        context.branch,
                        context.branch,
                        context.worktree_path,
                        context.parent_workspace_id,
                    ),
                )
                workspace_row = db.execute(
                    "SELECT id FROM workspaces WHERE rowid = ?",
                    (workspace_cursor.lastrowid,),
                ).fetchone()
                assert workspace_row is not None
                workspace_id = str(workspace_row["id"])
                session_cursor = db.execute(
                    "INSERT INTO sessions (workspace_id, title) VALUES (?, ?)",
                    (workspace_id, body.title.strip()),
                )
                session_row = db.execute(
                    "SELECT id FROM sessions WHERE rowid = ?",
                    (session_cursor.lastrowid,),
                ).fetchone()
                assert session_row is not None
                session_id = str(session_row["id"])
                claim = db.execute(
                    """UPDATE thread_delegations
                       SET child_workspace_id = ?, child_session_id = ?,
                           base_kind = ?, base_commit = ?, snapshot_ref = ?,
                           status = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status = ?""",
                    (
                        workspace_id,
                        session_id,
                        staged.base_kind,
                        staged.base_commit,
                        staged.snapshot_ref,
                        DELEGATION_STATUS_QUEUED,
                        outcome.delegation_id,
                        DELEGATION_STATUS_PROVISIONING,
                    ),
                )
                if claim.rowcount != 1:
                    # A concurrent writer claimed the reservation. Committing
                    # would overwrite its decision, so roll the attach back.
                    raise ThreadAttachConflictError()
                attached = cast(
                    sqlite3.Row,
                    db.execute(
                        "SELECT * FROM thread_delegations WHERE id = ?",
                        (outcome.delegation_id,),
                    ).fetchone(),
                )
                db.commit()
                return attached
            except BaseException:
                db.rollback()
                raise

        try:
            attached = await run_db_operation_for_request(request, attach)
        except BaseException:
            await self._discard_failed_attach(
                request,
                workspace_service,
                context,
                staged,
                outcome.delegation_id,
            )
            await self._fail_reservation_best_effort(request, outcome.delegation_id)
            raise
        outcome = ThreadSpawnOutcome.from_row(attached)
        if not body.start_immediately or outcome.status != DELEGATION_STATUS_QUEUED:
            return outcome
        return await self._start_initial_prompt(request, outcome, body)

    _RETRY_ALLOWED_STATUSES = frozenset(
        {
            DELEGATION_STATUS_FAILED,
            DELEGATION_STATUS_CANCELLED,
            DELEGATION_STATUS_INTERRUPTED,
        }
    )

    async def retry_child(
        self,
        request: Request,
        *,
        child_session_id: str,
        body: ThreadRetryCreate,
    ) -> ThreadSpawnOutcome:
        """Retry one terminal delegated child as a fresh lineage child.

        Only failed, cancelled, or interrupted delegated children qualify;
        every other status is rejected with one safe code. The new child is
        a full spawn under the original parent: it copies the stored title,
        task, context, role, and model choices unless overridden, forces
        ``start_immediately``, and records ``retry_of_delegation_id`` on the
        new reservation atomically. The same retry key replays to the same
        new child, and the original child's resources and result stay
        untouched.
        """
        if not isinstance(child_session_id, str) or not child_session_id.strip():
            raise ValueError("child_session_id must be a non-empty string")
        from yinshi.config import get_settings

        if not get_settings().thread_hierarchy_enabled:
            raise ThreadHierarchyDisabledError()
        # Reconciliation precedes every Phase 3 write. The spawn flow below
        # reconciles again; the second pass claims nothing because interrupted
        # rows are terminal, so re-entry never recurses or double-cleans.
        await reconcile_stale_provisioning(request)

        def resolve(db: sqlite3.Connection) -> sqlite3.Row:
            return self._load_child_delegation(db, request, child_session_id)

        delegation = await run_db_operation_for_request(request, resolve)
        status = str(delegation["status"])
        if status not in self._RETRY_ALLOWED_STATUSES:
            raise ThreadRetryNotAllowedError(
                "thread retry requires a failed, cancelled, or interrupted child",
            )
        stored_model = str(delegation["requested_model"] or "")
        stored_thinking = delegation["requested_thinking"]
        retry_body = ThreadChildCreate(
            idempotency_key=body.idempotency_key,
            title=str(delegation["title"]),
            task=str(delegation["task"]),
            context=None if delegation["context"] is None else str(delegation["context"]),
            role=cast(
                Literal["general", "research", "implementation", "test", "review", "debug"],
                str(delegation["role"]),
            ),
            model=body.model if body.model is not None else stored_model,
            thinking=(
                body.thinking
                if body.thinking is not None
                else (None if stored_thinking is None else str(stored_thinking))
            ),
            start_immediately=True,
        )
        return await self.spawn_child(
            request,
            parent_session_id=str(delegation["parent_session_id"]),
            body=retry_body,
            retry_of_delegation_id=str(delegation["id"]),
        )

    async def cancel_child(
        self,
        request: Request,
        *,
        thread_id: str,
    ) -> ThreadSpawnOutcome:
        """Cancel one delegated child thread and return its stable outcome.

        ``thread_id`` accepts an attached child session ID or, before the
        child attaches, the provisioning delegation ID. The child session is
        authorized like the read API first, so unknown and foreign threads
        hide behind the same not-found error. Queued children lose their
        reservation atomically while every attached resource stays in place,
        provisioning claims clean only their own staged artifacts, and
        repeats return the stored outcome.
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id must be a non-empty string")
        from yinshi.config import get_settings

        if not get_settings().thread_hierarchy_enabled:
            raise ThreadHierarchyDisabledError()
        # Reconciliation precedes every Phase 3 write (repeated calls are
        # harmless and claim nothing once rows are terminal).
        await reconcile_stale_provisioning(request)
        row = await run_db_operation_for_request(
            request,
            lambda db: self._resolve_cancel_target(db, request, thread_id),
        )
        delegation_id = str(row["id"])
        for _ in range(4):
            delegation_id = str(row["id"])
            status = str(row["status"])
            if status == DELEGATION_STATUS_PROVISIONING:
                await self._cancel_provisioning(request, delegation_id)
                row = await self._reload_delegation(request, delegation_id)
                continue
            if status == DELEGATION_STATUS_QUEUED:
                await self._cancel_queued(request, delegation_id)
                row = await self._reload_delegation(request, delegation_id)
                continue
            if status in (DELEGATION_STATUS_RUNNING, DELEGATION_STATUS_CANCELLING):
                outcome = await self._cancel_running(
                    request,
                    delegation_id,
                    str(row["child_session_id"]),
                    already_cancelling=status == DELEGATION_STATUS_CANCELLING,
                )
                if outcome is not None:
                    return outcome
                row = await self._reload_delegation(request, delegation_id)
                continue
            # Terminal statuses, including an adopted cancellation outcome
            # and a provisioning reservation another writer advanced, repeat
            # stably without touching the stored decision.
            return ThreadSpawnOutcome.from_row(row)
        return ThreadSpawnOutcome.from_row(await self._reload_delegation(request, delegation_id))

    def _resolve_cancel_target(
        self,
        db: sqlite3.Connection,
        request: Request,
        thread_id: str,
    ) -> sqlite3.Row:
        """Resolve one cancel target as an attached child or provisioning row.

        The child session wins when ``thread_id`` names one, keeping the
        established session authorization and delegation lookup. Otherwise
        the thread ID names a provisioning delegation, which is authorized
        through its parent session before any detail is revealed. A
        delegation that already advanced keeps its winner decision and is
        returned unchanged.
        """
        session = db.execute(
            "SELECT id FROM sessions WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if session is not None:
            return self._load_child_delegation(db, request, thread_id)
        row = db.execute(
            "SELECT * FROM thread_delegations WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise ThreadNotFoundError(thread_id)
        parent_session_id = str(row["parent_session_id"])
        _authorize_session(db, request, parent_session_id)
        return cast(sqlite3.Row, row)

    async def _cancel_running(
        self,
        request: Request,
        delegation_id: str,
        child_session_id: str,
        *,
        already_cancelling: bool,
    ) -> ThreadSpawnOutcome | None:
        """Drive one running child to its stable cancellation outcome.

        The cancelling claim is a conditional update, so a run that finished
        first keeps its terminal decision. The journal cancel call happens
        between short transactions, never inside one, because the journal
        opens its own tenant-scoped connections. The delegation then adopts
        the durable prompt-run state, where completion beats cancellation.
        """
        if not already_cancelling:
            rowcount = await run_db_operation_for_request(
                request,
                lambda db: self._claim_cancelling(db, delegation_id),
            )
            if rowcount != 1:
                return None
        run = await self._cancel_child_prompt_run(request, delegation_id, child_session_id)
        target = None if run is None else _RUN_STATUS_TO_DELEGATION_STATUS.get(str(run.status))
        if target is not None:
            await run_db_operation_for_request(
                request,
                lambda db: self._adopt_terminal_cancellation(db, delegation_id, target),
            )
        return ThreadSpawnOutcome.from_row(await self._reload_delegation(request, delegation_id))

    @staticmethod
    def _claim_cancelling(db: sqlite3.Connection, delegation_id: str) -> int:
        """CAS one running delegation to cancelling inside one transaction."""
        db.execute("BEGIN IMMEDIATE")
        try:
            result = db.execute(
                """UPDATE thread_delegations
                   SET status = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = ?""",
                (
                    DELEGATION_STATUS_CANCELLING,
                    delegation_id,
                    DELEGATION_STATUS_RUNNING,
                ),
            )
            db.commit()
            return int(result.rowcount)
        except BaseException:
            db.rollback()
            raise

    @staticmethod
    def _adopt_terminal_cancellation(
        db: sqlite3.Connection,
        delegation_id: str,
        target: str,
    ) -> None:
        """Resolve one cancelling delegation to the durable run outcome."""
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                """UPDATE thread_delegations
                   SET status = ?, completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = ?""",
                (target, delegation_id, DELEGATION_STATUS_CANCELLING),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

    async def _cancel_child_prompt_run(
        self,
        request: Request,
        delegation_id: str,
        child_session_id: str,
    ) -> PromptRun | None:
        """Stop the child's deterministic initial run with no held transaction.

        The run is located by the delegation-derived idempotency key, and the
        journal's own durable cancel flow decides the final run state. A run
        that never started, vanished, or failed to stop leaves the delegation
        in cancelling so a repeat can converge without destroying state.
        """
        journal = getattr(request.app.state, "prompt_journal", None)
        if not isinstance(journal, PromptJournal):
            logger.warning(
                "Thread delegation %s has no prompt journal; cancellation stays pending",
                delegation_id,
            )
            return None
        run_key = initial_run_idempotency_key(delegation_id)

        def find_run(db: sqlite3.Connection) -> str | None:
            row = db.execute(
                """SELECT id FROM prompt_runs
                   WHERE session_id = ? AND idempotency_key = ?""",
                (child_session_id, run_key),
            ).fetchone()
            return None if row is None else str(row["id"])

        run_id = await run_db_operation_for_request(request, find_run)
        if run_id is None:
            logger.warning(
                "Thread delegation %s has no initial prompt run; cancellation stays pending",
                delegation_id,
            )
            return None
        try:
            return await journal.cancel(
                request=request,
                session_id=child_session_id,
                run_id=run_id,
            )
        except PromptRunNotFoundError:
            logger.warning(
                "Thread delegation %s prompt run vanished before cancellation",
                delegation_id,
            )
            return None
        except Exception as exc:
            logger.warning(
                "Thread delegation %s prompt cancel failed with %s",
                delegation_id,
                type(exc).__name__,
            )
            return None

    async def seal_result(
        self,
        request: Request,
        *,
        child_session_id: str,
    ) -> dict[str, Any]:
        """Seal one reported result draft with an immutable Git publication.

        The first database operation authorizes the delegated child and
        captures the immutable finalization context, then the connection
        closes before any Git or repository lock runs. Git publication is
        create-only and retry safe. The database reopens with
        ``BEGIN IMMEDIATE`` and the seal CASes the unsealed row plus the
        delegation, workspace, and base identity. A Git failure leaves the
        draft unsealed and the delegation untouched.
        """
        if not isinstance(child_session_id, str) or not child_session_id.strip():
            raise ValueError("child_session_id must be a non-empty string")
        from yinshi.config import get_settings

        if not get_settings().thread_hierarchy_enabled:
            raise ThreadHierarchyDisabledError()
        workspace_service = ThreadWorkspaceService()

        intent = await run_db_operation_for_request(
            request,
            lambda db: self._load_seal_intent(
                db,
                request,
                workspace_service,
                child_session_id,
            ),
        )
        finalized = await workspace_service.finalize_child_context(intent["context"])
        sealed = await run_db_operation_for_request(
            request,
            lambda db: self._commit_seal(db, intent, finalized),
        )
        return _project_result_row(sealed)

    def _load_seal_intent(
        self,
        db: sqlite3.Connection,
        request: Request,
        workspace_service: ThreadWorkspaceService,
        child_session_id: str,
    ) -> dict[str, Any]:
        """Authorize the child and capture the immutable seal context."""
        delegation = self._load_child_delegation(db, request, child_session_id)
        status = str(delegation["status"])
        if status not in TERMINAL_DELEGATION_STATUSES:
            raise ThreadResultNotSealableError()
        delegation_id = str(delegation["id"])
        child_workspace_id = delegation["child_workspace_id"]
        base_commit = delegation["base_commit"]
        if child_workspace_id is None or base_commit is None:
            raise ThreadResultSealConflictError(
                "delegation lacks its workspace or base identity",
            )
        row = db.execute(
            "SELECT * FROM thread_results WHERE delegation_id = ?",
            (delegation_id,),
        ).fetchone()
        if row is None or str(row["source"]) != "reported":
            raise ThreadResultDraftMissingError()
        context = workspace_service.load_finalization_context(
            db,
            get_tenant(request),
            delegation_id=delegation_id,
            workspace_id=str(child_workspace_id),
            base_commit=str(base_commit),
        )
        return {
            "delegation_id": delegation_id,
            "child_session_id": child_session_id,
            "child_workspace_id": str(child_workspace_id),
            "base_commit": str(base_commit),
            "context": context,
        }

    def _commit_seal(
        self,
        db: sqlite3.Connection,
        intent: dict[str, Any],
        finalized: FinalizedThreadGitResult,
    ) -> sqlite3.Row:
        """CAS the unsealed draft to sealed with the finalized Git identity."""
        delegation_id = str(intent["delegation_id"])
        db.execute("BEGIN IMMEDIATE")
        try:
            delegation = db.execute(
                "SELECT * FROM thread_delegations WHERE id = ?",
                (delegation_id,),
            ).fetchone()
            if (
                delegation is None
                or str(delegation["child_session_id"]) != intent["child_session_id"]
                or str(delegation["child_workspace_id"]) != intent["child_workspace_id"]
                or str(delegation["base_commit"]) != intent["base_commit"]
            ):
                raise ThreadResultSealConflictError(
                    "delegation identity changed during finalization",
                )
            row = db.execute(
                "SELECT * FROM thread_results WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if row is None:
                raise ThreadResultDraftMissingError()
            changed_json = _changed_files_json(finalized.changed_files)
            if int(row["sealed"]) == 1:
                if not _sealed_git_fields_match(row, finalized):
                    raise ThreadResultSealConflictError(
                        "stored sealed Git metadata differs from this attempt",
                    )
                db.rollback()
                return cast(sqlite3.Row, row)
            result = db.execute(
                """UPDATE thread_results
                   SET sealed = 1, base_commit = ?, result_commit = ?, result_ref = ?,
                       changed_files_json = ?, sealed_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE delegation_id = ? AND sealed = 0 AND version = ?""",
                (
                    finalized.base_commit,
                    finalized.result_commit,
                    finalized.result_ref,
                    changed_json,
                    delegation_id,
                    int(row["version"]),
                ),
            )
            if result.rowcount != 1:
                raise ThreadResultSealConflictError(
                    "another writer sealed this draft first",
                )
            sealed = cast(
                sqlite3.Row,
                db.execute(
                    "SELECT * FROM thread_results WHERE delegation_id = ?",
                    (delegation_id,),
                ).fetchone(),
            )
            db.commit()
            return sealed
        except BaseException:
            db.rollback()
            raise

    def _load_child_delegation(
        self,
        db: sqlite3.Connection,
        request: Request,
        child_session_id: str,
    ) -> sqlite3.Row:
        """Authorize the child session and return its delegation row."""
        _authorize_session(db, request, child_session_id)
        row = db.execute(
            "SELECT * FROM thread_delegations WHERE child_session_id = ?",
            (child_session_id,),
        ).fetchone()
        if row is None:
            raise ThreadNotFoundError(child_session_id)
        return cast(sqlite3.Row, row)

    async def report_result(
        self,
        request: Request,
        *,
        child_session_id: str,
        body: ThreadResultReportCreate,
    ) -> dict[str, Any]:
        """Store one reported result draft for a delegated child thread."""
        if not isinstance(child_session_id, str) or not child_session_id.strip():
            raise ValueError("child_session_id must be a non-empty string")
        from yinshi.config import get_settings

        if not get_settings().thread_hierarchy_enabled:
            raise ThreadHierarchyDisabledError()
        # Reconciliation precedes every Phase 3 write. Repeated calls claim
        # nothing because interrupted is terminal.
        await reconcile_stale_provisioning(request)

        def operate(db: sqlite3.Connection) -> sqlite3.Row:
            return self._report_result_write(db, request, child_session_id, body)

        row = await run_db_operation_for_request(request, operate)
        return _project_result_row(row)

    def _report_result_write(
        self,
        db: sqlite3.Connection,
        request: Request,
        child_session_id: str,
        body: ThreadResultReportCreate,
    ) -> sqlite3.Row:
        """Apply one report against the draft row inside one transaction."""
        db.execute("BEGIN IMMEDIATE")
        try:
            delegation = self._load_child_delegation(db, request, child_session_id)
            delegation_id = str(delegation["id"])
            row = db.execute(
                "SELECT * FROM thread_results WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if row is None:
                if body.expected_version != 0:
                    raise ThreadResultVersionConflictError()
                db.execute(
                    """INSERT INTO thread_results (
                           delegation_id, version, source, summary,
                           tests_json, warnings_json
                       ) VALUES (?, 1, 'reported', ?, ?, ?)""",
                    (
                        delegation_id,
                        body.summary,
                        _report_tests_json(body),
                        _report_warnings_json(body),
                    ),
                )
            else:
                if int(row["sealed"]) == 1:
                    raise ThreadResultSealedError()
                current_version = int(row["version"])
                if current_version != body.expected_version:
                    if _report_canonical(_report_stored_payload(row)) == _report_canonical(
                        _report_incoming_payload(body)
                    ):
                        db.rollback()
                        return cast(sqlite3.Row, row)
                    raise ThreadResultVersionConflictError()
                db.execute(
                    """UPDATE thread_results
                       SET version = ?, summary = ?, tests_json = ?, warnings_json = ?,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE delegation_id = ? AND sealed = 0 AND version = ?""",
                    (
                        current_version + 1,
                        body.summary,
                        _report_tests_json(body),
                        _report_warnings_json(body),
                        delegation_id,
                        current_version,
                    ),
                )
            updated = cast(
                sqlite3.Row,
                db.execute(
                    "SELECT * FROM thread_results WHERE delegation_id = ?",
                    (delegation_id,),
                ).fetchone(),
            )
            db.commit()
            return updated
        except BaseException:
            db.rollback()
            raise

    async def _cancel_provisioning(self, request: Request, delegation_id: str) -> None:
        """Claim one provisioning reservation, then clean its staged artifacts.

        The atomic claim is the coordination point with an in-flight spawn:
        a losing attach rolls back and discards its own staged Git artifacts,
        while a winner has cancelled the reservation and stamped completion.
        Only a claim over a row without an attached child runs the artifact
        cleanup, and the database stays closed while Git subprocesses run.
        Cleanup failures stay logged for later maintenance and never mask
        the cancellation outcome.
        """
        won, has_child = await run_db_operation_for_request(
            request,
            lambda db: self._claim_provisioning_cancel(db, delegation_id),
        )
        if not won or has_child:
            # Another writer advanced the reservation, or an attached child
            # exists: its decision and resources are the winner and stand.
            if won and has_child:
                logger.warning(
                    "Thread delegation %s cancelled while a child was attached; "
                    "keeping its resources",
                    delegation_id,
                )
            return
        context = await self._load_provisioning_parent_context(request, delegation_id)
        if context is None:
            return
        try:
            await ThreadWorkspaceService().discard_staged_child_git_artifacts(
                context,
                _CANCELLED_PROVISIONING_STAGED,
            )
        except Exception as cleanup_error:
            logger.warning(
                "Thread provisioning cleanup failed for delegation %s with %s",
                delegation_id,
                type(cleanup_error).__name__,
            )

    async def _load_provisioning_parent_context(
        self,
        request: Request,
        delegation_id: str,
    ) -> ThreadParentGitContext | None:
        """Derive one immutable parent context for a cancelled reservation.

        The parent session and delegation ID fully determine the Git context,
        so cleanup can run after the claiming transaction closed. A missing
        parent session or failed context load leaves nothing to clean.
        """
        workspace_service = ThreadWorkspaceService()

        def load(db: sqlite3.Connection) -> ThreadParentGitContext | None:
            parent = db.execute(
                "SELECT workspace_id FROM sessions WHERE id = ("
                "SELECT parent_session_id FROM thread_delegations WHERE id = ?)",
                (delegation_id,),
            ).fetchone()
            if parent is None:
                return None
            try:
                return workspace_service.load_parent_context(
                    db,
                    get_tenant(request),
                    parent_workspace_id=str(parent["workspace_id"]),
                    delegation_id=delegation_id,
                )
            except Exception as context_error:
                logger.warning(
                    "Thread provisioning context load failed for delegation %s with %s",
                    delegation_id,
                    type(context_error).__name__,
                )
                return None

        try:
            return await run_db_operation_for_request(request, load)
        except Exception as load_error:
            logger.warning(
                "Thread provisioning context query failed for delegation %s with %s",
                delegation_id,
                type(load_error).__name__,
            )
            return None

    @staticmethod
    def _claim_provisioning_cancel(
        db: sqlite3.Connection,
        delegation_id: str,
    ) -> tuple[bool, bool]:
        """CAS one provisioning reservation to cancelled in one transaction.

        Returns whether the claim won and whether a child session is already
        attached. The status decision and the attached-child observation are
        read atomically so cleanup can never race an attach.
        """
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT child_session_id FROM thread_delegations WHERE id = ?",
                (delegation_id,),
            ).fetchone()
            has_child = row is not None and row["child_session_id"] is not None
            result = db.execute(
                """UPDATE thread_delegations
                   SET status = ?, completed_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = ?""",
                (
                    DELEGATION_STATUS_CANCELLED,
                    delegation_id,
                    DELEGATION_STATUS_PROVISIONING,
                ),
            )
            db.commit()
            return (int(result.rowcount) == 1, bool(has_child))
        except BaseException:
            db.rollback()
            raise

    async def _cancel_queued(self, request: Request, delegation_id: str) -> None:
        """CAS one queued delegation to cancelled and stamp its completion.

        The conditional update keeps a concurrent start or retry decision
        authoritative. Attached child resources are deliberately untouched.
        """

        def cancel(db: sqlite3.Connection) -> int:
            db.execute("BEGIN IMMEDIATE")
            try:
                result = db.execute(
                    """UPDATE thread_delegations
                       SET status = ?, completed_at = CURRENT_TIMESTAMP,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status = ?""",
                    (
                        DELEGATION_STATUS_CANCELLED,
                        delegation_id,
                        DELEGATION_STATUS_QUEUED,
                    ),
                )
                db.commit()
                return int(result.rowcount)
            except BaseException:
                db.rollback()
                raise

        rowcount = await run_db_operation_for_request(request, cancel)
        if rowcount != 1:
            logger.warning(
                "Thread delegation %s left the queued state before cancellation",
                delegation_id,
            )

    async def _reload_delegation(self, request: Request, delegation_id: str) -> sqlite3.Row:
        """Reload one delegation row through one short connection."""

        def reload(db: sqlite3.Connection) -> sqlite3.Row:
            row = db.execute(
                "SELECT * FROM thread_delegations WHERE id = ?",
                (delegation_id,),
            ).fetchone()
            assert row is not None
            return cast(sqlite3.Row, row)

        return await run_db_operation_for_request(request, reload)

    async def _start_initial_prompt(
        self,
        request: Request,
        outcome: ThreadSpawnOutcome,
        body: ThreadChildCreate,
    ) -> ThreadSpawnOutcome:
        """Start the child's initial run and promote the queued delegation.

        The journal receives the caller's request so the run commits in the
        same tenant database runtime as the attached child session. Only an
        accepted start promotes the delegation to running. Any start failure
        marks the delegation failed while the attached workspace survives. If
        a concurrent cancellation wins the queued-to-cancelled transition
        before the running CAS, the exact accepted run is compensated through
        the journal so no orphaned prompt keeps executing.
        """
        journal = getattr(request.app.state, "prompt_journal", None)
        if not isinstance(journal, PromptJournal) or outcome.child_session_id is None:
            await self._fail_queued_start(request, outcome.delegation_id)
            raise ThreadPromptStartError()
        from yinshi.api.stream import PromptRequest, ThinkingLevel

        prompt_body = PromptRequest(
            prompt=build_initial_prompt(
                title=body.title.strip(),
                role=body.role,
                task=body.task.strip(),
                context=None if body.context is None else body.context.strip() or None,
            ),
            model=body.model,
            thinking=cast("ThinkingLevel | None", body.thinking),
        )
        try:
            accepted_run = await journal.start(
                request=request,
                session_id=outcome.child_session_id,
                idempotency_key=initial_run_idempotency_key(outcome.delegation_id),
                body=prompt_body,
            )
        except Exception as exc:
            await self._fail_queued_start(request, outcome.delegation_id)
            raise ThreadPromptStartError() from exc
        promoted = await self._mark_running(request, outcome)
        if promoted.status != DELEGATION_STATUS_RUNNING:
            await self._cancel_accepted_run(request, journal, accepted_run, promoted)
        return promoted

    async def _cancel_accepted_run(
        self,
        request: Request,
        journal: PromptJournal,
        accepted_run: PromptRun,
        outcome: ThreadSpawnOutcome,
    ) -> None:
        """Cancel the exact accepted run after the running CAS was lost.

        The start call already returned this run's durable identity, so
        compensation cancels it directly and never re-queries for a run.
        Journal cancellation is idempotent on retries, a run that already
        finished keeps its existing terminal status, and no database
        connection is held across the journal call. Failures are logged with
        safe identifiers and never mask the winning delegation decision.
        """
        try:
            durable_run = await journal.cancel(
                request=request,
                session_id=accepted_run.session_id,
                run_id=accepted_run.id,
            )
        except Exception as exc:
            logger.warning(
                "Compensating accepted run %s for delegation %s failed: %s",
                accepted_run.id,
                outcome.delegation_id,
                type(exc).__name__,
            )
            return
        logger.info(
            "Compensated accepted run %s for delegation %s with durable status %s",
            accepted_run.id,
            outcome.delegation_id,
            durable_run.status,
        )

    async def _mark_running(
        self,
        request: Request,
        outcome: ThreadSpawnOutcome,
    ) -> ThreadSpawnOutcome:
        """Promote one queued delegation to running with a conditional update."""

        def mark(db: sqlite3.Connection) -> int:
            db.execute("BEGIN IMMEDIATE")
            try:
                result = db.execute(
                    """UPDATE thread_delegations
                       SET status = ?, started_at = CURRENT_TIMESTAMP,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status = ?""",
                    (
                        DELEGATION_STATUS_RUNNING,
                        outcome.delegation_id,
                        DELEGATION_STATUS_QUEUED,
                    ),
                )
                db.commit()
                return int(result.rowcount)
            except BaseException:
                db.rollback()
                raise

        try:
            rowcount = await run_db_operation_for_request(request, mark)
        except Exception as mark_error:
            logger.warning(
                "Marking thread delegation %s running failed with %s",
                outcome.delegation_id,
                type(mark_error).__name__,
            )
            return outcome
        if rowcount != 1:
            logger.warning(
                "Thread delegation %s left the queued state before running",
                outcome.delegation_id,
            )

        def reload(db: sqlite3.Connection) -> sqlite3.Row:
            row = db.execute(
                "SELECT * FROM thread_delegations WHERE id = ?",
                (outcome.delegation_id,),
            ).fetchone()
            assert row is not None
            return cast(sqlite3.Row, row)

        return ThreadSpawnOutcome.from_row(await run_db_operation_for_request(request, reload))

    async def _fail_queued_start(self, request: Request, delegation_id: str) -> None:
        """Mark one queued delegation failed after its prompt start failed.

        The conditional update only applies while the delegation is still
        queued, so a concurrent decision always wins. The attached child
        workspace and session rows are deliberately left untouched. Only the
        delegation carries the safe failure code. Failures here are logged
        and never mask the original start error.
        """

        def fail(db: sqlite3.Connection) -> None:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """UPDATE thread_delegations
                       SET status = ?, error_code = ?, error_detail_safe = ?,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status = ?""",
                    (
                        DELEGATION_STATUS_FAILED,
                        _START_FAILED_ERROR_CODE,
                        _START_FAILED_SAFE_DETAIL,
                        delegation_id,
                        DELEGATION_STATUS_QUEUED,
                    ),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise

        try:
            await run_db_operation_for_request(request, fail)
        except Exception as failure_error:
            logger.warning(
                "Marking thread delegation %s start-failed failed with %s",
                delegation_id,
                type(failure_error).__name__,
            )

    async def _discard_failed_attach(
        self,
        request: Request,
        workspace_service: ThreadWorkspaceService,
        context: ThreadParentGitContext,
        staged: ThreadStagedChildGit,
        delegation_id: str,
    ) -> None:
        """Clean staged Git artifacts after one attach failure.

        The attach transaction already rolled back its rows. Staged worktree,
        branch, and snapshot artifacts are removed through the connection-free
        workspace cleanup API, so no database connection is open while Git
        subprocesses run. When the attach committed despite the surfaced error
        the child now owns those artifacts, and cleanup never deletes them.
        Cleanup failures are logged and never mask the original attach error.
        """

        def load_child_session_id(db: sqlite3.Connection) -> str | None:
            row = db.execute(
                "SELECT child_session_id FROM thread_delegations WHERE id = ?",
                (delegation_id,),
            ).fetchone()
            if row is None or row["child_session_id"] is None:
                return None
            return str(row["child_session_id"])

        child_session_id = await run_db_operation_for_request(request, load_child_session_id)
        if child_session_id is not None:
            logger.warning(
                "Thread attach for delegation %s raised after committing; "
                "keeping its attached Git artifacts",
                delegation_id,
            )
            return
        try:
            await workspace_service.discard_staged_child_git_artifacts(context, staged)
        except Exception as cleanup_error:
            logger.warning(
                "Thread attach cleanup failed for delegation %s with %s",
                delegation_id,
                type(cleanup_error).__name__,
            )

    async def _fail_reservation_best_effort(
        self,
        request: Request,
        delegation_id: str,
    ) -> None:
        """Mark one provisioning reservation failed with safe sanitized fields.

        The conditional update only applies while the delegation still sits in
        ``provisioning``, so a concurrent cancellation or retry decision always
        wins. Failures here are logged and never mask the original spawn error.
        """

        def fail(db: sqlite3.Connection) -> None:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """UPDATE thread_delegations
                       SET status = ?, error_code = ?, error_detail_safe = ?,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status = ?""",
                    (
                        DELEGATION_STATUS_FAILED,
                        _PROVISION_FAILED_ERROR_CODE,
                        _PROVISION_FAILED_SAFE_DETAIL,
                        delegation_id,
                        DELEGATION_STATUS_PROVISIONING,
                    ),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise

        try:
            await run_db_operation_for_request(request, fail)
        except Exception as failure_error:
            logger.warning(
                "Marking thread delegation %s failed failed with %s",
                delegation_id,
                type(failure_error).__name__,
            )

    def _reserve(
        self,
        db: sqlite3.Connection,
        request: Request,
        parent_session_id: str,
        idempotency_key: str,
        body: ThreadChildCreate,
        retry_of_delegation_id: str | None = None,
    ) -> sqlite3.Row:
        """Insert one provisioning delegation inside one immediate transaction."""
        db.execute("BEGIN IMMEDIATE")
        try:
            existing = cast(
                sqlite3.Row,
                db.execute(
                    """SELECT * FROM thread_delegations
                       WHERE parent_session_id = ? AND idempotency_key = ?""",
                    (parent_session_id, idempotency_key),
                ).fetchone(),
            )
            if existing is not None:
                db.rollback()
                _assert_request_matches(existing, body, retry_of_delegation_id)
                return existing
            _authorize_parent(db, request, parent_session_id)
            _enforce_spawn_limits(db, request, parent_session_id)
            db.execute(
                """INSERT INTO thread_delegations (
                       parent_session_id, idempotency_key, initiator,
                       title, task, context, role,
                       requested_model, requested_thinking, status,
                       retry_of_delegation_id
                   ) VALUES (?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    parent_session_id,
                    idempotency_key,
                    body.title.strip(),
                    body.task.strip(),
                    None if body.context is None else body.context.strip() or None,
                    body.role,
                    normalize_model_ref(body.model),
                    None if body.thinking is None else body.thinking.strip() or None,
                    DELEGATION_STATUS_PROVISIONING,
                    retry_of_delegation_id,
                ),
            )
            row = cast(
                sqlite3.Row,
                db.execute(
                    """SELECT * FROM thread_delegations
                       WHERE parent_session_id = ? AND idempotency_key = ?""",
                    (parent_session_id, idempotency_key),
                ).fetchone(),
            )
            db.commit()
            return row
        except BaseException:
            db.rollback()
            raise


def _authorize_session(
    db: sqlite3.Connection,
    request: Request,
    session_id: str,
) -> None:
    """Authorize one session before its delegation is revealed.

    Tenant mode isolates sessions per tenant database, so membership in the
    tenant database is the authorization. Legacy mode keeps the per-repo
    ``owner_email`` ownership check, and unknown or foreign sessions map to
    the same not-found error so foreign delegations never disclose existence.
    """
    if get_tenant(request) is not None:
        session = db.execute(
            "SELECT id FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    else:
        session = db.execute(
            """SELECT s.id AS id, r.owner_email AS owner_email
               FROM sessions s
               JOIN workspaces w ON s.workspace_id = w.id
               JOIN repos r ON w.repo_id = r.id
               WHERE s.id = ?""",
            (session_id,),
        ).fetchone()
        if session is not None:
            owner_email = session["owner_email"]
            user_email = get_user_email(request)
            if user_email and owner_email and owner_email != user_email:
                raise ThreadParentNotAuthorizedError(
                    "session is not owned by this user",
                )
    if session is None:
        raise ThreadNotFoundError(session_id)


def _spawn_owner_email(request: Request) -> str | None:
    """Return the legacy visibility owner for reads, mirroring the read API."""
    if get_tenant(request) is not None:
        return None
    return get_user_email(request)


def _enforce_spawn_limits(
    db: sqlite3.Connection,
    request: Request,
    parent_session_id: str,
) -> None:
    """Reject one spawn whose child would sit beyond the maximum depth."""
    owner_email = _spawn_owner_email(request)
    parent_thread = get_thread(db, parent_session_id, owner_email=owner_email)
    limits = get_thread_limits(db, parent_session_id, owner_email=owner_email)
    candidate_depth = int(parent_thread["depth"]) + 1
    if candidate_depth > int(limits["max_depth"]):
        raise ThreadDepthLimitError(
            "thread depth limit exceeded for this root tree",
        )
    if int(limits["direct_children"]) >= int(limits["max_direct_children"]):
        raise ThreadChildLimitError(
            "thread direct-child limit exceeded for the parent",
        )
    if int(limits["active_descendants"]) >= int(limits["max_active_descendants"]):
        raise ThreadActiveDescendantsLimitError(
            "thread active-descendant limit exceeded for the root tree",
        )
    if int(limits["total_threads"]) >= int(limits["max_total_threads"]):
        raise ThreadTreeLimitError(
            "thread tree size limit exceeded for the root tree",
        )


def _authorize_parent(
    db: sqlite3.Connection,
    request: Request,
    parent_session_id: str,
) -> None:
    """Authorize the parent before its existence or metadata is revealed.

    Tenant mode isolates sessions per tenant database, so membership in the
    tenant database is the authorization. Legacy mode keeps the long-standing
    per-repo ``owner_email`` ownership check with the same mismatch rule as
    the session APIs. Callers must already hold the write transaction so the
    check and the reservation commit atomically.
    """
    if get_tenant(request) is not None:
        parent = db.execute(
            "SELECT id FROM sessions WHERE id = ?",
            (parent_session_id,),
        ).fetchone()
    else:
        parent = db.execute(
            """SELECT s.id AS id, r.owner_email AS owner_email
               FROM sessions s
               JOIN workspaces w ON s.workspace_id = w.id
               JOIN repos r ON w.repo_id = r.id
               WHERE s.id = ?""",
            (parent_session_id,),
        ).fetchone()
        if parent is not None:
            owner_email = parent["owner_email"]
            user_email = get_user_email(request)
            if user_email and owner_email and owner_email != user_email:
                raise ThreadParentNotAuthorizedError(
                    "parent session is not owned by this user",
                )
    if parent is None:
        raise ThreadNotFoundError(parent_session_id)


def _canonical_idempotency_key(value: str) -> str:
    """Return one canonical UUID string for the idempotency key.

    Retries must never alias: ``2c2fbe56-...`` and its uppercase or
    brace-wrapped spelling resolve to the same canonical key.
    """
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("idempotency_key must be a UUID") from exc


def _assert_request_matches(
    row: sqlite3.Row,
    body: ThreadChildCreate,
    retry_of_delegation_id: str | None = None,
) -> None:
    """Reject one idempotency key reuse whose normalized request differs.

    Retry lineage participates in the comparison so a lineage-less manual
    spawn can never replay a retry's key and vice versa.
    """
    stored = {
        "title": str(row["title"] or "").strip(),
        "task": str(row["task"] or "").strip(),
        "context": (None if row["context"] is None else str(row["context"]).strip() or None),
        "role": str(row["role"]),
        "model": normalize_model_ref(str(row["requested_model"] or "")),
        "thinking": (
            None
            if row["requested_thinking"] is None
            else str(row["requested_thinking"]).strip() or None
        ),
        "retry_of_delegation_id": (
            None if row["retry_of_delegation_id"] is None else str(row["retry_of_delegation_id"])
        ),
    }
    incoming = {
        "title": body.title.strip(),
        "task": body.task.strip(),
        "context": None if body.context is None else body.context.strip() or None,
        "role": str(body.role),
        "model": normalize_model_ref(body.model),
        "thinking": None if body.thinking is None else body.thinking.strip() or None,
        "retry_of_delegation_id": retry_of_delegation_id,
    }
    if stored != incoming:
        raise ThreadIdempotencyConflictError(
            "idempotency key was already used with a different request",
        )
