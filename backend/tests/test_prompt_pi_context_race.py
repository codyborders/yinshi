"""Regression tests for concurrent first-prompt Pi context resolution."""

import asyncio
import sqlite3
import threading
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.factories import create_full_stack, make_mock_sidecar
from yinshi.api import stream
from yinshi.api.stream import ExecutionContext

_WAIT_BOUND_S = 5.0
_HOLD_WINDOW_S = 0.3


@pytest.fixture
def session_id(client: TestClient, git_repo: str) -> str:
    """Create a repo, workspace, and session stack for prompt race tests."""
    stack = create_full_stack(client, git_repo, name="pi-context-race")
    return stack["session"]["id"]


@pytest.mark.asyncio
async def test_concurrent_first_prompts_use_one_context_transaction(
    client: TestClient,
    session_id: str,
) -> None:
    """A competing first prompt cannot enter context checks during reservation."""
    from yinshi.db import get_db

    with get_db() as db:
        db.execute(
            "UPDATE sessions SET pi_context_version = 0 WHERE id = ?",
            (session_id,),
        )
        db.commit()

    release_stream = asyncio.Event()
    first_count_entered = threading.Event()
    allow_first_count = threading.Event()
    count_entries_while_held: list[bool] = []
    original_count = stream._message_count_for_session

    async def slow_query(*args: object, **kwargs: object) -> AsyncIterator[dict[str, Any]]:
        del args, kwargs
        await asyncio.wait_for(release_stream.wait(), timeout=_WAIT_BOUND_S)
        yield {"type": "message", "data": {"type": "result", "usage": {}}}

    def held_count(database: sqlite3.Connection, target_session_id: str) -> int:
        entered_while_held = not allow_first_count.is_set()
        count_entries_while_held.append(entered_while_held)
        if len(count_entries_while_held) == 1:
            first_count_entered.set()
            if not allow_first_count.wait(timeout=_WAIT_BOUND_S):
                raise TimeoutError("first prompt count hold timed out")
        return original_count(database, target_session_id)

    mock_sidecar = make_mock_sidecar(slow_query)
    with (
        patch(
            "yinshi.api.stream.create_sidecar_connection",
            return_value=mock_sidecar,
        ),
        patch(
            "yinshi.api.stream._resolve_execution_context",
            new=AsyncMock(
                return_value=ExecutionContext(
                    sidecar_socket=None,
                    effective_cwd="/tmp",
                    key_source="platform",
                    provider="test-provider",
                    provider_auth=None,
                    provider_config=None,
                    model_ref="test-model",
                ),
            ),
        ),
        patch("yinshi.api.stream._message_count_for_session", new=held_count),
    ):
        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as async_client:
            first_task = asyncio.create_task(
                async_client.post(
                    f"/api/sessions/{session_id}/prompt",
                    json={"prompt": "first prompt"},
                )
            )
            await asyncio.wait_for(
                asyncio.to_thread(first_count_entered.wait, _WAIT_BOUND_S),
                timeout=_WAIT_BOUND_S,
            )
            second_task = asyncio.create_task(
                async_client.post(
                    f"/api/sessions/{session_id}/prompt",
                    json={"prompt": "second prompt"},
                )
            )
            await asyncio.sleep(_HOLD_WINDOW_S)
            entries_before_release = tuple(count_entries_while_held)
            allow_first_count.set()
            second_response = await asyncio.wait_for(second_task, timeout=_WAIT_BOUND_S)
            release_stream.set()
            first_response = await asyncio.wait_for(first_task, timeout=_WAIT_BOUND_S)

    assert entries_before_release == (True,)
    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 409, second_response.text
    assert second_response.json()["detail"] == "Session already has an active stream"
    with get_db() as db:
        row = db.execute(
            "SELECT pi_context_version FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    assert row is not None
    assert row["pi_context_version"] == 1
