"""Verify durable, ordered prompt events and idempotent cancellation.

Tests use the real SQLite journal with injected event generators so reconnect and
cancellation behavior are checked without starting a sidecar process.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request
from fastapi.responses import StreamingResponse

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

    async def stream_chunks():
        yield 'data: {"status":"started","type":"status"}\n'
        yield "\n"
        yield 'data: {"type":"result","usage":{}}\n\n'

    async def prompt_session(session_id: str, body: Any, request: Request):
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
async def test_prompt_journal_replays_ordered_events_from_sequence(
    db: sqlite3.Connection,
) -> None:
    """A repeated start returns one run and reconnect resumes at an exact cursor."""
    session_id = _seed_session(db)

    async def events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ):
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
    ):
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
    ):
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
    ):
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
    ):
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
