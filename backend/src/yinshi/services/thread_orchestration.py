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

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from fastapi import Request

from yinshi.api.deps import (
    get_tenant,
    get_user_email,
    request_database_identity,
    run_db_operation_for_request,
)
from yinshi.exceptions import YinshiError
from yinshi.model_catalog import normalize_model_ref
from yinshi.models import ThreadChildCreate, ThreadResultReportCreate, ThreadRetryCreate
from yinshi.services.orchestration_bridge import THREAD_OPERATIONS, VerifiedThreadCaller
from yinshi.services.prompt_journal import PromptJournal, PromptRun, PromptRunNotFoundError
from yinshi.services.thread_git_ownership import (
    ThreadGitClaim,
    ThreadGitFinalization,
    ThreadGitOwnershipError,
    ThreadGitWorktree,
)
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
    ThreadCycleError,
    ThreadNotFoundError,
    get_thread,
    get_thread_limits,
)
from yinshi.services.thread_reconciliation import (
    cleanup_provisioning_artifacts,
    reconcile_stale_provisioning,
)
from yinshi.services.thread_workspaces import (
    FinalizedThreadGitResult,
    ThreadParentGitContext,
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


@dataclass(slots=True)
class _ThreadWaitChannel:
    """A generation changes before notification so subscriptions never clear a wakeup."""

    event: asyncio.Event
    generation: int = 0
    waiters: int = 0


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
    """Coordinate durable manual and agent child-thread lifecycles."""

    def __init__(self) -> None:
        self._wait_channels: dict[str, _ThreadWaitChannel] = {}
        self._activation_locks: dict[str, asyncio.Lock] = {}
        self._activated_databases: set[str] = set()

    async def activate(self, request: Request) -> None:
        """Recover one selected execution database before accepting runtime work."""
        identity = request_database_identity(request)
        if identity in self._activated_databases:
            return
        lock = self._activation_locks.setdefault(identity, asyncio.Lock())
        async with lock:
            if identity in self._activated_databases:
                return
            journal = getattr(request.app.state, "prompt_journal", None)
            if not isinstance(journal, PromptJournal):
                raise ThreadPromptStartError("Prompt recovery is unavailable.")
            await journal.recover(request)
            await self.reconcile(request)
            self._activated_databases.add(identity)

    @staticmethod
    def authorize_caller(
        db: sqlite3.Connection,
        request: Request,
        caller: VerifiedThreadCaller,
    ) -> None:
        """Require the calling session to own its active durable prompt run."""
        from yinshi.config import get_settings

        settings = get_settings()
        if not settings.thread_hierarchy_enabled or not settings.agent_delegation_enabled:
            raise ThreadHierarchyDisabledError()
        tenant = get_tenant(request)
        tenant_id = tenant.user_id if tenant is not None else None
        if (
            caller.tenant_id != tenant_id
            or caller.expires_at <= time.monotonic()
            or caller.database_path != request_database_identity(request)
        ):
            raise ThreadNotFoundError(caller.session_id)
        _authorize_session(db, request, caller.session_id)
        run = db.execute(
            "SELECT status FROM prompt_runs WHERE id = ? AND session_id = ?",
            (caller.run_id, caller.session_id),
        ).fetchone()
        if run is None or run["status"] != "running":
            raise ThreadNotFoundError(caller.session_id)

    def authorize_descendant(
        self,
        db: sqlite3.Connection,
        request: Request,
        caller: VerifiedThreadCaller,
        thread_id: str,
    ) -> sqlite3.Row:
        """Resolve a child or placeholder within the caller's permitted subtree."""
        self.authorize_caller(db, request, caller)
        if thread_id == caller.session_id:
            raise ThreadNotFoundError(thread_id)
        target = self._resolve_cancel_target(db, request, thread_id)
        caller_repo = self._session_repository(db, caller.session_id)
        child_id = target["child_session_id"]
        if child_id is not None and self._session_repository(db, str(child_id)) != caller_repo:
            raise ThreadNotFoundError(thread_id)
        if child_id == caller.session_id:
            raise ThreadNotFoundError(thread_id)
        current = target
        visited = {thread_id}
        for _ in range(64):
            parent_id = str(current["parent_session_id"])
            if parent_id in visited:
                raise ThreadNotFoundError(thread_id)
            _authorize_session(db, request, parent_id)
            if self._session_repository(db, parent_id) != caller_repo:
                raise ThreadNotFoundError(thread_id)
            if parent_id == caller.session_id:
                return target
            visited.add(parent_id)
            current = db.execute(
                "SELECT * FROM thread_delegations WHERE child_session_id = ?",
                (parent_id,),
            ).fetchone()
            if current is None:
                break
        raise ThreadNotFoundError(thread_id)

    @staticmethod
    def _assert_no_cancellation(db: sqlite3.Connection, parent_session_id: str) -> None:
        """Check the whole ancestor barrier inside the writer's transaction."""
        current = parent_session_id
        seen = set()
        for _ in range(32):
            if current in seen:
                raise ThreadCycleError("thread ancestry contains a cycle")
            seen.add(current)
            row = db.execute(
                "SELECT parent_session_id, cancel_scope, status FROM thread_delegations WHERE child_session_id = ?",
                (current,),
            ).fetchone()
            if row is None:
                return
            if row["cancel_scope"] is not None or row["status"] == DELEGATION_STATUS_CANCELLING:
                raise ThreadOrchestrationError(
                    "thread_cancel_pending", "An ancestor cancellation is pending."
                )
            current = str(row["parent_session_id"])
        raise ThreadTreeLimitError("thread ancestry exceeds the depth bound")

    @staticmethod
    def _session_repository(db: sqlite3.Connection, session_id: str) -> str:
        row = db.execute(
            "SELECT w.repo_id FROM sessions s JOIN workspaces w ON s.workspace_id = w.id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise ThreadNotFoundError(session_id)
        return str(row["repo_id"])

    async def query_operations(
        self,
        request: Request,
        *,
        session_id: str,
        run_id: str | None,
    ) -> frozenset[str]:
        """Select tools from the durable actor and backend feature policy."""
        from yinshi.config import get_settings

        settings = get_settings()
        if (
            run_id is None
            or not settings.thread_hierarchy_enabled
            or not settings.agent_delegation_enabled
        ):
            return frozenset({"ping_thread_bridge"})
        tenant = get_tenant(request)
        actor = VerifiedThreadCaller(
            session_id=session_id,
            run_id=run_id,
            tenant_id=tenant.user_id if tenant is not None else None,
            runtime_id=None,
            tool_call_id="query-binding",
            expires_at=time.monotonic() + 60,
            database_path=request_database_identity(request),
        )

        def select(db: sqlite3.Connection) -> frozenset[str]:
            self.authorize_caller(db, request, actor)
            child = db.execute(
                "SELECT id FROM thread_delegations WHERE child_session_id = ?",
                (session_id,),
            ).fetchone()
            return (
                THREAD_OPERATIONS
                if child is not None
                else THREAD_OPERATIONS - {"report_thread_result"}
            )

        return await run_db_operation_for_request(request, select)

    async def list_agent_children(
        self,
        request: Request,
        *,
        caller: VerifiedThreadCaller,
        include_terminal: bool = True,
    ) -> dict[str, Any]:
        """List authorized direct children and placeholders with current limits."""

        def load(db: sqlite3.Connection) -> dict[str, Any]:
            self.authorize_caller(db, request, caller)
            selection = "parent_session_id = ? AND (? OR status IN ('provisioning', 'queued', 'running', 'cancelling'))"
            parameters = (caller.session_id, include_terminal)
            rows = db.execute(
                "SELECT id FROM thread_delegations WHERE "
                + selection
                + " ORDER BY created_at, id LIMIT 20",
                parameters,
            ).fetchall()
            count = db.execute(
                "SELECT COUNT(*) FROM thread_delegations WHERE " + selection,
                parameters,
            ).fetchone()[0]
            return {
                "children": [
                    self._agent_thread_snapshot(db, request, caller, str(row["id"])) for row in rows
                ],
                "children_total": int(count),
                "truncated": int(count) > len(rows),
                "limits": get_thread_limits(
                    db, caller.session_id, owner_email=_spawn_owner_email(request)
                ),
            }

        selected = await run_db_operation_for_request(request, load)
        await self._refresh_selection(
            request, [str(row["delegation_id"]) for row in selected["children"]], caller=caller
        )
        return await run_db_operation_for_request(request, load)

    async def get_agent_thread(
        self,
        request: Request,
        *,
        caller: VerifiedThreadCaller,
        thread_id: str,
        include_result: bool = True,
    ) -> dict[str, Any]:
        """Read an authorized descendant with a bounded result preview."""
        from yinshi.services.thread_queries import get_tool_result

        def load(db: sqlite3.Connection) -> dict[str, Any]:
            thread = self._agent_thread_snapshot(db, request, caller, thread_id)
            return {
                "thread": thread,
                "result": get_tool_result(db, thread["delegation_id"]) if include_result else None,
            }

        selected = await run_db_operation_for_request(request, load)
        await self._refresh_selection(
            request, [str(selected["thread"]["delegation_id"])], caller=caller
        )
        return await run_db_operation_for_request(request, load)

    async def get_manual_tree(
        self,
        request: Request,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Refresh only delegations in the authorized bounded tree projection."""
        from yinshi.services.thread_queries import get_tree

        def load(db: sqlite3.Connection) -> dict[str, Any]:
            _authorize_session(db, request, session_id)
            return get_tree(db, session_id, owner_email=_spawn_owner_email(request))

        def selection(tree: dict[str, Any]) -> set[str]:
            return {str(row["delegation_id"]) for row in [*tree["nodes"], *tree["placeholders"]]}

        identifiers = selection(await run_db_operation_for_request(request, load))

        def authorize(db: sqlite3.Connection) -> None:
            if not identifiers.issubset(selection(load(db))):
                raise ThreadNotFoundError(session_id)

        await self._refresh_selection(request, sorted(identifiers), authorization_guard=authorize)
        return await run_db_operation_for_request(request, load)

    async def get_manual_result(
        self,
        request: Request,
        *,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Retry only the authorized child's result and return sealed data only."""
        from yinshi.services.thread_queries import get_thread_result

        def load(db: sqlite3.Connection) -> tuple[dict[str, Any] | None, list[str]]:
            _authorize_session(db, request, session_id)
            result = get_thread_result(db, session_id, owner_email=_spawn_owner_email(request))
            row = db.execute(
                "SELECT id FROM thread_delegations WHERE child_session_id = ?",
                (session_id,),
            ).fetchone()
            return result, [] if row is None else [str(row["id"])]

        _, identifiers = await run_db_operation_for_request(request, load)

        def authorize(db: sqlite3.Connection) -> None:
            if load(db)[1] != identifiers:
                raise ThreadNotFoundError(session_id)

        await self._refresh_selection(request, identifiers, authorization_guard=authorize)
        return (await run_db_operation_for_request(request, load))[0]

    async def _refresh_selection(
        self,
        request: Request,
        delegation_ids: list[str],
        *,
        caller: VerifiedThreadCaller | None = None,
        authorization_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        """Cancel and drain only this read's bounded optional recovery work."""
        recovery = asyncio.create_task(
            self.reconcile(
                request,
                delegation_ids=delegation_ids,
                caller=caller,
                authorization_guard=authorization_guard,
            ),
            name="thread-read-reconciliation",
        )
        try:
            async with asyncio.timeout(2.0):
                await asyncio.shield(recovery)
        except TimeoutError:
            logger.debug("Thread read reconciliation remains pending")
        finally:
            if not recovery.done():
                recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)

    async def reconcile(
        self,
        request: Request,
        *,
        delegation_ids: list[str] | None = None,
        caller: VerifiedThreadCaller | None = None,
        authorization_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        """Recover a trusted runtime or a fully authorized, bounded selection."""
        parameters: tuple[str, ...] = ()
        scope = ""
        parents: set[str] | None = None
        read_guard = authorization_guard
        if caller is not None and delegation_ids is None:
            raise ThreadNotFoundError(caller.session_id)
        if delegation_ids is not None:
            if len(delegation_ids) > 500 or len(set(delegation_ids)) != len(delegation_ids):
                raise ThreadTreeLimitError("reconciliation selection exceeds its bound")
            if not delegation_ids:
                return
            identifiers = tuple(delegation_ids)

            def authorize(db: sqlite3.Connection) -> list[sqlite3.Row]:
                if read_guard is not None:
                    read_guard(db)
                return [
                    (
                        self.authorize_descendant(db, request, caller, identifier)
                        if caller is not None
                        else self._resolve_cancel_target(db, request, identifier)
                    )
                    for identifier in identifiers
                ]

            def authorize_claim(db: sqlite3.Connection) -> None:
                authorize(db)

            authorization_guard = authorize_claim
            selected = await run_db_operation_for_request(request, authorize)
            parameters = tuple(str(row["id"]) for row in selected)
            parents = {str(row["parent_session_id"]) for row in selected}
            scope = " AND d.id IN (" + ",".join("?" for _ in parameters) + ") "
        # Selected reads must not expand a persisted subtree cancellation.
        # Explicit cancellation and trusted runtime recovery own that work.
        cancellations = (
            []
            if delegation_ids is not None
            else await run_db_operation_for_request(
                request,
                lambda db: db.execute(
                    "SELECT d.id, d.cancel_scope FROM thread_delegations d WHERE d.cancel_scope IS NOT NULL "
                    "ORDER BY d.cancel_scope DESC, d.updated_at, d.id LIMIT 128",
                ).fetchall(),
            )
        )
        for row in cancellations:
            try:
                await self._cancel_claimed_scope(
                    request,
                    str(row["id"]),
                    caller,
                    row["cancel_scope"] == "subtree",
                )
            except (ThreadNotFoundError, ThreadParentNotAuthorizedError):
                continue
            except Exception:
                logger.warning("Thread cancellation recovery remains pending")
        if parents is None:
            await reconcile_stale_provisioning(request)
        else:
            for parent in sorted(parents):
                await reconcile_stale_provisioning(
                    request,
                    parent_session_id=parent,
                    authorization_guard=authorization_guard,
                    delegation_ids=parameters,
                )

        def load(db: sqlite3.Connection) -> tuple[list[tuple[str, str, str]], list[sqlite3.Row]]:
            delegations = db.execute(
                "SELECT d.* FROM thread_delegations d LEFT JOIN thread_results r ON r.delegation_id = d.id "
                "WHERE d.child_session_id IS NOT NULL AND COALESCE(r.sealed, 0) = 0 "
                + scope
                + " ORDER BY d.updated_at, d.id LIMIT 128",
                parameters,
            ).fetchall()
            outcomes = []
            pending = []
            for row in delegations:
                try:
                    _authorize_session(db, request, str(row["child_session_id"]))
                except (ThreadNotFoundError, ThreadParentNotAuthorizedError):
                    continue
                try:
                    initial_key = initial_run_idempotency_key(str(row["id"]))
                except ValueError:
                    # Legacy identifiers remain readable, never adopted for execution.
                    continue
                run = db.execute(
                    "SELECT id, status FROM prompt_runs WHERE session_id = ? AND idempotency_key = ?",
                    (row["child_session_id"], initial_key),
                ).fetchone()
                if run is not None and str(run["status"]) in _RUN_STATUS_TO_DELEGATION_STATUS:
                    outcomes.append(
                        (str(row["child_session_id"]), str(run["id"]), str(run["status"]))
                    )
                elif (
                    run is None
                    and row["status"] == DELEGATION_STATUS_QUEUED
                    and int(row["auto_start"]) == 1
                ):
                    if delegation_ids is not None:
                        try:
                            self._assert_no_cancellation(db, str(row["child_session_id"]))
                        except ThreadOrchestrationError as error:
                            if error.code != "thread_cancel_pending":
                                raise
                            continue
                    pending.append(row)
            return outcomes, pending

        outcomes, pending = await run_db_operation_for_request(request, load)
        workspace_service = ThreadWorkspaceService()
        intents: list[dict[str, Any]] = []
        for session_id, run_id, _status in outcomes:

            def prepare(
                db: sqlite3.Connection,
                session_id: str = session_id,
                run_id: str = run_id,
            ) -> dict[str, Any] | None:
                return self._prepare_terminal_result(
                    db,
                    request,
                    workspace_service,
                    session_id,
                    run_id,
                    authorization_guard=authorization_guard,
                )

            try:
                intent = await run_db_operation_for_request(request, prepare)
                if intent is not None:
                    intents.append(intent)
                    self._notify_thread_change(request)
            except Exception:
                logger.warning("Thread outcome reconciliation remains pending")
        for row in pending:
            try:
                body = ThreadChildCreate(
                    idempotency_key=str(row["idempotency_key"]),
                    title=str(row["title"]),
                    task=str(row["task"]),
                    context=row["context"],
                    role=row["role"],
                    model=row["requested_model"],
                    thinking=row["requested_thinking"],
                    start_immediately=True,
                )
                await self._start_initial_prompt(
                    request,
                    ThreadSpawnOutcome.from_row(row),
                    body,
                    authorization_guard=authorization_guard,
                )
            except Exception:
                await self._fail_queued_start(
                    request, str(row["id"]), authorization_guard=authorization_guard
                )
                logger.warning("Queued thread recovery failed")
        for intent in intents:
            try:
                await self._finalize_terminal_result(request, workspace_service, intent)
            except Exception:
                logger.warning("Thread result reconciliation remains pending")

    async def wait_for_threads(
        self,
        request: Request,
        *,
        caller: VerifiedThreadCaller,
        thread_ids: list[str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Wait for selected descendants with bounded durable-state rechecks."""
        from yinshi.config import get_settings

        if not 1 <= len(thread_ids) <= 20 or len(set(thread_ids)) != len(thread_ids):
            raise ValueError("wait requires one to twenty distinct thread IDs")
        if type(timeout_seconds) not in {int, float} or not 0 <= timeout_seconds <= 60:
            raise ValueError("wait timeout must be between zero and sixty seconds")
        timeout_effective = min(timeout_seconds, get_settings().thread_wait_timeout_seconds_max, 60)
        key = request_database_identity(request)
        channel = self._wait_channels.setdefault(key, _ThreadWaitChannel(asyncio.Event()))
        channel.waiters += 1
        deadline = time.monotonic() + timeout_effective
        reconciliation: asyncio.Task[None] | None = None
        try:
            while True:
                generation, event = channel.generation, channel.event
                threads = await run_db_operation_for_request(
                    request,
                    lambda db: [
                        self._agent_thread_snapshot(db, request, caller, thread_id)
                        for thread_id in thread_ids
                    ],
                )
                terminal = all(item["status"] in TERMINAL_DELEGATION_STATUSES for item in threads)
                remaining = deadline - time.monotonic()
                if terminal or remaining <= 0:
                    return {"threads": threads, "all_terminal": terminal, "timed_out": not terminal}
                if reconciliation is not None and reconciliation.done():
                    if not reconciliation.cancelled() and reconciliation.exception() is not None:
                        logger.warning("Thread wait reconciliation remains pending")
                    reconciliation = None
                if reconciliation is None:
                    reconciliation = asyncio.create_task(
                        self.reconcile(
                            request,
                            delegation_ids=[str(item["delegation_id"]) for item in threads],
                            caller=caller,
                        ),
                        name="thread-wait-reconciliation",
                    )
                if channel.generation != generation:
                    continue
                try:
                    await asyncio.wait_for(event.wait(), timeout=min(remaining, 0.25))
                except TimeoutError:
                    # Durable rereads also cover notifications from another process.
                    continue
        finally:
            channel.waiters -= 1
            if channel.waiters == 0 and self._wait_channels.get(key) is channel:
                self._wait_channels.pop(key, None)
            if reconciliation is not None:
                if not reconciliation.done():
                    reconciliation.cancel()
                await asyncio.gather(reconciliation, return_exceptions=True)

    def _notify_thread_change(self, request: Request) -> None:
        channel = self._wait_channels.get(request_database_identity(request))
        if channel is not None:
            channel.generation += 1
            event, channel.event = channel.event, asyncio.Event()
            event.set()

    def _agent_thread_snapshot(
        self,
        db: sqlite3.Connection,
        request: Request,
        caller: VerifiedThreadCaller,
        thread_id: str,
    ) -> dict[str, Any]:
        row = self.authorize_descendant(db, request, caller, thread_id)
        result = db.execute(
            "SELECT sealed, substr(summary, 1, 2001) AS summary, "
            "CASE WHEN length(changed_files_json) <= 4000000 THEN "
            "CASE WHEN json_valid(changed_files_json) THEN "
            "CASE WHEN json_type(changed_files_json) = 'array' THEN json_array_length(changed_files_json) END "
            "END END AS changed_files_count FROM thread_results WHERE delegation_id = ?",
            (row["id"],),
        ).fetchone()
        snapshot = {
            "id": str(row["child_session_id"] or row["id"]),
            "delegation_id": str(row["id"]),
            "parent_id": str(row["parent_session_id"]),
            "title": str(row["title"]),
            "status": str(row["status"]),
            "role": row["role"],
            "model": row["requested_model"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "changed_files_count": None if result is None else result["changed_files_count"],
            "result_available": result is not None and int(result["sealed"]) == 1,
            "result_pending": str(row["status"]) in TERMINAL_DELEGATION_STATUSES
            and (result is None or int(result["sealed"]) == 0),
            "summary": None if result is None else result["summary"],
            "truncated": False,
        }
        for key, limit in (
            ("title", 200),
            ("model", 200),
            ("role", 32),
            ("summary", 2000),
            ("started_at", 64),
            ("completed_at", 64),
        ):
            value = snapshot[key]
            if isinstance(value, str) and len(value) > limit:
                snapshot[key] = value[:limit]
                snapshot["truncated"] = True
        return snapshot

    async def observe_terminal(
        self,
        request: Request,
        session_id: str,
        run_id: str,
        status: str,
    ) -> None:
        """Seal the delegated initial run after its journal outcome commits."""
        if status not in _RUN_STATUS_TO_DELEGATION_STATUS:
            raise ValueError("terminal prompt status is invalid")
        workspace_service = ThreadWorkspaceService()
        intent = await run_db_operation_for_request(
            request,
            lambda db: self._prepare_terminal_result(
                db,
                request,
                workspace_service,
                session_id,
                run_id,
            ),
        )
        if intent is None:
            return
        self._notify_thread_change(request)
        await self._finalize_terminal_result(request, workspace_service, intent)

    async def _finalize_terminal_result(
        self,
        request: Request,
        workspace_service: ThreadWorkspaceService,
        intent: dict[str, Any],
    ) -> None:
        """Finalize outside database transactions after terminal state becomes visible."""
        try:
            finalized = await workspace_service.finalize_child_context(
                intent["context"],
                ownership=self._finalization_ownership(request, intent),
            )
            await run_db_operation_for_request(
                request,
                lambda db: self._commit_seal(db, request, intent, finalized),
            )
        except Exception:
            try:
                await run_db_operation_for_request(
                    request,
                    lambda db: self._record_finalization_failure(db, request, intent),
                )
            except Exception as error:  # noqa: BLE001
                # Preserve the original finalization failure.
                logger.warning(
                    "Thread finalization failure remains unrecorded (%s)", type(error).__name__
                )
            raise
        finally:
            self._notify_thread_change(request)

    def _prepare_terminal_result(
        self,
        db: sqlite3.Connection,
        request: Request,
        workspace_service: ThreadWorkspaceService,
        session_id: str,
        run_id: str,
        *,
        authorization_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> dict[str, Any] | None:
        """Load exact initial-run authority and preserve one derived fallback draft."""
        db.execute("BEGIN IMMEDIATE")
        try:
            if authorization_guard is not None:
                authorization_guard(db)
            delegation = db.execute(
                "SELECT * FROM thread_delegations WHERE child_session_id = ?",
                (session_id,),
            ).fetchone()
            if delegation is None:
                db.rollback()
                return None
            delegation_id = str(delegation["id"])
            run = db.execute(
                "SELECT status FROM prompt_runs WHERE id = ? AND session_id = ? "
                "AND idempotency_key = ?",
                (run_id, session_id, initial_run_idempotency_key(delegation_id)),
            ).fetchone()
            if run is None or str(run["status"]) not in _RUN_STATUS_TO_DELEGATION_STATUS:
                db.rollback()
                return None
            result = db.execute(
                "SELECT sealed FROM thread_results WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if result is not None and int(result["sealed"]) == 1:
                db.rollback()
                return None
            if result is None:
                message = db.execute(
                    "SELECT substr(content, 1, 20000) AS summary FROM messages "
                    "WHERE session_id = ? AND turn_id = ? AND role = 'assistant' "
                    "AND COALESCE(content, '') <> '' ORDER BY rowid DESC LIMIT 1",
                    (session_id, run_id),
                ).fetchone()
                summary = "" if message is None else str(message["summary"])
                db.execute(
                    "INSERT INTO thread_results (delegation_id, version, source, summary, "
                    "tests_json, warnings_json) VALUES (?, 1, 'derived', ?, '[]', ?)",
                    (
                        delegation_id,
                        summary,
                        json.dumps(["Child did not submit a structured result report."]),
                    ),
                )
            intent = self._load_seal_intent(
                db,
                request,
                workspace_service,
                session_id,
                observed_terminal=True,
            )
            intent["terminal_run_id"] = run_id
            intent["authorization_guard"] = authorization_guard
            current_status = str(delegation["status"])
            intent["terminal_status"] = (
                current_status
                if current_status in TERMINAL_DELEGATION_STATUSES
                else _RUN_STATUS_TO_DELEGATION_STATUS[str(run["status"])]
            )
            db.execute(
                "UPDATE thread_delegations SET status = ?, completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP), "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (intent["terminal_status"], delegation_id),
            )
            db.commit()
            return intent
        except BaseException:
            db.rollback()
            raise

    def _record_finalization_failure(
        self,
        db: sqlite3.Connection,
        request: Request,
        intent: dict[str, Any],
    ) -> None:
        """Record pending finalization without changing a terminal winner."""
        db.execute("BEGIN IMMEDIATE")
        try:
            self._validate_seal_identity(db, request, intent, require_ownership=False)
            db.execute(
                "UPDATE thread_delegations SET error_code = 'result_finalization_failed', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? "
                "AND NOT EXISTS (SELECT 1 FROM thread_results WHERE delegation_id = ? AND sealed = 1)",
                (intent["delegation_id"], intent["delegation_id"]),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

    async def spawn_child(
        self,
        request: Request,
        *,
        parent_session_id: str,
        body: ThreadChildCreate,
        retry_of_delegation_id: str | None = None,
        caller: VerifiedThreadCaller | None = None,
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
        await run_db_operation_for_request(
            request,
            lambda db: self._authorize_spawn_parent(db, request, parent_session_id, caller),
        )
        # Operation-driven recovery is limited to the authorized parent.
        await reconcile_stale_provisioning(
            request,
            parent_session_id=parent_session_id,
            authorization_guard=lambda db: self._authorize_spawn_parent(
                db, request, parent_session_id, caller
            ),
        )
        if caller is not None:
            body = body.model_copy(
                update={
                    "idempotency_key": str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"yinshi:thread-spawn:{caller.run_id}:{caller.tool_call_id}",
                        )
                    ),
                }
            )
        idempotency_key = _canonical_idempotency_key(body.idempotency_key)
        workspace_service = ThreadWorkspaceService()
        reservation_id = uuid.uuid4().hex
        reservation = await run_db_operation_for_request(
            request,
            lambda db: self._reserve(
                db,
                request,
                parent_session_id,
                idempotency_key,
                body,
                retry_of_delegation_id,
                reservation_id=reservation_id,
                caller=caller,
            ),
        )
        outcome = ThreadSpawnOutcome.from_row(reservation)
        if (
            outcome.delegation_id != reservation_id
            or outcome.status != DELEGATION_STATUS_PROVISIONING
        ):
            # Only the reservation winner may create its Git artifacts.
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

        async def claim_namespace(namespace: str) -> None:
            await run_db_operation_for_request(
                request,
                lambda db: self._claim_git_artifacts(
                    db,
                    request,
                    context,
                    parent_session_id,
                    reservation_id,
                    namespace,
                    caller,
                ),
            )

        async def record_snapshot(namespace: str, ref: str, oid: str) -> None:
            await run_db_operation_for_request(
                request,
                lambda db: self._record_snapshot_intent(
                    db,
                    request,
                    context,
                    parent_session_id,
                    reservation_id,
                    namespace,
                    ref,
                    oid,
                    caller,
                ),
            )

        def load_owned_worktrees(db: sqlite3.Connection) -> tuple[ThreadGitWorktree, ...]:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._authorize_spawn_parent(db, request, parent_session_id, caller)
                self._assert_no_cancellation(db, parent_session_id)
                current = db.execute(
                    "SELECT * FROM thread_delegations WHERE id = ?", (reservation_id,)
                ).fetchone()
                if (
                    current is None
                    or current["status"] != DELEGATION_STATUS_PROVISIONING
                    or current["git_artifacts_claimed"] != 1
                    or current["cancel_scope"] is not None
                    or current["child_session_id"] is not None
                    or current["child_workspace_id"] is not None
                ):
                    raise ThreadGitOwnershipError()
                rows = db.execute(
                    "SELECT d.id, d.git_artifact_namespace, d.child_session_id, d.child_workspace_id, "
                    "c.workspace_id AS session_workspace_id, "
                    "child.path AS child_path, child.branch AS child_branch, child.kind AS child_kind "
                    "FROM thread_delegations d JOIN sessions p ON p.id = d.parent_session_id "
                    "JOIN workspaces w ON w.id = p.workspace_id JOIN repos r ON r.id = w.repo_id "
                    "LEFT JOIN sessions c ON c.id = d.child_session_id "
                    "LEFT JOIN workspaces child ON child.id = d.child_workspace_id "
                    "WHERE d.git_artifacts_claimed = 1 AND r.root_path = ? LIMIT 501",
                    (context.repo_path,),
                ).fetchall()
                if len(rows) > 500:
                    raise ThreadGitOwnershipError()
                owned = []
                for row in rows:
                    branch, path = workspace_service.child_artifact_location(
                        context.repo_path, str(row["id"])
                    )
                    if (
                        row["child_session_id"] is not None or row["child_workspace_id"] is not None
                    ) and (
                        row["child_session_id"] is None
                        or row["child_workspace_id"] is None
                        or row["session_workspace_id"] != row["child_workspace_id"]
                        or row["child_path"] != path
                        or row["child_branch"] != branch
                        or row["child_kind"] != "delegated"
                    ):
                        raise ThreadGitOwnershipError()
                    owned.append(
                        ThreadGitWorktree(str(row["id"]), str(row["git_artifact_namespace"]), path)
                    )
                return tuple(owned)
            finally:
                db.rollback()

        async def owned_worktrees() -> tuple[ThreadGitWorktree, ...]:
            return await run_db_operation_for_request(request, load_owned_worktrees)

        ownership = ThreadGitClaim(
            request_database_identity(request), claim_namespace, record_snapshot, owned_worktrees
        )
        try:
            staged = await workspace_service.create_child_git_artifacts(
                context, ownership=ownership
            )
        except BaseException:
            # Publish failure before cleanup without changing a terminal winner.
            await self._fail_reservation_best_effort(request, outcome.delegation_id)
            await cleanup_provisioning_artifacts(request, outcome.delegation_id)
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
            # An uncertain commit may already own an attached child.
            await self._fail_reservation_best_effort(request, outcome.delegation_id)
            await cleanup_provisioning_artifacts(request, outcome.delegation_id)
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

        def resolve(db: sqlite3.Connection) -> sqlite3.Row:
            return self._load_child_delegation(db, request, child_session_id)

        authorized = await run_db_operation_for_request(request, resolve)
        await reconcile_stale_provisioning(
            request,
            parent_session_id=str(authorized["parent_session_id"]),
            authorization_guard=lambda db: _authorize_parent(
                db, request, str(authorized["parent_session_id"])
            ),
        )
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
        caller: VerifiedThreadCaller | None = None,
        cascade: bool = False,
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
        if caller is not None or cascade:
            return await self._cancel_claimed_scope(request, thread_id, caller, cascade)
        row = await run_db_operation_for_request(
            request,
            lambda db: self._resolve_cancel_target(db, request, thread_id),
        )
        await reconcile_stale_provisioning(
            request,
            parent_session_id=str(row["parent_session_id"]),
            authorization_guard=lambda db: _authorize_parent(
                db, request, str(row["parent_session_id"])
            ),
        )
        delegation_id = str(row["id"])
        row = await self._reload_delegation(request, delegation_id)
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

    async def _cancel_claimed_scope(
        self,
        request: Request,
        thread_id: str,
        caller: VerifiedThreadCaller | None,
        cascade: bool,
    ) -> ThreadSpawnOutcome:
        targets = await run_db_operation_for_request(
            request,
            lambda db: self._claim_cancel_targets(db, request, thread_id, caller, cascade),
        )
        try:
            for target in reversed(targets):
                await self.cancel_child(request, thread_id=str(target["id"]))
            await run_db_operation_for_request(
                request, lambda db: self._clear_cancel_claims(db, targets)
            )
            return ThreadSpawnOutcome.from_row(
                await self._reload_delegation(request, str(targets[0]["id"]))
            )
        finally:
            self._notify_thread_change(request)

    def _claim_cancel_targets(
        self,
        db: sqlite3.Connection,
        request: Request,
        thread_id: str,
        caller: VerifiedThreadCaller | None,
        cascade: bool,
    ) -> list[sqlite3.Row]:
        """Persist the authorized cancellation barrier before any external stop."""
        db.execute("BEGIN IMMEDIATE")
        try:
            targets = self._cancel_targets(db, request, thread_id, caller, cascade)
            for index, row in enumerate(targets):
                scope = (
                    "subtree"
                    if index == 0 and (cascade or row["cancel_scope"] == "subtree")
                    else "self"
                )
                db.execute(
                    "UPDATE thread_delegations SET cancel_scope = CASE WHEN cancel_scope = 'subtree' "
                    "THEN cancel_scope ELSE ? END, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (scope, row["id"]),
                )
            db.commit()
            return targets
        except BaseException:
            db.rollback()
            raise

    @staticmethod
    def _clear_cancel_claims(db: sqlite3.Connection, targets: list[sqlite3.Row]) -> None:
        """Keep a subtree barrier until every selected initial run is quiescent."""
        db.execute("BEGIN IMMEDIATE")
        try:
            complete = []
            for target in targets:
                row = db.execute(
                    "SELECT * FROM thread_delegations WHERE id = ?", (target["id"],)
                ).fetchone()
                if row is None or row["status"] not in TERMINAL_DELEGATION_STATUSES:
                    continue
                run = db.execute(
                    "SELECT status FROM prompt_runs WHERE session_id = ? AND idempotency_key = ?",
                    (row["child_session_id"], initial_run_idempotency_key(str(row["id"]))),
                ).fetchone()
                if run is None or run["status"] in _RUN_STATUS_TO_DELEGATION_STATUS:
                    complete.append(row)
            all_done = len(complete) == len(targets)
            for row in complete:
                if row["cancel_scope"] != "subtree" or all_done:
                    db.execute(
                        "UPDATE thread_delegations SET cancel_scope = NULL WHERE id = ?",
                        (row["id"],),
                    )
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def _cancel_targets(
        self,
        db: sqlite3.Connection,
        request: Request,
        thread_id: str,
        caller: VerifiedThreadCaller | None,
        cascade: bool,
    ) -> list[sqlite3.Row]:
        """Authorize every selected node before any cancellation is requested."""
        root = (
            self.authorize_descendant(db, request, caller, thread_id)
            if caller is not None
            else self._resolve_cancel_target(db, request, thread_id)
        )
        cascade = cascade or root["cancel_scope"] == "subtree"
        targets = [root]
        frontier = [(root, 0)]
        seen = {str(root["id"])}
        while frontier and cascade:
            parent, depth = frontier.pop(0)
            if parent["child_session_id"] is None:
                continue
            children = db.execute(
                "SELECT id FROM thread_delegations WHERE parent_session_id = ? ORDER BY id LIMIT 501",
                (parent["child_session_id"],),
            ).fetchall()
            if children and depth >= 32:
                raise ThreadTreeLimitError("cancellation tree exceeds the depth bound")
            for child in children:
                identifier = str(child["id"])
                if identifier in seen or len(seen) >= 500:
                    raise ThreadTreeLimitError("cancellation tree exceeds its graph bound")
                seen.add(identifier)
                row = (
                    self.authorize_descendant(db, request, caller, identifier)
                    if caller is not None
                    else self._resolve_cancel_target(db, request, identifier)
                )
                targets.append(row)
                frontier.append((row, depth + 1))
        return targets

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
        finalized = await workspace_service.finalize_child_context(
            intent["context"],
            ownership=self._finalization_ownership(request, intent),
        )
        sealed = await run_db_operation_for_request(
            request,
            lambda db: self._commit_seal(db, request, intent, finalized),
        )
        return _project_result_row(sealed)

    def _load_seal_intent(
        self,
        db: sqlite3.Connection,
        request: Request,
        workspace_service: ThreadWorkspaceService,
        child_session_id: str,
        *,
        observed_terminal: bool = False,
    ) -> dict[str, Any]:
        """Authorize the child and capture the immutable seal context."""
        delegation = self._load_child_delegation(db, request, child_session_id)
        status = str(delegation["status"])
        if not observed_terminal and status not in TERMINAL_DELEGATION_STATUSES:
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
        if row is None or str(row["source"]) not in {"reported", "derived"}:
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
            "parent_session_id": str(delegation["parent_session_id"]),
            "git_artifacts_claimed": int(delegation["git_artifacts_claimed"]),
            "git_artifact_namespace": delegation["git_artifact_namespace"],
            "result_version": int(row["version"]),
            "context": context,
        }

    def _finalization_ownership(
        self, request: Request, intent: dict[str, Any]
    ) -> ThreadGitFinalization:
        if intent["git_artifacts_claimed"] != 1 or not intent["git_artifact_namespace"]:
            raise ThreadGitOwnershipError()

        async def validate_claim() -> None:
            def validate(db: sqlite3.Connection) -> None:
                db.execute("BEGIN IMMEDIATE")
                try:
                    self._validate_seal_identity(db, request, intent)
                finally:
                    db.rollback()

            await run_db_operation_for_request(request, validate)

        return ThreadGitFinalization(
            request_database_identity(request),
            str(intent["git_artifact_namespace"]),
            validate_claim,
        )

    def _validate_seal_identity(
        self,
        db: sqlite3.Connection,
        request: Request,
        intent: dict[str, Any],
        *,
        require_ownership: bool = True,
    ) -> None:
        """Reauthorize a fixed result and workspace identity inside a writer transaction."""
        from yinshi.config import get_settings

        if not get_settings().thread_hierarchy_enabled:
            raise ThreadHierarchyDisabledError()
        guard = intent.get("authorization_guard")
        if guard is not None:
            guard(db)
        delegation = self._load_child_delegation(db, request, str(intent["child_session_id"]))
        _authorize_parent(db, request, str(delegation["parent_session_id"]))
        if (
            str(delegation["id"]) != intent["delegation_id"]
            or str(delegation["parent_session_id"]) != intent["parent_session_id"]
            or str(delegation["child_workspace_id"]) != intent["child_workspace_id"]
            or str(delegation["base_commit"]) != intent["base_commit"]
            or delegation["git_artifact_namespace"] != intent["git_artifact_namespace"]
            or int(delegation["git_artifacts_claimed"]) != intent["git_artifacts_claimed"]
            or str(delegation["status"]) not in TERMINAL_DELEGATION_STATUSES
        ):
            raise ThreadResultSealConflictError("Delegation identity changed during finalization.")
        if require_ownership and (
            delegation["git_artifacts_claimed"] != 1 or not delegation["git_artifact_namespace"]
        ):
            raise ThreadGitOwnershipError()
        context = intent["context"]
        workspace = db.execute(
            "SELECT w.repo_id, w.path, r.root_path FROM workspaces w JOIN repos r ON r.id = w.repo_id WHERE w.id = ?",
            (intent["child_workspace_id"],),
        ).fetchone()
        if workspace is None or tuple(workspace) != (
            context.repo_id,
            context.workspace_path,
            context.repo_path,
        ):
            raise ThreadResultSealConflictError("Workspace identity changed during finalization.")
        if self._session_repository(db, str(delegation["parent_session_id"])) != context.repo_id:
            raise ThreadResultSealConflictError("Parent repository changed during finalization.")
        result = db.execute(
            "SELECT version FROM thread_results WHERE delegation_id = ?", (intent["delegation_id"],)
        ).fetchone()
        if result is None or int(result["version"]) != intent["result_version"]:
            raise ThreadResultSealConflictError("Result version changed during finalization.")

    def _commit_seal(
        self,
        db: sqlite3.Connection,
        request: Request,
        intent: dict[str, Any],
        finalized: FinalizedThreadGitResult,
    ) -> sqlite3.Row:
        """CAS the unsealed draft to sealed with the finalized Git identity."""
        delegation_id = str(intent["delegation_id"])
        db.execute("BEGIN IMMEDIATE")
        try:
            self._validate_seal_identity(db, request, intent)
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
            if "terminal_run_id" in intent:
                run = db.execute(
                    "SELECT status FROM prompt_runs WHERE id = ? AND session_id = ? "
                    "AND idempotency_key = ?",
                    (
                        intent["terminal_run_id"],
                        intent["child_session_id"],
                        initial_run_idempotency_key(delegation_id),
                    ),
                ).fetchone()
                if run is None or str(run["status"]) not in _RUN_STATUS_TO_DELEGATION_STATUS:
                    raise ThreadResultSealConflictError("initial run identity changed")
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
            if "terminal_status" in intent:
                db.execute(
                    "UPDATE thread_delegations SET status = CASE WHEN status IN ('completed', 'failed', 'cancelled', 'interrupted') "
                    "THEN status ELSE ? END, error_code = NULL, completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP), "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (intent["terminal_status"], delegation_id),
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

    async def report_agent_result(
        self,
        request: Request,
        *,
        caller: VerifiedThreadCaller,
        body: ThreadResultReportCreate,
    ) -> dict[str, Any]:
        """Record a child report with a durable receipt for its SDK call."""
        row = await run_db_operation_for_request(
            request,
            lambda db: self._report_result_write(
                db,
                request,
                caller.session_id,
                body,
                caller=caller,
            ),
        )
        return {"delegation_id": str(row["delegation_id"]), "version": int(row["version"])}

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
        authorized = await run_db_operation_for_request(
            request,
            lambda db: self._load_child_delegation(db, request, child_session_id),
        )
        await reconcile_stale_provisioning(
            request,
            parent_session_id=str(authorized["parent_session_id"]),
            authorization_guard=lambda db: _authorize_parent(
                db, request, str(authorized["parent_session_id"])
            ),
        )

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
        *,
        caller: VerifiedThreadCaller | None = None,
    ) -> sqlite3.Row:
        """Apply one report against the draft row inside one transaction."""
        db.execute("BEGIN IMMEDIATE")
        try:
            if caller is not None:
                self.authorize_caller(db, request, caller)
            delegation = self._load_child_delegation(db, request, child_session_id)
            delegation_id = str(delegation["id"])
            if caller is not None:
                initial = db.execute(
                    "SELECT id FROM prompt_runs WHERE id = ? AND session_id = ? AND idempotency_key = ?",
                    (caller.run_id, caller.session_id, initial_run_idempotency_key(delegation_id)),
                ).fetchone()
                if initial is None:
                    raise ThreadNotFoundError(child_session_id)
                receipt = db.execute(
                    "SELECT * FROM thread_report_calls WHERE run_id = ? AND tool_call_id = ?",
                    (caller.run_id, caller.tool_call_id),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["delegation_id"]) != delegation_id or str(
                        receipt["payload_json"]
                    ) != _report_canonical(_report_incoming_payload(body)):
                        raise ThreadIdempotencyConflictError("report call payload changed")
                    db.rollback()
                    return cast(sqlite3.Row, receipt)
            row = db.execute(
                "SELECT * FROM thread_results WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if caller is not None:
                body = body.model_copy(
                    update={"expected_version": 0 if row is None else int(row["version"])}
                )
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
            if caller is not None:
                db.execute(
                    "INSERT INTO thread_report_calls "
                    "(run_id, tool_call_id, delegation_id, payload_json, version) VALUES (?, ?, ?, ?, ?)",
                    (
                        caller.run_id,
                        caller.tool_call_id,
                        delegation_id,
                        _report_canonical(_report_incoming_payload(body)),
                        int(updated["version"]),
                    ),
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
        await cleanup_provisioning_artifacts(request, delegation_id)

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
        *,
        authorization_guard: Callable[[sqlite3.Connection], None] | None = None,
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
            await self._fail_queued_start(
                request, outcome.delegation_id, authorization_guard=authorization_guard
            )
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
                admission_guard=lambda db: self._admit_initial_run(
                    db, outcome, authorization_guard=authorization_guard
                ),
            )
        except ThreadOrchestrationError as exc:
            if exc.code == "thread_cancel_pending":
                if authorization_guard is None:
                    await self._cancel_queued(request, outcome.delegation_id)
                return ThreadSpawnOutcome.from_row(
                    await self._reload_delegation(request, outcome.delegation_id)
                )
            await self._fail_queued_start(
                request, outcome.delegation_id, authorization_guard=authorization_guard
            )
            raise ThreadPromptStartError() from exc
        except Exception as exc:
            await self._fail_queued_start(
                request, outcome.delegation_id, authorization_guard=authorization_guard
            )
            raise ThreadPromptStartError() from exc
        promoted = await self._mark_running(
            request, outcome, authorization_guard=authorization_guard
        )
        if promoted.status != DELEGATION_STATUS_RUNNING:
            await self._cancel_accepted_run(request, journal, accepted_run, promoted)
        return promoted

    def _admit_initial_run(
        self,
        db: sqlite3.Connection,
        outcome: ThreadSpawnOutcome,
        *,
        authorization_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        """Check child and ancestor claims in the journal's acceptance transaction."""
        if authorization_guard is not None:
            authorization_guard(db)
        row = db.execute(
            "SELECT * FROM thread_delegations WHERE id = ?", (outcome.delegation_id,)
        ).fetchone()
        if (
            row is None
            or row["status"] != DELEGATION_STATUS_QUEUED
            or row["cancel_scope"] is not None
        ):
            raise ThreadOrchestrationError(
                "thread_cancel_pending", "Child initial execution is no longer available."
            )
        assert outcome.child_session_id is not None
        self._assert_no_cancellation(db, outcome.child_session_id)
        from yinshi.config import get_settings

        settings = get_settings()
        if not settings.thread_hierarchy_enabled or (
            row["initiator"] == "agent" and not settings.agent_delegation_enabled
        ):
            raise ThreadHierarchyDisabledError()
        if row["initiator"] == "agent":
            origin = db.execute(
                "SELECT id FROM prompt_runs WHERE id = ? AND session_id = ? "
                "AND status = 'running'",
                (row["delegated_by_run_id"], row["parent_session_id"]),
            ).fetchone()
            if origin is None:
                raise ThreadOrchestrationError(
                    "thread_actor_inactive", "The originating agent run is no longer active."
                )

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
        *,
        authorization_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> ThreadSpawnOutcome:
        """Promote one queued delegation to running with a conditional update."""

        def mark(db: sqlite3.Connection) -> int:
            db.execute("BEGIN IMMEDIATE")
            try:
                if authorization_guard is not None:
                    authorization_guard(db)
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

    async def _fail_queued_start(
        self,
        request: Request,
        delegation_id: str,
        *,
        authorization_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
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
                if authorization_guard is not None:
                    authorization_guard(db)
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
                       WHERE id = ? AND status = ?
                         AND child_session_id IS NULL AND child_workspace_id IS NULL""",
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

    def _claim_git_artifacts(
        self,
        db: sqlite3.Connection,
        request: Request,
        context: ThreadParentGitContext,
        parent_session_id: str,
        reservation_id: str,
        namespace: str,
        caller: VerifiedThreadCaller | None,
    ) -> None:
        """Claim an available physical namespace while its Git lifecycle lock is held."""
        db.execute("BEGIN IMMEDIATE")
        try:
            from yinshi.config import get_settings

            if not get_settings().thread_hierarchy_enabled:
                raise ThreadHierarchyDisabledError()
            self._authorize_spawn_parent(db, request, parent_session_id, caller)
            row = db.execute(
                "SELECT * FROM thread_delegations WHERE id = ?", (context.delegation_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] != DELEGATION_STATUS_PROVISIONING
                or row["id"] != reservation_id
                or row["child_session_id"] is not None
                or row["cancel_scope"] is not None
                or row["git_artifacts_claimed"] != 0
                or row["parent_session_id"] != parent_session_id
            ):
                raise ThreadAttachConflictError(
                    "The provisioning reservation is no longer available."
                )
            self._assert_no_cancellation(db, parent_session_id)
            existing = db.execute(
                "SELECT id FROM thread_delegations WHERE git_artifacts_claimed = 1 AND git_artifact_namespace = ?",
                (namespace,),
            ).fetchone()
            if existing is not None:
                raise ThreadAttachConflictError("The thread Git namespace is already claimed.")
            db.execute(
                "UPDATE thread_delegations SET git_artifacts_claimed = 1, git_artifact_namespace = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (namespace, context.delegation_id),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def _record_snapshot_intent(
        self,
        db: sqlite3.Connection,
        request: Request,
        context: ThreadParentGitContext,
        parent_session_id: str,
        reservation_id: str,
        namespace: str,
        ref: str,
        oid: str,
        caller: VerifiedThreadCaller | None,
    ) -> None:
        """Commit immutable snapshot intent under the physical lifecycle lock."""
        from yinshi.config import get_settings

        db.execute("BEGIN IMMEDIATE")
        try:
            if not get_settings().thread_hierarchy_enabled:
                raise ThreadHierarchyDisabledError()
            self._authorize_spawn_parent(db, request, parent_session_id, caller)
            self._assert_no_cancellation(db, parent_session_id)
            row = db.execute(
                "SELECT * FROM thread_delegations WHERE id = ?", (context.delegation_id,)
            ).fetchone()
            if (
                row is None
                or row["id"] != reservation_id
                or row["parent_session_id"] != parent_session_id
                or row["status"] != DELEGATION_STATUS_PROVISIONING
                or row["child_session_id"] is not None
                or row["child_workspace_id"] is not None
                or row["cancel_scope"] is not None
                or row["git_artifacts_claimed"] != 1
                or row["git_artifact_namespace"] != namespace
            ):
                raise ThreadAttachConflictError(
                    "The provisioning reservation is no longer available."
                )
            parent = db.execute(
                "SELECT s.workspace_id, w.repo_id, w.path, r.root_path FROM sessions s "
                "JOIN workspaces w ON w.id = s.workspace_id JOIN repos r ON r.id = w.repo_id WHERE s.id = ?",
                (parent_session_id,),
            ).fetchone()
            if parent is None or tuple(parent) != (
                context.parent_workspace_id,
                context.repo_id,
                context.parent_workspace_path,
                context.repo_path,
            ):
                raise ThreadAttachConflictError("The provisioning parent has changed.")
            if db.execute(
                "SELECT 1 FROM thread_results WHERE delegation_id = ?", (reservation_id,)
            ).fetchone():
                raise ThreadAttachConflictError(
                    "The provisioning reservation already has a result."
                )
            if (
                ref != f"refs/yinshi/snapshots/{context.delegation_id}"
                or len(oid) not in {40, 64}
                or any(character not in "0123456789abcdef" for character in oid)
            ):
                raise ThreadAttachConflictError("The snapshot intent is invalid.")
            previous = (row["snapshot_ref"], row["base_commit"], row["base_kind"])
            if previous not in ((None, None, None), (ref, oid, "snapshot")):
                raise ThreadAttachConflictError("The snapshot intent has already been recorded.")
            db.execute(
                "UPDATE thread_delegations SET snapshot_ref = ?, base_commit = ?, base_kind = 'snapshot', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (ref, oid, reservation_id),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def _authorize_spawn_parent(
        self,
        db: sqlite3.Connection,
        request: Request,
        parent_session_id: str,
        caller: VerifiedThreadCaller | None,
    ) -> None:
        _authorize_parent(db, request, parent_session_id)
        if caller is not None:
            if caller.session_id != parent_session_id:
                raise ThreadNotFoundError(parent_session_id)
            self.authorize_caller(db, request, caller)

    def _reserve(
        self,
        db: sqlite3.Connection,
        request: Request,
        parent_session_id: str,
        idempotency_key: str,
        body: ThreadChildCreate,
        retry_of_delegation_id: str | None = None,
        *,
        reservation_id: str | None = None,
        caller: VerifiedThreadCaller | None = None,
    ) -> sqlite3.Row:
        """Insert one provisioning delegation inside one immediate transaction."""
        db.execute("BEGIN IMMEDIATE")
        try:
            self._authorize_spawn_parent(db, request, parent_session_id, caller)
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
            self._assert_no_cancellation(db, parent_session_id)
            _enforce_spawn_limits(db, request, parent_session_id)
            if caller is not None:
                from yinshi.config import get_settings

                count = db.execute(
                    "SELECT COUNT(*) FROM thread_delegations WHERE parent_session_id = ? "
                    "AND delegated_by_run_id = ? AND initiator = 'agent'",
                    (parent_session_id, caller.run_id),
                ).fetchone()[0]
                if int(count) >= get_settings().thread_max_spawns_per_turn:
                    raise ThreadOrchestrationError(
                        "spawn_limit_exceeded",
                        "The current turn has reached its child spawn limit.",
                    )
            db.execute(
                """INSERT INTO thread_delegations (
                       id, parent_session_id, idempotency_key, initiator,
                       title, task, context, role,
                       requested_model, requested_thinking, status,
                       retry_of_delegation_id, auto_start,
                       delegated_by_run_id, delegated_by_tool_call_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    reservation_id or uuid.uuid4().hex,
                    parent_session_id,
                    idempotency_key,
                    "agent" if caller is not None else "user",
                    body.title.strip(),
                    body.task.strip(),
                    None if body.context is None else body.context.strip() or None,
                    body.role,
                    normalize_model_ref(body.model),
                    None if body.thinking is None else body.thinking.strip() or None,
                    DELEGATION_STATUS_PROVISIONING,
                    retry_of_delegation_id,
                    int(body.start_immediately),
                    caller.run_id if caller is not None else None,
                    caller.tool_call_id if caller is not None else None,
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
