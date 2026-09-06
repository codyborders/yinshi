"""Durable prompt-event journal used by reconnectable runtime transports."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from yinshi.api.deps import request_database_identity, run_db_operation_for_request
from yinshi.services.run_coordinator import CancelOutcome, get_run_coordinator

logger = logging.getLogger(__name__)

PromptExecutor = Callable[[Request, str, Any], AsyncIterator[dict[str, Any]]]
TerminalObserver = Callable[[Request, str, str, str], Awaitable[None]]
_EVENT_BYTES_MAX = 1_048_576
_EVENT_BATCH_BYTES_MAX = 48_000
_EVENT_COUNT_MAX = 100_000
_EVENT_BATCH_MAX = 100
_ACTIVE_STATUSES = frozenset({"starting", "running", "stopping"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
_RESOURCE_ID_LENGTH = 32
_TERMINAL_STATUS_ATTEMPTS = 3
_TERMINAL_STATUS_RETRY_DELAY_SECONDS = 0.05
_SQLITE_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
_SQLITE_BUSY_MESSAGES = frozenset({"database is locked", "database table is locked"})
_TERMINAL_EVENT_FINAL_STATUSES = {
    "result": "completed",
    "cancelled": "cancelled",
    "error": "failed",
}
_ACTIVE_PROMPT_RUN_ID: ContextVar[str | None] = ContextVar(
    "active_prompt_run_id",
    default=None,
)


def get_active_prompt_run_id() -> str | None:
    """Return the journal run that owns the current stream turn."""
    return _ACTIVE_PROMPT_RUN_ID.get()


class PromptRunNotFoundError(LookupError):
    """Requested session or prompt run does not exist in the active tenant."""


class PromptRunConflictError(RuntimeError):
    """Session already owns a different active prompt run."""


class _PromptRunStoppingError(RuntimeError):
    """Prompt cancellation won before executor startup."""


async def _drain_task_after_cancellation(attempt: asyncio.Task[Any]) -> None:
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


@dataclass(frozen=True, slots=True)
class PromptRun:
    """Public durable state for one prompt execution."""

    id: str
    session_id: str
    status: str


@dataclass(frozen=True, slots=True)
class PromptEventBatch:
    """Ordered journal events and next reconnect cursor."""

    run_id: str
    status: str
    events: tuple[dict[str, Any], ...]
    next_sequence: int


def recover_prompt_database(
    database: sqlite3.Connection,
    *,
    reset_sessions: bool = False,
) -> list[PromptRun]:
    """Commit orphan recovery for the journal and a newly activated worker.

    Only worker process activation may reset all recovered sessions. Lazy
    journal recovery must preserve a session already owned by another turn.
    No observer or external operation runs inside this transaction.
    """
    recovered = []
    event_json = json.dumps(
        {"type": "error", "error": "Prompt run was interrupted"},
        separators=(",", ":"),
        sort_keys=True,
    )
    database.execute("BEGIN IMMEDIATE")
    try:
        rows = database.execute(
            "SELECT id, session_id, status FROM prompt_runs WHERE status IN ('starting', 'running', 'stopping')",
        ).fetchall()
        for row in rows:
            derived = PromptJournal._derive_terminal_status_from_events(database, row["id"])
            if derived is None:
                sequence = database.execute(
                    "SELECT COALESCE(MAX(sequence), -1) + 1 FROM prompt_events WHERE run_id = ?",
                    (row["id"],),
                ).fetchone()
                if sequence is None or type(sequence[0]) is not int:
                    raise RuntimeError("prompt journal recovery sequence is invalid")
                if sequence[0] < _EVENT_COUNT_MAX:
                    database.execute(
                        "INSERT INTO prompt_events (run_id, sequence, event_json) VALUES (?, ?, ?)",
                        (row["id"], sequence[0], event_json),
                    )
                status = "interrupted"
            else:
                status = "cancelled" if row["status"] == "stopping" else derived
            database.execute(
                "UPDATE sessions SET status = 'idle' WHERE id = ? AND status = 'running' "
                "AND (? OR ? = (SELECT turn_id FROM messages WHERE session_id = ? AND role = 'user' ORDER BY rowid DESC LIMIT 1))",
                (row["session_id"], reset_sessions, row["id"], row["session_id"]),
            )
            database.execute(
                "UPDATE prompt_runs SET status = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status IN ('starting', 'running', 'stopping')",
                (status, row["id"]),
            )
            recovered.append(
                PromptRun(id=str(row["id"]), session_id=str(row["session_id"]), status=status)
            )
        database.commit()
        return recovered
    except BaseException:
        database.rollback()
        raise


async def _default_prompt_executor(
    request: Request,
    session_id: str,
    body: Any,
) -> AsyncIterator[dict[str, Any]]:
    """Adapt the existing SSE route generator to structured journal events."""
    from yinshi.api.stream import prompt_session

    response = await prompt_session(session_id, body, request)
    body_iterator = response.body_iterator
    buffer = ""
    try:
        async for chunk in body_iterator:
            if isinstance(chunk, str):
                text = chunk
            elif isinstance(chunk, bytes):
                text = chunk.decode("utf-8", errors="strict")
            else:
                raise TypeError("prompt stream yielded an invalid chunk type")
            buffer += text
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", maxsplit=1)
                data_lines = [line[6:] for line in frame.splitlines() if line.startswith("data: ")]
                if len(data_lines) != 1:
                    raise ValueError("prompt stream yielded an invalid SSE frame")
                event = json.loads(data_lines[0])
                if not isinstance(event, dict):
                    raise ValueError("prompt stream event must be an object")
                yield event
        if buffer.strip():
            raise ValueError("prompt stream ended with an incomplete SSE frame")
    finally:
        close_body_iterator = getattr(body_iterator, "aclose", None)
        if close_body_iterator is not None:
            await close_body_iterator()


class PromptJournal:
    """Run prompts in background tasks while durably journaling ordered events."""

    def __init__(
        self,
        *,
        executor: PromptExecutor | None = None,
        terminal_observer: TerminalObserver | None = None,
    ) -> None:
        selected_executor = executor or _default_prompt_executor
        if not callable(selected_executor):
            raise TypeError("prompt executor must be callable")
        self._executor = selected_executor
        if terminal_observer is not None and not callable(terminal_observer):
            raise TypeError("terminal observer must be callable")
        self._terminal_observer = terminal_observer
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._tasks_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._recovered_database_paths: set[str] = set()
        self._pending_terminal_statuses: dict[str, tuple[str, str]] = {}
        self._append_locks: dict[str, asyncio.Lock] = {}
        self._closing = False

    async def start(
        self,
        *,
        request: Request,
        session_id: str,
        idempotency_key: str,
        body: Any,
        admission_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> PromptRun:
        """Create one run, with an optional synchronous guard inside its write transaction.

        The guard must not perform external I/O, control the transaction, or
        retain the connection. Existing-run replay does not invoke it again.
        """
        self._validate_resource_id(session_id, "session_id")
        self._validate_idempotency_key(idempotency_key)
        if self._closing:
            raise RuntimeError("prompt journal is closing")
        if body is None:
            raise TypeError("prompt body must not be None")
        await self._recover_database(request)
        await self._reconcile_pending_for_database(request)
        async with self._start_lock:
            if self._closing:
                raise RuntimeError("prompt journal is closing")
            start_task = asyncio.create_task(
                self._start_serialized(
                    request=request,
                    session_id=session_id,
                    idempotency_key=idempotency_key,
                    body=body,
                    admission_guard=admission_guard,
                )
            )
            try:
                return await asyncio.shield(start_task)
            except asyncio.CancelledError:
                await _drain_task_after_cancellation(start_task)
                raise

    async def _start_serialized(
        self,
        *,
        request: Request,
        session_id: str,
        idempotency_key: str,
        body: Any,
        admission_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> PromptRun:
        """Commit and register one prompt task while concurrent starts are excluded."""
        run_id = uuid.uuid4().hex

        def create_run(database: sqlite3.Connection) -> tuple[PromptRun, bool]:
            existing = database.execute(
                """SELECT id, session_id, status FROM prompt_runs
                   WHERE session_id = ? AND idempotency_key = ?""",
                (session_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                run = self._row_to_run(existing)
                return run, run.id == run_id
            session = database.execute(
                "SELECT id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise PromptRunNotFoundError("session not found")
            try:
                database.execute("BEGIN IMMEDIATE")
                # Another writer can accept this key before this lock is held.
                existing = database.execute(
                    "SELECT id, session_id, status FROM prompt_runs WHERE session_id = ? AND idempotency_key = ?",
                    (session_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    database.rollback()
                    run = self._row_to_run(existing)
                    return run, run.id == run_id
                if admission_guard is not None:
                    admission_guard(database)
                database.execute(
                    """INSERT INTO prompt_runs
                       (id, session_id, idempotency_key, status)
                       VALUES (?, ?, ?, 'starting')""",
                    (run_id, session_id, idempotency_key),
                )
                database.commit()
            except sqlite3.IntegrityError as exc:
                database.rollback()
                existing = database.execute(
                    """SELECT id, session_id, status FROM prompt_runs
                       WHERE session_id = ? AND idempotency_key = ?""",
                    (session_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    run = self._row_to_run(existing)
                    return run, run.id == run_id
                active = database.execute(
                    """SELECT id FROM prompt_runs
                       WHERE session_id = ?
                         AND status IN ('starting', 'running', 'stopping')""",
                    (session_id,),
                ).fetchone()
                if active is not None:
                    raise PromptRunConflictError(
                        "session already has an active prompt run"
                    ) from exc
                raise
            except BaseException:
                database.rollback()
                raise
            return PromptRun(id=run_id, session_id=session_id, status="starting"), True

        run, created = await run_db_operation_for_request(request, create_run)
        async with self._tasks_lock:
            active_task = self._tasks.get(run.id)
            should_start = created or (run.status == "starting" and active_task is None)
            if not should_start:
                return run
            task = asyncio.create_task(
                self._consume(
                    request=request,
                    session_id=session_id,
                    run_id=run.id,
                    body=body,
                ),
                name=f"prompt-journal-{run.id}",
            )
            previous_task = self._tasks.setdefault(run.id, task)
        assert previous_task is task, "new run ID must not collide with an active task"
        return run

    async def active(
        self,
        *,
        request: Request,
        session_id: str,
    ) -> PromptRun | None:
        """Return the session's current durable run without starting another."""
        self._validate_resource_id(session_id, "session_id")
        await self._recover_database(request)
        await self._reconcile_pending_for_database(request)

        def read_active(database: sqlite3.Connection) -> PromptRun | None:
            row = database.execute(
                """SELECT id, session_id, status FROM prompt_runs
                   WHERE session_id = ?
                     AND status IN ('starting', 'running', 'stopping')""",
                (session_id,),
            ).fetchone()
            return self._row_to_run(row) if row is not None else None

        return await run_db_operation_for_request(request, read_active)

    async def events(
        self,
        *,
        request: Request,
        session_id: str,
        run_id: str,
        next_sequence: int,
    ) -> PromptEventBatch:
        """Read one bounded ordered event page from an exact reconnect cursor."""
        self._validate_resource_id(session_id, "session_id")
        self._validate_resource_id(run_id, "run_id")
        if type(next_sequence) is not int or next_sequence < 0:
            raise ValueError("next_sequence must be a non-negative integer")
        await self._recover_database(request)
        await self._reconcile_pending_terminal_status(request, run_id)

        def read_events(database: sqlite3.Connection) -> PromptEventBatch:
            run = database.execute(
                "SELECT id, status FROM prompt_runs WHERE id = ? AND session_id = ?",
                (run_id, session_id),
            ).fetchone()
            if run is None:
                raise PromptRunNotFoundError("prompt run not found")
            rows = database.execute(
                """SELECT sequence, event_json FROM prompt_events
                   WHERE run_id = ? AND sequence >= ?
                   ORDER BY sequence ASC LIMIT ?""",
                (run_id, next_sequence, _EVENT_BATCH_MAX),
            )
            events: list[dict[str, Any]] = []
            cursor = next_sequence
            event_bytes = 0
            for row in rows:
                sequence = row["sequence"]
                if type(sequence) is not int or sequence != cursor:
                    raise RuntimeError("prompt journal sequence is not contiguous")
                serialized_event = row["event_json"]
                if not isinstance(serialized_event, str):
                    raise RuntimeError("prompt journal event JSON is invalid")
                serialized_bytes = len(serialized_event.encode("utf-8"))
                if events and event_bytes + serialized_bytes > _EVENT_BATCH_BYTES_MAX:
                    break
                event = json.loads(serialized_event)
                if not isinstance(event, dict):
                    raise RuntimeError("prompt journal event is not an object")
                events.append(event)
                event_bytes += serialized_bytes
                cursor += 1
            return PromptEventBatch(
                run_id=run_id,
                status=run["status"],
                events=tuple(events),
                next_sequence=cursor,
            )

        return await run_db_operation_for_request(request, read_events)

    async def cancel(
        self,
        *,
        request: Request,
        session_id: str,
        run_id: str,
    ) -> PromptRun:
        """Request cancellation once and return stable terminal state on retries."""
        self._validate_resource_id(session_id, "session_id")
        self._validate_resource_id(run_id, "run_id")
        await self._recover_database(request)
        await self._reconcile_pending_terminal_status(request, run_id)

        def request_stop(database: sqlite3.Connection) -> tuple[PromptRun, bool]:
            row = database.execute(
                "SELECT id, session_id, status FROM prompt_runs WHERE id = ? AND session_id = ?",
                (run_id, session_id),
            ).fetchone()
            if row is None:
                raise PromptRunNotFoundError("prompt run not found")
            run = self._row_to_run(row)
            if run.status in _TERMINAL_STATUSES:
                return run, False
            if run.status == "stopping":
                return run, True
            if run.status not in _ACTIVE_STATUSES:
                raise RuntimeError("prompt run has an invalid status")
            result = database.execute(
                """UPDATE prompt_runs SET status = 'stopping', updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status IN ('starting', 'running')""",
                (run_id,),
            )
            if result.rowcount == 1:
                database.commit()
                return (
                    PromptRun(
                        id=run.id,
                        session_id=run.session_id,
                        status="stopping",
                    ),
                    True,
                )
            database.rollback()
            current = database.execute(
                "SELECT id, session_id, status FROM prompt_runs WHERE id = ? AND session_id = ?",
                (run_id, session_id),
            ).fetchone()
            if current is None:
                raise PromptRunNotFoundError("prompt run not found")
            reconciled = self._row_to_run(current)
            if reconciled.status in _TERMINAL_STATUSES:
                return reconciled, False
            if reconciled.status == "stopping":
                return reconciled, True
            raise RuntimeError("prompt cancellation transition was rejected")

        run, should_cancel = await run_db_operation_for_request(request, request_stop)
        if not should_cancel:
            return run
        cancellation_outcome = await get_run_coordinator().request_cancel(session_id)
        if cancellation_outcome is CancelOutcome.ABSENT:
            async with self._tasks_lock:
                task = self._tasks.get(run_id)
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await self._append_event(
                request,
                run_id,
                {"type": "cancelled", "reason": "user_stop"},
                deduplicate=True,
            )
            await self._set_terminal_status(request, run_id, "cancelled")
            self._append_locks.pop(run_id, None)
        return await self._load_run(request=request, session_id=session_id, run_id=run_id)

    async def close(self) -> None:
        """Cancel active tasks and wait for their cleanup before app shutdown."""
        async with self._start_lock:
            async with self._tasks_lock:
                self._closing = True
                tasks = tuple(self._tasks.values())
            for task in tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                async with self._tasks_lock:
                    self._tasks.clear()
                    self._append_locks.clear()

    @staticmethod
    def _derive_terminal_status_from_events(
        database: sqlite3.Connection,
        run_id: str,
    ) -> str | None:
        """Derive one terminal status from the last persisted terminal event."""
        for row in database.execute(
            "SELECT event_json FROM prompt_events WHERE run_id = ? ORDER BY sequence DESC",
            (run_id,),
        ):
            try:
                event = json.loads(row["event_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if not isinstance(event_type, str):
                continue
            final_status = _TERMINAL_EVENT_FINAL_STATUSES.get(event_type)
            if final_status is not None:
                return final_status
        return None

    async def recover(self, request: Request) -> None:
        """Recover the selected database before runtime or account activation."""
        await self._recover_database(request)

    async def _recover_database(self, request: Request) -> None:
        """Mark active rows from a previous process as interrupted once per tenant DB."""
        database_path = self._database_path(request)
        async with self._recovery_lock:
            if database_path in self._recovered_database_paths:
                return
            recovered = await run_db_operation_for_request(request, recover_prompt_database)
            self._recovered_database_paths.add(database_path)
        # Observers can perform Git work or re-enter the journal. No database or
        # recovery lock remains held across these best-effort notifications.
        for run in recovered:
            await self._set_terminal_status(request, run.id, run.status)

    async def _consume(
        self,
        *,
        request: Request,
        session_id: str,
        run_id: str,
        body: Any,
    ) -> None:
        final_status = "completed"
        event_source: AsyncIterator[dict[str, Any]] | None = None
        executor_token: Token[str | None] | None = None
        try:
            await self._set_status(
                request,
                run_id,
                "running",
                expected={"starting"},
                background=True,
            )
            executor_token = _ACTIVE_PROMPT_RUN_ID.set(run_id)
            event_source = self._executor(request, session_id, body)
            async for event in event_source:
                await self._append_event(
                    request,
                    run_id,
                    event,
                    deduplicate=event.get("type") == "cancelled",
                    background=True,
                )
                event_type = event.get("type")
                if event_type == "cancelled":
                    final_status = "cancelled"
                elif event_type == "error":
                    final_status = "failed"
        except _PromptRunStoppingError:
            final_status = "cancelled"
        except asyncio.CancelledError:
            if self._closing:
                final_status = "interrupted"
                event = {"type": "error", "error": "Prompt run was interrupted"}
            else:
                final_status = "cancelled"
                event = {"type": "cancelled", "reason": "user_stop"}
            await self._append_terminal_event_safely(request, run_id, event)
            raise
        except Exception:
            final_status = "failed"
            await self._append_terminal_event_safely(
                request,
                run_id,
                {"type": "error", "error": "Prompt execution failed"},
            )
        finally:
            try:
                if event_source is not None:
                    close_event_source = getattr(event_source, "aclose", None)
                    if close_event_source is not None:
                        with suppress(Exception):
                            await close_event_source()
            finally:
                if executor_token is not None:
                    _ACTIVE_PROMPT_RUN_ID.reset(executor_token)
                try:
                    try:
                        await self._persist_terminal_status(
                            request,
                            run_id,
                            final_status,
                        )
                    except Exception:
                        await self._remember_pending_terminal_status(
                            request,
                            run_id,
                            final_status,
                        )
                        logger.exception("Prompt terminal status persistence failed")
                        raise
                finally:
                    async with self._tasks_lock:
                        current_task = asyncio.current_task()
                        if self._tasks.get(run_id) is current_task:
                            self._tasks.pop(run_id, None)
                            self._append_locks.pop(run_id, None)

    async def _append_terminal_event_safely(
        self,
        request: Request,
        run_id: str,
        event: dict[str, Any],
    ) -> None:
        """Best-effort terminal event persistence without blocking status cleanup."""
        try:
            await self._append_event(
                request,
                run_id,
                event,
                unless_terminal=True,
                deduplicate=event.get("type") == "cancelled",
                background=True,
            )
        except (PromptRunNotFoundError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            return

    async def _append_event(
        self,
        request: Request,
        run_id: str,
        event: dict[str, Any],
        *,
        unless_terminal: bool = False,
        deduplicate: bool = False,
        background: bool = False,
    ) -> None:
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("prompt journal event must have a string type")
        serialized = json.dumps(event, separators=(",", ":"), sort_keys=True)
        if len(serialized.encode("utf-8")) > _EVENT_BYTES_MAX:
            raise ValueError("prompt journal event exceeds the byte limit")
        intended_sequence: int | None = None
        append_lock = self._append_locks.setdefault(run_id, asyncio.Lock())

        def append(database: sqlite3.Connection) -> None:
            nonlocal intended_sequence
            database.execute("BEGIN IMMEDIATE")
            run = database.execute(
                "SELECT status FROM prompt_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise PromptRunNotFoundError("prompt run not found")
            if unless_terminal and (
                run["status"] in _TERMINAL_STATUSES or run["status"] == "stopping"
            ):
                database.rollback()
                return
            if deduplicate:
                duplicate = database.execute(
                    "SELECT 1 FROM prompt_events WHERE run_id = ? AND event_json = ? LIMIT 1",
                    (run_id, serialized),
                ).fetchone()
                if duplicate is not None:
                    database.rollback()
                    return
            if intended_sequence is None:
                sequence_row = database.execute(
                    "SELECT MAX(sequence) AS sequence_max FROM prompt_events WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                assert sequence_row is not None
                sequence_max = sequence_row["sequence_max"]
                if sequence_max is not None and type(sequence_max) is not int:
                    raise RuntimeError("prompt journal sequence is invalid")
                intended_sequence = 0 if sequence_max is None else sequence_max + 1
            if intended_sequence >= _EVENT_COUNT_MAX:
                raise RuntimeError("prompt journal event count exceeded the limit")
            existing = database.execute(
                "SELECT event_json FROM prompt_events WHERE run_id = ? AND sequence = ?",
                (run_id, intended_sequence),
            ).fetchone()
            if existing is not None:
                if existing["event_json"] != serialized:
                    raise RuntimeError("prompt journal sequence contains a different event")
                database.rollback()
                return
            database.execute(
                "INSERT INTO prompt_events (run_id, sequence, event_json) VALUES (?, ?, ?)",
                (run_id, intended_sequence, serialized),
            )
            database.execute(
                "UPDATE prompt_runs SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (run_id,),
            )
            database.commit()

        async with append_lock:
            await run_db_operation_for_request(
                request,
                append,
                shared_request_budget=not background,
            )

    async def _set_status(
        self,
        request: Request,
        run_id: str,
        status: str,
        *,
        expected: set[str],
        background: bool = False,
    ) -> None:
        def update(database: sqlite3.Connection) -> None:
            row = database.execute(
                "SELECT status FROM prompt_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is not None and row["status"] == status:
                return
            if status == "running" and row is not None and row["status"] == "stopping":
                raise _PromptRunStoppingError("prompt cancellation won before startup")
            placeholders = ",".join("?" for _ in expected)
            result = database.execute(
                f"""UPDATE prompt_runs SET status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status IN ({placeholders})""",
                (status, run_id, *sorted(expected)),
            )
            database.commit()
            if result.rowcount != 1:
                raise RuntimeError("prompt run status transition was rejected")

        await run_db_operation_for_request(
            request,
            update,
            shared_request_budget=not background,
        )

    async def _remember_pending_terminal_status(
        self,
        request: Request,
        run_id: str,
        status: str,
    ) -> None:
        """Retain an intended terminal transition after persistence fails."""
        pending = (self._database_path(request), status)
        async with self._tasks_lock:
            self._pending_terminal_statuses[run_id] = pending

    async def _reconcile_pending_terminal_status(
        self,
        request: Request,
        run_id: str,
    ) -> None:
        """Retry one retained terminal transition before returning its run."""
        database_path = self._database_path(request)
        async with self._tasks_lock:
            pending = self._pending_terminal_statuses.get(run_id)
        if pending is None or pending[0] != database_path:
            return
        await self._persist_terminal_status(request, run_id, pending[1])
        async with self._tasks_lock:
            if self._pending_terminal_statuses.get(run_id) == pending:
                self._pending_terminal_statuses.pop(run_id, None)

    async def _reconcile_pending_for_database(self, request: Request) -> None:
        """Reconcile retained transitions before starting another prompt."""
        database_path = self._database_path(request)
        async with self._tasks_lock:
            run_ids = tuple(
                run_id
                for run_id, pending in self._pending_terminal_statuses.items()
                if pending[0] == database_path
            )
        for run_id in run_ids:
            await self._reconcile_pending_terminal_status(request, run_id)

    async def _persist_terminal_status(
        self,
        request: Request,
        run_id: str,
        status: str,
    ) -> None:
        """Persist one idempotent terminal transition across bounded lock contention."""
        for attempt in range(_TERMINAL_STATUS_ATTEMPTS):
            try:
                await self._set_terminal_status(
                    request,
                    run_id,
                    status,
                    background=True,
                )
                return
            except Exception as exc:
                if not self._is_sqlite_busy(exc) or attempt + 1 >= _TERMINAL_STATUS_ATTEMPTS:
                    raise
                await asyncio.sleep(_TERMINAL_STATUS_RETRY_DELAY_SECONDS * (attempt + 1))
        raise AssertionError("Terminal status attempts must return or raise")

    @staticmethod
    def _is_sqlite_busy(exc: Exception) -> bool:
        """Return whether an exception is exact SQLite lock contention."""
        error_code = getattr(exc, "sqlite_errorcode", None)
        if type(error_code) is int and error_code & 0xFF in _SQLITE_BUSY_CODES:
            return True
        return str(exc) in _SQLITE_BUSY_MESSAGES

    async def _set_terminal_status(
        self,
        request: Request,
        run_id: str,
        status: str,
        *,
        background: bool = False,
    ) -> None:
        if status not in _TERMINAL_STATUSES:
            raise ValueError("prompt terminal status is invalid")

        def update(database: sqlite3.Connection) -> tuple[str, str]:
            row = database.execute(
                "SELECT status, session_id FROM prompt_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise PromptRunNotFoundError("prompt run not found")
            if row["status"] in _TERMINAL_STATUSES:
                return str(row["session_id"]), str(row["status"])
            effective_status = "cancelled" if row["status"] == "stopping" else status
            result = database.execute(
                """UPDATE prompt_runs SET status = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status IN ('starting', 'running', 'stopping')""",
                (effective_status, run_id),
            )
            if result.rowcount != 1:
                database.rollback()
                current = database.execute(
                    "SELECT status FROM prompt_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if current is not None and current["status"] in _TERMINAL_STATUSES:
                    return str(row["session_id"]), str(current["status"])
                raise RuntimeError("prompt terminal status transition was rejected")
            database.execute(
                """UPDATE sessions SET status = 'idle'
                   WHERE id = ? AND status = 'running'
                     AND ? = (
                         SELECT turn_id FROM messages
                         WHERE session_id = ? AND role = 'user'
                         ORDER BY rowid DESC LIMIT 1
                     )""",
                (row["session_id"], run_id, row["session_id"]),
            )
            database.commit()
            return str(row["session_id"]), effective_status

        session_id, effective_status = await run_db_operation_for_request(
            request,
            update,
            shared_request_budget=not background,
        )
        if self._terminal_observer is not None:
            try:
                await self._terminal_observer(request, session_id, run_id, effective_status)
            except Exception:  # noqa: BLE001
                # Optional observers must preserve the committed run outcome.
                # Reconciliation retries observation from the committed journal state.
                logger.warning("Thread terminal observation failed")

    async def _load_run(
        self,
        *,
        request: Request,
        session_id: str,
        run_id: str,
    ) -> PromptRun:
        def load(database: sqlite3.Connection) -> PromptRun:
            row = database.execute(
                "SELECT id, session_id, status FROM prompt_runs WHERE id = ? AND session_id = ?",
                (run_id, session_id),
            ).fetchone()
            if row is None:
                raise PromptRunNotFoundError("prompt run not found")
            return self._row_to_run(row)

        return await run_db_operation_for_request(request, load)

    @staticmethod
    def _database_path(request: Request) -> str:
        return request_database_identity(request)

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> PromptRun:
        run = PromptRun(id=row["id"], session_id=row["session_id"], status=row["status"])
        if len(run.id) != _RESOURCE_ID_LENGTH or run.status not in (
            _ACTIVE_STATUSES | _TERMINAL_STATUSES
        ):
            raise RuntimeError("prompt run row is invalid")
        return run

    @staticmethod
    def _validate_resource_id(value: str, name: str) -> None:
        if not isinstance(value, str) or len(value) != _RESOURCE_ID_LENGTH:
            raise ValueError(f"{name} must contain exactly 32 characters")
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError(f"{name} must be lowercase hexadecimal")

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("idempotency_key must be a string")
        try:
            normalized = str(uuid.UUID(value))
        except ValueError as exc:
            raise ValueError("idempotency_key must be a UUID") from exc
        if normalized != value:
            raise ValueError("idempotency_key must be canonical")
