"""Synchronous domain admission joins the durable prompt reservation."""

import uuid

import pytest

from tests.test_prompt_journal import _request, _seed_session, _wait_for_terminal
from yinshi.services.prompt_journal import PromptJournal


async def test_admission_rolls_back_rejection_and_does_not_repeat_on_replay(db):
    session_id = _seed_session(db)
    original_title = db.execute(
        "SELECT title FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()[0]
    calls = []

    async def execute(request, selected_session_id, body):
        calls.append("executor")
        yield {"type": "result"}

    def deny(database):
        assert database.in_transaction
        calls.append("deny")
        database.execute("UPDATE sessions SET title = 'uncommitted' WHERE id = ?", (session_id,))
        raise ValueError("admission rejected")

    def allow(database):
        assert database.in_transaction
        calls.append("allow")

    journal = PromptJournal(executor=execute)
    request = _request()
    key = str(uuid.uuid4())
    try:
        with pytest.raises(ValueError, match="admission rejected"):
            await journal.start(
                request=request,
                session_id=session_id,
                idempotency_key=key,
                body={"prompt": "inspect"},
                admission_guard=deny,
            )
        assert db.execute("SELECT COUNT(*) FROM prompt_runs").fetchone()[0] == 0
        assert (
            db.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()[0]
            == original_title
        )
        accepted = await journal.start(
            request=request,
            session_id=session_id,
            idempotency_key=key,
            body={"prompt": "inspect"},
            admission_guard=allow,
        )
        assert await _wait_for_terminal(journal, request, session_id, accepted.id) == "completed"
        replay = await journal.start(
            request=request,
            session_id=session_id,
            idempotency_key=key,
            body={"prompt": "inspect"},
            admission_guard=deny,
        )
        assert replay.id == accepted.id
        assert calls == ["deny", "allow", "executor"]
    finally:
        await journal.close()
