"""Durable prompt-event journal used by reconnectable runtime transports."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request

from yinshi.api.deps import get_db_for_request
from yinshi.config import get_settings
from yinshi.services.run_coordinator import get_run_coordinator

PromptExecutor = Callable[[Request, str, Any], AsyncIterator[dict[str, Any]]]
_EVENT_BYTES_MAX = 1_048_576
_EVENT_BATCH_BYTES_MAX = 48_000
_EVENT_COUNT_MAX = 100_000
_EVENT_BATCH_MAX = 100
_ACTIVE_STATUSES = frozenset({"starting", "running", "stopping"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
_RESOURCE_ID_LENGTH = 32


class PromptRunNotFoundError(LookupError):
    """Requested session or prompt run does not exist in the active tenant."""


class PromptRunConflictError(RuntimeError):
    """Session already owns a different active prompt run."""


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

    def __init__(self, *, executor: PromptExecutor | None = None) -> None:
        selected_executor = executor or _default_prompt_executor
        if not callable(selected_executor):
            raise TypeError("prompt executor must be callable")
        self._executor = selected_executor
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._tasks_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._recovered_database_paths: set[str] = set()
        self._closing = False

    async def start(
        self,
        *,
        request: Request,
        session_id: str,
        idempotency_key: str,
        body: Any,
    ) -> PromptRun:
        """Create one durable run or return its idempotent predecessor."""
        self._validate_resource_id(session_id, "session_id")
        self._validate_idempotency_key(idempotency_key)
        if self._closing:
            raise RuntimeError("prompt journal is closing")
        if body is None:
            raise TypeError("prompt body must not be None")
        await self._recover_database(request)
        async with self._start_lock:
            if self._closing:
                raise RuntimeError("prompt journal is closing")
            return await self._start_serialized(
                request=request,
                session_id=session_id,
                idempotency_key=idempotency_key,
                body=body,
            )

    async def _start_serialized(
        self,
        *,
        request: Request,
        session_id: str,
        idempotency_key: str,
        body: Any,
    ) -> PromptRun:
        """Commit and register one prompt task while concurrent starts are excluded."""
        with get_db_for_request(request) as database:
            existing = database.execute(
                """SELECT id, session_id, status FROM prompt_runs
                   WHERE session_id = ? AND idempotency_key = ?""",
                (session_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return self._row_to_run(existing)
            session = database.execute(
                "SELECT id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise PromptRunNotFoundError("session not found")
            run_id = uuid.uuid4().hex
            try:
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
                    return self._row_to_run(existing)
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

        task = asyncio.create_task(
            self._consume(request=request, session_id=session_id, run_id=run_id, body=body),
            name=f"prompt-journal-{run_id}",
        )
        async with self._tasks_lock:
            previous_task = self._tasks.setdefault(run_id, task)
        assert previous_task is task, "new run ID must not collide with an active task"
        return PromptRun(id=run_id, session_id=session_id, status="starting")

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

        with get_db_for_request(request) as database:
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
            status = run["status"]

        return PromptEventBatch(
            run_id=run_id,
            status=status,
            events=tuple(events),
            next_sequence=cursor,
        )

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
        with get_db_for_request(request) as database:
            row = database.execute(
                "SELECT id, session_id, status FROM prompt_runs WHERE id = ? AND session_id = ?",
                (run_id, session_id),
            ).fetchone()
            if row is None:
                raise PromptRunNotFoundError("prompt run not found")
            run = self._row_to_run(row)
            if run.status in _TERMINAL_STATUSES or run.status == "stopping":
                return run
            if run.status not in _ACTIVE_STATUSES:
                raise RuntimeError("prompt run has an invalid status")
            database.execute(
                """UPDATE prompt_runs SET status = 'stopping', updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (run_id,),
            )
            database.commit()

        cancellation_found = await get_run_coordinator().request_cancel(session_id)
        if not cancellation_found:
            async with self._tasks_lock:
                task = self._tasks.get(run_id)
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await self._append_terminal_event_safely(
                request,
                run_id,
                {"type": "cancelled", "reason": "user_stop"},
            )
            self._set_terminal_status(request, run_id, "cancelled")
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

    async def _recover_database(self, request: Request) -> None:
        """Mark active rows from a previous process as interrupted once per tenant DB."""
        database_path = self._database_path(request)
        async with self._recovery_lock:
            if database_path in self._recovered_database_paths:
                return
            event_json = json.dumps(
                {"type": "error", "error": "Prompt run was interrupted"},
                separators=(",", ":"),
                sort_keys=True,
            )
            with get_db_for_request(request) as database:
                database.execute("BEGIN IMMEDIATE")
                try:
                    rows = database.execute("""SELECT id, session_id FROM prompt_runs
                           WHERE status IN ('starting', 'running', 'stopping')""").fetchall()
                    for row in rows:
                        sequence_row = database.execute(
                            """SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence
                               FROM prompt_events WHERE run_id = ?""",
                            (row["id"],),
                        ).fetchone()
                        if sequence_row is None or type(sequence_row["next_sequence"]) is not int:
                            raise RuntimeError("prompt journal recovery sequence is invalid")
                        if sequence_row["next_sequence"] < _EVENT_COUNT_MAX:
                            database.execute(
                                """INSERT INTO prompt_events (run_id, sequence, event_json)
                                   VALUES (?, ?, ?)""",
                                (row["id"], sequence_row["next_sequence"], event_json),
                            )
                        database.execute(
                            "UPDATE sessions SET status = 'idle' WHERE id = ? AND status = 'running'",
                            (row["session_id"],),
                        )
                    database.execute("""UPDATE prompt_runs
                           SET status = 'interrupted', updated_at = CURRENT_TIMESTAMP
                           WHERE status IN ('starting', 'running', 'stopping')""")
                    database.commit()
                except (RuntimeError, sqlite3.Error):
                    database.rollback()
                    raise
            self._recovered_database_paths.add(database_path)

    async def _rearm_database_recovery(self, request: Request) -> None:
        """Require orphan recovery again after a terminal status write fails."""
        database_path = self._database_path(request)
        async with self._recovery_lock:
            self._recovered_database_paths.discard(database_path)

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
        try:
            self._set_status(request, run_id, "running", expected={"starting"})
            event_source = self._executor(request, session_id, body)
            async for event in event_source:
                await self._append_event(request, run_id, event)
                event_type = event.get("type")
                if event_type == "cancelled":
                    final_status = "cancelled"
                elif event_type == "error":
                    final_status = "failed"
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
                try:
                    try:
                        self._set_terminal_status(request, run_id, final_status)
                    except Exception:
                        with suppress(BaseException):
                            await self._rearm_database_recovery(request)
                        raise
                finally:
                    async with self._tasks_lock:
                        current_task = asyncio.current_task()
                        if self._tasks.get(run_id) is current_task:
                            self._tasks.pop(run_id, None)

    async def _append_terminal_event_safely(
        self,
        request: Request,
        run_id: str,
        event: dict[str, Any],
    ) -> None:
        """Best-effort terminal event persistence without blocking status cleanup."""
        try:
            await self._append_event(request, run_id, event, unless_terminal=True)
        except (PromptRunNotFoundError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            return

    async def _append_event(
        self,
        request: Request,
        run_id: str,
        event: dict[str, Any],
        *,
        unless_terminal: bool = False,
    ) -> None:
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("prompt journal event must have a string type")
        serialized = json.dumps(event, separators=(",", ":"), sort_keys=True)
        if len(serialized.encode("utf-8")) > _EVENT_BYTES_MAX:
            raise ValueError("prompt journal event exceeds the byte limit")
        with get_db_for_request(request) as database:
            run = database.execute(
                "SELECT status FROM prompt_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise PromptRunNotFoundError("prompt run not found")
            if unless_terminal and run["status"] in _TERMINAL_STATUSES:
                return
            sequence_row = database.execute(
                "SELECT MAX(sequence) AS sequence_max FROM prompt_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert sequence_row is not None
            sequence_max = sequence_row["sequence_max"]
            if sequence_max is not None and type(sequence_max) is not int:
                raise RuntimeError("prompt journal sequence is invalid")
            sequence = 0 if sequence_max is None else sequence_max + 1
            if sequence >= _EVENT_COUNT_MAX:
                raise RuntimeError("prompt journal event count exceeded the limit")
            database.execute(
                "INSERT INTO prompt_events (run_id, sequence, event_json) VALUES (?, ?, ?)",
                (run_id, sequence, serialized),
            )
            database.execute(
                "UPDATE prompt_runs SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (run_id,),
            )
            database.commit()

    def _set_status(
        self,
        request: Request,
        run_id: str,
        status: str,
        *,
        expected: set[str],
    ) -> None:
        with get_db_for_request(request) as database:
            placeholders = ",".join("?" for _ in expected)
            result = database.execute(
                f"""UPDATE prompt_runs SET status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status IN ({placeholders})""",
                (status, run_id, *sorted(expected)),
            )
            database.commit()
        if result.rowcount != 1:
            raise RuntimeError("prompt run status transition was rejected")

    def _set_terminal_status(self, request: Request, run_id: str, status: str) -> None:
        if status not in _TERMINAL_STATUSES:
            raise ValueError("prompt terminal status is invalid")
        with get_db_for_request(request) as database:
            database.execute(
                """UPDATE prompt_runs SET status = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status IN ('starting', 'running', 'stopping')""",
                (status, run_id),
            )
            database.commit()

    async def _load_run(
        self,
        *,
        request: Request,
        session_id: str,
        run_id: str,
    ) -> PromptRun:
        with get_db_for_request(request) as database:
            row = database.execute(
                "SELECT id, session_id, status FROM prompt_runs WHERE id = ? AND session_id = ?",
                (run_id, session_id),
            ).fetchone()
        if row is None:
            raise PromptRunNotFoundError("prompt run not found")
        return self._row_to_run(row)

    @staticmethod
    def _database_path(request: Request) -> str:
        tenant = getattr(request.state, "tenant", None)
        database_path = getattr(tenant, "db_path", None)
        if database_path is None:
            database_path = str(Path(get_settings().db_path).resolve())
        if not isinstance(database_path, str) or not database_path:
            raise RuntimeError("prompt journal request database is unavailable")
        return database_path

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
