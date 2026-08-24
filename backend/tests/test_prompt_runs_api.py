"""Verify reconnectable prompt-run HTTP contracts with a durable event journal.

Tests use authenticated routes and an injected event source, then reconnect from
an event cursor and retry cancellation/start requests.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from tests.factories import create_full_stack
from yinshi.services.prompt_journal import PromptJournal, PromptRun


def test_active_prompt_run_route_returns_run_or_null(
    auth_client: TestClient,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active-run discovery preserves session ownership and nullable results."""
    stack = create_full_stack(auth_client, git_repo, name="active-run")
    session_id = stack["session"]["id"]
    run_id = uuid.uuid4().hex

    class ActiveJournal(PromptJournal):
        active_run: PromptRun | None = PromptRun(
            id=run_id,
            session_id=session_id,
            status="running",
        )

        async def active(self, **_kwargs):
            return self.active_run

    from yinshi.main import app

    journal = ActiveJournal()
    monkeypatch.setattr(app.state, "prompt_journal", journal)

    active = auth_client.get(f"/api/sessions/{session_id}/runs/active")
    journal.active_run = None
    inactive = auth_client.get(f"/api/sessions/{session_id}/runs/active")
    missing = auth_client.get(f"/api/sessions/{uuid.uuid4().hex}/runs/active")

    assert active.status_code == 200
    assert active.json() == {
        "id": run_id,
        "session_id": session_id,
        "status": "running",
    }
    assert inactive.status_code == 200
    assert inactive.json() is None
    assert missing.status_code == 404


def test_prompt_run_start_and_sequence_reconnect(
    auth_client: TestClient,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt start is idempotent and event pages resume at exact sequence."""
    stack = create_full_stack(auth_client, git_repo, name="journal")
    session_id = stack["session"]["id"]

    async def prompt_events(
        request: Request,
        selected_session_id: str,
        body: Any,
    ):
        assert selected_session_id == session_id
        assert body.prompt == "journal prompt"
        yield {"type": "status", "status": "started"}
        yield {"type": "assistant", "message": {"content": []}}
        yield {"type": "result", "usage": {}}

    from yinshi.main import app

    journal = PromptJournal(executor=prompt_events)
    monkeypatch.setattr(app.state, "prompt_journal", journal)
    idempotency_key = str(uuid.uuid4())
    payload = {
        "prompt": "journal prompt",
        "model": None,
        "thinking": None,
        "idempotency_key": idempotency_key,
    }
    started = auth_client.post(f"/api/sessions/{session_id}/runs", json=payload)
    repeated = auth_client.post(f"/api/sessions/{session_id}/runs", json=payload)

    assert started.status_code == 202
    assert repeated.status_code == 202
    assert repeated.json()["id"] == started.json()["id"]
    run_id = started.json()["id"]

    batch_body: dict[str, Any] = {}
    for _ in range(100):
        batch = auth_client.get(f"/api/sessions/{session_id}/runs/{run_id}/events/1")
        assert batch.status_code == 200
        batch_body = batch.json()
        if batch_body["status"] == "completed":
            break
        asyncio.run(asyncio.sleep(0))

    assert batch_body["status"] == "completed"
    assert [event["type"] for event in batch_body["events"]] == [
        "assistant",
        "result",
    ]
    assert batch_body["next_sequence"] == 3


def test_prompt_event_storage_outage_returns_retryable_json(
    auth_client: TestClient,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausted tenant storage returns bounded JSON instead of closing transport."""
    from yinshi.main import app
    from yinshi.tenant import TenantDatabaseTemporarilyUnavailable

    stack = create_full_stack(auth_client, git_repo, name="storage-outage")
    session_id = stack["session"]["id"]

    class UnavailableJournal(PromptJournal):
        async def events(self, **_kwargs):
            raise TenantDatabaseTemporarilyUnavailable(
                "Tenant database storage is temporarily unavailable"
            )

    monkeypatch.setattr(app.state, "prompt_journal", UnavailableJournal())
    response = auth_client.get(f"/api/sessions/{session_id}/runs/{uuid.uuid4().hex}/events/0")

    assert response.status_code == 503
    assert response.json() == {"detail": "Tenant storage is temporarily unavailable"}
    assert response.headers["retry-after"] == "1"
