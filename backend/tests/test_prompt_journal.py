"""Verify durable, ordered prompt events and idempotent cancellation.

Tests use the real SQLite journal with injected event generators so reconnect and
cancellation behavior are checked without starting a sidecar process.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from fastapi.responses import StreamingResponse

from yinshi.api.deps import get_db_for_request
from yinshi.services import prompt_journal
from yinshi.services.prompt_journal import PromptJournal


def _request() -> Request:
    app = SimpleNamespace(state=SimpleNamespace())
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
            "app": app,
            "state": {},
        }
    )


def _seed_session(database: sqlite3.Connection) -> str:
    repository_id = uuid.uuid4().hex
    workspace_id = uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    database.execute(
        "INSERT INTO repos (id, name, root_path) VALUES (?, 'repo', '/tmp/repo')",
        (repository_id,),
    )
    database.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path)
           VALUES (?, ?, 'workspace', 'branch', '/tmp/workspace')""",
        (workspace_id, repository_id),
    )
    database.execute(
        "INSERT INTO sessions (id, workspace_id) VALUES (?, ?)",
        (session_id, workspace_id),
    )
    database.commit()
    return session_id


async def _wait_for_terminal(
    journal: PromptJournal,
    request: Request,
    session_id: str,
    run_id: str,
) -> str:
    for _ in range(100):
        batch = await journal.events(
            request=request,
            session_id=session_id,
            run_id=run_id,
            next_sequence=0,
        )
        if batch.status not in {"starting", "running", "stopping"}:
            return batch.status
        await asyncio.sleep(0)
    raise AssertionError("prompt journal did not reach a terminal state")


@pytest.mark.asyncio
async def test_default_executor_adapts_existing_sse_stream(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The journal consumes existing SSE chunks without changing stream behavior."""
    session_id = _seed_session(db)

    async def stream_chunks() -> AsyncIterator[str]:
        yield 'data: {"status":"started","type":"status"}\n'
        yield "\n"
        yield 'data: {"type":"result","usage":{}}\n\n'

    async def prompt_session(session_id: str, body: Any, request: Request) -> StreamingResponse:
        return StreamingResponse(stream_chunks(), media_type="text/event-stream")

    monkeypatch.setattr("yinshi.api.stream.prompt_session", prompt_session)
    journal_request = _request()
    journal = PromptJournal()
    run = await journal.start(
        request=journal_request,
        session_id=session_id,
        idempotency_key=str(uuid.uuid4()),
        body={"prompt": "hello"},
    )

    assert await _wait_for_terminal(journal, journal_request, session_id, run.id) == "completed"
    batch = await journal.events(
        request=journal_request,
        session_id=session_id,
        run_id=run.id,
        next_sequence=0,
    )
    assert [event["type"] for event in batch.events] == ["status", "result"]
    await journal.close()


@pytest.mark.asyncio
async def test_default_executor_closes_sse_body_iterator_on_failure(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SSE response iterator closes when adapting one chunk fails."""
    session_id = _seed_session(db)
    body_iterator_closed = asyncio.Event()

    async def stream_chunks() -> AsyncIterator[bytes]:
        try:
            yield cast(bytes, object())
        finally:
            body_iterator_closed.set()

    async def prompt_session(session_id: str, body: Any, request: Request) -> StreamingResponse:
        return StreamingResponse(stream_chunks(), media_type="text/event-stream")

    monkeypatch.setattr("yinshi.api.stream.prompt_session", prompt_session)
    journal_request = _request()
    journal = PromptJournal()
    run = await journal.start(
        request=journal_request,
        session_id=session_id,
        idempotency_key=str(uuid.uuid4()),
        body={"prompt": "hello"},
    )

    assert await _wait_for_terminal(journal, journal_request, session_id, run.id) == "failed"
    assert body_iterator_closed.is_set()
    await journal.close()


@pytest.mark.asyncio
async def test_prompt_journal_replays_ordered_events_from_sequence(
    db: sqlite3.Connection,
) -> None:
    """A repeated start returns one run and reconnect resumes at an exact cursor."""
    session_id = _seed_session(db)

    async def events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        assert request is journal_request
        assert selected_session_id == session_id
        assert body == {"prompt": "hello"}
        yield {"type": "status", "status": "started"}
        yield {"type": "assistant", "message": {"content": []}}
        yield {"type": "result", "usage": {}}

    journal_request = _request()
    journal = PromptJournal(executor=events)
    idempotency_key = str(uuid.uuid4())
    first = await journal.start(
        request=journal_request,
        session_id=session_id,
        idempotency_key=idempotency_key,
        body={"prompt": "hello"},
    )
    repeated = await journal.start(
        request=journal_request,
        session_id=session_id,
        idempotency_key=idempotency_key,
        body={"prompt": "ignored"},
    )

    assert repeated.id == first.id
    assert await _wait_for_terminal(journal, journal_request, session_id, first.id) == "completed"
    batch = await journal.events(
        request=journal_request,
        session_id=session_id,
        run_id=first.id,
        next_sequence=1,
    )
    assert [event["type"] for event in batch.events] == ["assistant", "result"]
    assert batch.next_sequence == 3
    await journal.close()


@pytest.mark.asyncio
async def test_prompt_journal_stops_reading_rows_at_page_byte_limit(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Page reads stop after inspecting the first event beyond the byte limit."""
    session_id = _seed_session(db)
    run_id = uuid.uuid4().hex
    with db:
        db.execute(
            """INSERT INTO prompt_runs (id, session_id, idempotency_key, status)
               VALUES (?, ?, ?, 'completed')""",
            (run_id, session_id, str(uuid.uuid4())),
        )
        for sequence in range(3):
            event_json = json.dumps(
                {"type": "assistant", "content": str(sequence) * 30_000},
                separators=(",", ":"),
                sort_keys=True,
            )
            db.execute(
                "INSERT INTO prompt_events (run_id, sequence, event_json) VALUES (?, ?, ?)",
                (run_id, sequence, event_json),
            )

    rows_read = 0
    original_get_db = get_db_for_request

    class InstrumentedCursor:
        def __init__(self, cursor: sqlite3.Cursor) -> None:
            self._cursor = cursor

        def __iter__(self) -> Iterator[sqlite3.Row]:
            return self

        def __next__(self) -> sqlite3.Row:
            nonlocal rows_read
            row = next(self._cursor)
            rows_read += 1
            return cast(sqlite3.Row, row)

        def fetchall(self) -> list[sqlite3.Row]:
            raise AssertionError("event page cursor must not use fetchall")

    class InstrumentedConnection:
        def __init__(self, database: sqlite3.Connection) -> None:
            self._database = database

        def execute(self, sql: str, parameters: Any = ()) -> Any:
            cursor = self._database.execute(sql, parameters)
            if "FROM prompt_events" in sql and "ORDER BY sequence" in sql:
                return InstrumentedCursor(cursor)
            return cursor

        def __getattr__(self, name: str) -> Any:
            return getattr(self._database, name)

    @contextmanager
    def instrumented_get_db(request: Request) -> Iterator[InstrumentedConnection]:
        with original_get_db(request) as database:
            yield InstrumentedConnection(database)

    monkeypatch.setattr(prompt_journal, "get_db_for_request", instrumented_get_db)
    journal = PromptJournal()
    batch = await journal.events(
        request=_request(),
        session_id=session_id,
        run_id=run_id,
        next_sequence=0,
    )

    assert len(batch.events) == 1
    assert batch.events[0]["content"] == "0" * 30_000
    assert batch.next_sequence == 1
    assert rows_read == 2
    await journal.close()


@pytest.mark.asyncio
async def test_prompt_journal_replays_one_mebibyte_event_intact(
    db: sqlite3.Connection,
) -> None:
    """A maximum-size event receives its own page without changing its shape."""
    session_id = _seed_session(db)
    empty_event = {"type": "assistant", "content": ""}
    empty_size = len(json.dumps(empty_event, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    large_event = {
        "type": "assistant",
        "content": "x" * (1_048_576 - empty_size),
    }
    assert (
        len(json.dumps(large_event, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        == 1_048_576
    )

    async def events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        assert selected_session_id == session_id
        yield {"type": "status"}
        yield large_event
        yield {"type": "result"}

    journal_request = _request()
    journal = PromptJournal(executor=events)
    run = await journal.start(
        request=journal_request,
        session_id=session_id,
        idempotency_key=str(uuid.uuid4()),
        body={"prompt": "large response"},
    )
    assert await _wait_for_terminal(journal, journal_request, session_id, run.id) == "completed"

    first_page = await journal.events(
        request=journal_request,
        session_id=session_id,
        run_id=run.id,
        next_sequence=0,
    )
    large_page = await journal.events(
        request=journal_request,
        session_id=session_id,
        run_id=run.id,
        next_sequence=first_page.next_sequence,
    )
    final_page = await journal.events(
        request=journal_request,
        session_id=session_id,
        run_id=run.id,
        next_sequence=large_page.next_sequence,
    )

    assert first_page.events == ({"type": "status"},)
    assert first_page.next_sequence == 1
    assert large_page.events == (large_event,)
    assert large_page.next_sequence == 2
    assert final_page.events == ({"type": "result"},)
    assert final_page.next_sequence == 3
    await journal.close()


@pytest.mark.asyncio
async def test_prompt_journal_rejects_event_over_one_mebibyte_before_insert(
    db: sqlite3.Connection,
) -> None:
    """An event above the durable limit fails without storing that event."""
    session_id = _seed_session(db)
    empty_event = {"type": "assistant", "content": ""}
    empty_size = len(json.dumps(empty_event, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    oversized_event = {
        "type": "assistant",
        "content": "x" * (1_048_577 - empty_size),
    }
    assert (
        len(json.dumps(oversized_event, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        == 1_048_577
    )

    async def events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        assert selected_session_id == session_id
        yield oversized_event

    journal_request = _request()
    journal = PromptJournal(executor=events)
    run = await journal.start(
        request=journal_request,
        session_id=session_id,
        idempotency_key=str(uuid.uuid4()),
        body={"prompt": "oversized response"},
    )
    assert await _wait_for_terminal(journal, journal_request, session_id, run.id) == "failed"

    batch = await journal.events(
        request=journal_request,
        session_id=session_id,
        run_id=run.id,
        next_sequence=0,
    )
    stored_events = db.execute(
        "SELECT event_json FROM prompt_events WHERE run_id = ? ORDER BY sequence",
        (run.id,),
    ).fetchall()

    assert batch.events == ({"error": "Prompt execution failed", "type": "error"},)
    assert [row["event_json"] for row in stored_events] == [
        '{"error":"Prompt execution failed","type":"error"}'
    ]
    await journal.close()


@pytest.mark.asyncio
async def test_prompt_journal_serializes_concurrent_idempotent_starts(
    db: sqlite3.Connection,
) -> None:
    """Concurrent retries return one run and register one executor task."""
    session_id = _seed_session(db)
    executor_calls = 0
    release_executor = asyncio.Event()

    async def blocked_events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        nonlocal executor_calls
        assert selected_session_id == session_id
        executor_calls += 1
        await release_executor.wait()
        yield {"type": "result"}

    journal = PromptJournal(executor=blocked_events)
    journal_request = _request()
    idempotency_key = str(uuid.uuid4())
    first, second = await asyncio.gather(
        journal.start(
            request=journal_request,
            session_id=session_id,
            idempotency_key=idempotency_key,
            body={"prompt": "hello"},
        ),
        journal.start(
            request=journal_request,
            session_id=session_id,
            idempotency_key=idempotency_key,
            body={"prompt": "hello"},
        ),
    )
    await asyncio.sleep(0)

    assert first.id == second.id
    assert executor_calls == 1
    release_executor.set()
    await journal.close()


@pytest.mark.asyncio
async def test_prompt_journal_start_cannot_outlive_close(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start paused before recovery cannot create work after close returns."""
    session_id = _seed_session(db)
    recovery_started = asyncio.Event()
    resume_recovery = asyncio.Event()
    executor_calls = 0

    async def events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        nonlocal executor_calls
        executor_calls += 1
        yield {"type": "result"}

    journal = PromptJournal(executor=events)
    original_recover_database = journal._recover_database

    async def paused_recover_database(request: Request) -> None:
        recovery_started.set()
        await resume_recovery.wait()
        await original_recover_database(request)

    monkeypatch.setattr(journal, "_recover_database", paused_recover_database)
    idempotency_key = str(uuid.uuid4())
    start_task = asyncio.create_task(
        journal.start(
            request=_request(),
            session_id=session_id,
            idempotency_key=idempotency_key,
            body={"prompt": "hello"},
        )
    )
    await asyncio.wait_for(recovery_started.wait(), timeout=1)

    await journal.close()
    resume_recovery.set()

    with pytest.raises(RuntimeError, match="prompt journal is closing"):
        await start_task
    assert executor_calls == 0
    assert journal._tasks == {}
    assert (
        db.execute(
            "SELECT COUNT(*) FROM prompt_runs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_prompt_journal_recovers_orphaned_run_on_first_tenant_request(
    db: sqlite3.Connection,
) -> None:
    """A new process marks durable active rows interrupted before serving events."""
    session_id = _seed_session(db)
    run_id = uuid.uuid4().hex
    with db:
        db.execute("UPDATE sessions SET status = 'running' WHERE id = ?", (session_id,))
        db.execute(
            """INSERT INTO prompt_runs (id, session_id, idempotency_key, status)
               VALUES (?, ?, ?, 'running')""",
            (run_id, session_id, str(uuid.uuid4())),
        )
        db.execute(
            "INSERT INTO prompt_events (run_id, sequence, event_json) VALUES (?, 0, ?)",
            (run_id, '{"type":"status"}'),
        )

    journal_request = _request()
    journal = PromptJournal()
    batch = await journal.events(
        request=journal_request,
        session_id=session_id,
        run_id=run_id,
        next_sequence=0,
    )

    assert batch.status == "interrupted"
    assert batch.events == (
        {"type": "status"},
        {"error": "Prompt run was interrupted", "type": "error"},
    )
    assert (
        db.execute("SELECT status FROM sessions WHERE id = ?", (session_id,)).fetchone()[0]
        == "idle"
    )
    await journal.close()


@pytest.mark.asyncio
async def test_prompt_journal_cancellation_closes_executor_iterator(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task cancellation explicitly closes the executor event iterator."""
    session_id = _seed_session(db)
    append_started = asyncio.Event()
    executor_closed = asyncio.Event()

    async def events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        assert selected_session_id == session_id
        try:
            yield {"type": "result"}
        finally:
            executor_closed.set()

    journal = PromptJournal(executor=events)
    original_append_event = journal._append_event

    async def blocked_append_event(
        request: Request,
        run_id: str,
        event: dict[str, Any],
        *,
        unless_terminal: bool = False,
    ) -> None:
        if not unless_terminal:
            append_started.set()
            await asyncio.Event().wait()
        await original_append_event(
            request,
            run_id,
            event,
            unless_terminal=unless_terminal,
        )

    monkeypatch.setattr(journal, "_append_event", blocked_append_event)
    run = await journal.start(
        request=_request(),
        session_id=session_id,
        idempotency_key=str(uuid.uuid4()),
        body={"prompt": "hello"},
    )
    await asyncio.wait_for(append_started.wait(), timeout=1)

    await journal.close()

    assert executor_closed.is_set()
    assert run.id not in journal._tasks


@pytest.mark.asyncio
async def test_prompt_journal_close_retrieves_all_failed_task_outcomes() -> None:
    """Shutdown absorbs failures raised while cancelled prompt tasks finalize."""
    journal = PromptJournal()
    finalizers_started = 0
    all_finalizers_started = asyncio.Event()

    async def fail_during_finalization(message: str) -> None:
        nonlocal finalizers_started
        try:
            await asyncio.Event().wait()
        finally:
            finalizers_started += 1
            if finalizers_started == 2:
                all_finalizers_started.set()
            await all_finalizers_started.wait()
            raise RuntimeError(message)

    first_task = asyncio.create_task(fail_during_finalization("first finalizer failed"))
    second_task = asyncio.create_task(fail_during_finalization("second finalizer failed"))
    journal._tasks = {"first": first_task, "second": second_task}
    await asyncio.sleep(0)

    await journal.close()

    assert first_task.done()
    assert second_task.done()
    assert journal._tasks == {}


@pytest.mark.asyncio
async def test_executor_iterator_close_failure_keeps_terminal_cleanup(
    db: sqlite3.Connection,
) -> None:
    """Iterator close errors do not block terminal state or task cleanup."""
    session_id = _seed_session(db)
    executor_started = asyncio.Event()
    journal_request = _request()

    class FailingCloseEvents:
        def __aiter__(self) -> FailingCloseEvents:
            return self

        async def __anext__(self) -> dict[str, Any]:
            executor_started.set()
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            raise RuntimeError("executor iterator close failed")

    def blocked_events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ) -> FailingCloseEvents:
        assert selected_session_id == session_id
        return FailingCloseEvents()

    journal = PromptJournal(executor=blocked_events)
    run = await journal.start(
        request=journal_request,
        session_id=session_id,
        idempotency_key=str(uuid.uuid4()),
        body={"prompt": "hello"},
    )
    await asyncio.wait_for(executor_started.wait(), timeout=1)

    await journal.close()
    batch = await journal.events(
        request=journal_request,
        session_id=session_id,
        run_id=run.id,
        next_sequence=0,
    )

    assert batch.status == "interrupted"
    assert batch.events == ({"error": "Prompt run was interrupted", "type": "error"},)
    assert journal._tasks == {}


@pytest.mark.asyncio
async def test_prompt_journal_close_marks_active_run_interrupted(
    db: sqlite3.Connection,
) -> None:
    """Graceful process shutdown records interruption rather than user cancellation."""
    session_id = _seed_session(db)
    executor_started = asyncio.Event()

    async def blocked_events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        assert selected_session_id == session_id
        executor_started.set()
        await asyncio.Event().wait()
        yield {"type": "result"}

    journal_request = _request()
    journal = PromptJournal(executor=blocked_events)
    run = await journal.start(
        request=journal_request,
        session_id=session_id,
        idempotency_key=str(uuid.uuid4()),
        body={"prompt": "hello"},
    )
    await asyncio.wait_for(executor_started.wait(), timeout=1)

    await journal.close()
    batch = await journal.events(
        request=journal_request,
        session_id=session_id,
        run_id=run.id,
        next_sequence=0,
    )

    assert batch.status == "interrupted"
    assert batch.events == ({"error": "Prompt run was interrupted", "type": "error"},)
    with pytest.raises(RuntimeError, match="closing"):
        await journal.start(
            request=journal_request,
            session_id=session_id,
            idempotency_key=str(uuid.uuid4()),
            body={"prompt": "again"},
        )


@pytest.mark.asyncio
async def test_prompt_journal_immediate_cancellation_wins_start_race(
    db: sqlite3.Connection,
) -> None:
    """Cancellation before executor startup never turns into a failed run."""
    session_id = _seed_session(db)

    async def never_started_events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        raise AssertionError("cancelled executor must not start")
        yield {"type": "result"}

    journal_request = _request()
    journal = PromptJournal(executor=never_started_events)
    run = await journal.start(
        request=journal_request,
        session_id=session_id,
        idempotency_key=str(uuid.uuid4()),
        body={"prompt": "stop now"},
    )
    cancelled = await journal.cancel(
        request=journal_request,
        session_id=session_id,
        run_id=run.id,
    )
    batch = await journal.events(
        request=journal_request,
        session_id=session_id,
        run_id=run.id,
        next_sequence=0,
    )

    assert cancelled.status == "cancelled"
    assert batch.status == "cancelled"
    assert batch.events == ({"reason": "user_stop", "type": "cancelled"},)
    await journal.close()


@pytest.mark.asyncio
async def test_terminal_status_failure_reconciles_before_same_process_reuse(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed terminal write must reconcile without false orphan recovery."""
    session_id = _seed_session(db)
    first_executor_finished = asyncio.Event()
    second_executor_started = asyncio.Event()
    release_second_executor = asyncio.Event()
    executor_calls = 0
    journal_request = _request()

    async def events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        nonlocal executor_calls
        assert selected_session_id == session_id
        executor_calls += 1
        if executor_calls == 1:
            yield {"type": "result"}
            first_executor_finished.set()
            return
        second_executor_started.set()
        await release_second_executor.wait()
        yield {"type": "result"}

    journal = PromptJournal(executor=events)
    original_set_terminal_status = journal._set_terminal_status
    terminal_attempts = 0

    def failing_set_terminal_status(request: Request, run_id: str, status: str) -> None:
        nonlocal terminal_attempts
        terminal_attempts += 1
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(journal, "_set_terminal_status", failing_set_terminal_status)
    first = await journal.start(
        request=journal_request,
        session_id=session_id,
        idempotency_key=str(uuid.uuid4()),
        body={"prompt": "first"},
    )
    first_task = journal._tasks[first.id]
    await asyncio.wait_for(first_executor_finished.wait(), timeout=1)

    with pytest.raises(sqlite3.DatabaseError, match="database disk image is malformed"):
        await first_task
    assert terminal_attempts == 1
    assert first.id not in journal._tasks
    assert (
        db.execute("SELECT status FROM prompt_runs WHERE id = ?", (first.id,)).fetchone()[0]
        == "running"
    )

    monkeypatch.setattr(journal, "_set_terminal_status", original_set_terminal_status)
    batch = await journal.events(
        request=journal_request,
        session_id=session_id,
        run_id=first.id,
        next_sequence=0,
    )
    assert batch.status == "completed"
    assert batch.events == ({"type": "result"},)
    second = await journal.start(
        request=journal_request,
        session_id=session_id,
        idempotency_key=str(uuid.uuid4()),
        body={"prompt": "second"},
    )
    await asyncio.wait_for(second_executor_started.wait(), timeout=1)

    first_row = db.execute("SELECT status FROM prompt_runs WHERE id = ?", (first.id,)).fetchone()
    first_events = db.execute(
        "SELECT event_json FROM prompt_events WHERE run_id = ? ORDER BY sequence",
        (first.id,),
    ).fetchall()
    assert first_row["status"] == "completed"
    assert [row["event_json"] for row in first_events] == ['{"type":"result"}']
    assert second.id != first.id
    assert executor_calls == 2

    release_second_executor.set()
    assert await _wait_for_terminal(journal, journal_request, session_id, second.id) == "completed"
    await journal.close()


@pytest.mark.asyncio
async def test_terminal_status_retries_sqlite_busy_without_interrupting_run(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded SQLite busy window must not corrupt completed prompt status."""
    session_id = _seed_session(db)

    async def events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        assert selected_session_id == session_id
        yield {"type": "result"}

    journal = PromptJournal(executor=events)
    original_set_terminal_status = journal._set_terminal_status
    attempts = 0

    def busy_then_complete(request: Request, run_id: str, status: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise sqlite3.OperationalError("database is locked")
        original_set_terminal_status(request, run_id, status)

    monkeypatch.setattr(journal, "_set_terminal_status", busy_then_complete)
    run = await journal.start(
        request=_request(),
        session_id=session_id,
        idempotency_key=str(uuid.uuid4()),
        body={"prompt": "complete after busy"},
    )
    task = journal._tasks[run.id]
    await asyncio.wait_for(task, timeout=1)
    batch = await journal.events(
        request=_request(),
        session_id=session_id,
        run_id=run.id,
        next_sequence=0,
    )

    assert attempts == 3
    assert batch.status == "completed"
    assert batch.events == ({"type": "result"},)
    await journal.close()


@pytest.mark.asyncio
async def test_prompt_journal_cancellation_is_idempotent_before_sidecar_registration(
    db: sqlite3.Connection,
) -> None:
    """Immediate repeated cancellation leaves one durable cancelled event."""
    session_id = _seed_session(db)
    executor_started = asyncio.Event()

    async def blocked_events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        executor_started.set()
        await asyncio.Event().wait()
        yield {"type": "result"}

    journal_request = _request()
    journal = PromptJournal(executor=blocked_events)
    run = await journal.start(
        request=journal_request,
        session_id=session_id,
        idempotency_key=str(uuid.uuid4()),
        body={"prompt": "stop"},
    )
    await executor_started.wait()

    first_cancel = await journal.cancel(
        request=journal_request,
        session_id=session_id,
        run_id=run.id,
    )
    repeated_cancel = await journal.cancel(
        request=journal_request,
        session_id=session_id,
        run_id=run.id,
    )
    batch = await journal.events(
        request=journal_request,
        session_id=session_id,
        run_id=run.id,
        next_sequence=0,
    )

    assert first_cancel.status == "cancelled"
    assert repeated_cancel.status == "cancelled"
    assert batch.status == "cancelled"
    assert batch.events == ({"reason": "user_stop", "type": "cancelled"},)
    await journal.close()
