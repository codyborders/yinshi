"""Behavior tests for privacy-safe prompt logging."""

import logging
import sqlite3
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from tests.factories import create_full_stack, make_mock_sidecar


def test_prompt_lifecycle_logs_exclude_private_values(
    auth_client: TestClient,
    git_repo: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Prompt logs retain safe metrics without request or sidecar content."""
    from yinshi.api.stream import ExecutionContext

    session_sentinel = "SESSION_PRIVATE_STREAM_SENTINEL"
    turn_sentinel = "TURN_PRIVATE_STREAM_SENTINEL"
    prompt_sentinel = "PROMPT_PRIVATE_STREAM_SENTINEL"
    event_sentinel = "EVENT_PRIVATE_STREAM_SENTINEL"
    assistant_sentinel = "ASSISTANT_PRIVATE_STREAM_SENTINEL"
    user_sentinel = "USER_PRIVATE_STREAM_SENTINEL"
    tenant_sentinel = "TENANT_PRIVATE_STREAM_SENTINEL"
    auth_sentinel = "AUTH_PRIVATE_STREAM_SENTINEL"
    exception_sentinel = "EXTERNAL_EXCEPTION_STREAM_SENTINEL"
    path_sentinel = f"/private/{tenant_sentinel}/workspace"
    stack = create_full_stack(auth_client, git_repo, name="private-stream-logs")
    original_session_id = stack["session"]["id"]
    tenant = getattr(auth_client, "yinshi_tenant")
    with sqlite3.connect(tenant.db_path) as database:
        database.execute(
            "UPDATE sessions SET id = ? WHERE id = ?",
            (session_sentinel, original_session_id),
        )
        database.commit()

    async def private_query(
        *_args: object,
        **_kwargs: object,
    ) -> AsyncIterator[dict[str, object]]:
        from yinshi.exceptions import SidecarError

        yield {
            "type": "message",
            "private_event": event_sentinel,
            "data": {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": assistant_sentinel}],
                },
            },
        }
        raise SidecarError(exception_sentinel)

    sidecar = make_mock_sidecar(private_query)
    context = ExecutionContext(
        sidecar_socket=None,
        effective_cwd=path_sentinel,
        key_source="api_key",
        provider="safe-provider",
        provider_auth={"secret": auth_sentinel, "account": user_sentinel},
        provider_config={"tenant": tenant_sentinel},
        model_ref="safe-provider/safe-model",
    )
    turn_uuid = Mock(hex=turn_sentinel)
    caplog.set_level(logging.DEBUG, logger="yinshi.api.stream")
    with (
        patch("yinshi.api.stream.uuid.uuid4", return_value=turn_uuid),
        patch(
            "yinshi.api.stream._resolve_execution_context",
            new=AsyncMock(return_value=context),
        ),
        patch("yinshi.api.stream.create_sidecar_connection", return_value=sidecar),
    ):
        response = auth_client.post(
            f"/api/sessions/{session_sentinel}/prompt",
            json={"prompt": prompt_sentinel},
        )

    assert response.status_code == 200
    private_values = (
        tenant.user_id,
        session_sentinel,
        turn_sentinel,
        prompt_sentinel,
        event_sentinel,
        assistant_sentinel,
        user_sentinel,
        tenant_sentinel,
        auth_sentinel,
        exception_sentinel,
        path_sentinel,
    )
    for record in caplog.records:
        rendered_record = f"{record.getMessage()} {record.args!r}"
        assert all(value not in rendered_record for value in private_values)
    assert (
        f"Prompt received: prompt_len={len(prompt_sentinel)} "
        "model=minimax/MiniMax-M2.7 provider=safe-provider" in caplog.text
    )
    assert "Prompt stream started" in caplog.text
    assert "Sidecar event received" in caplog.text
    assert "Sidecar message event received" in caplog.text
    assert "Sidecar prompt execution failed" in caplog.text
    assert "Turn complete: chunks=1 content_len=33 turn_status=failed" in caplog.text
