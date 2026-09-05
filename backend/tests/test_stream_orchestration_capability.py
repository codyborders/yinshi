"""Exercise the harmless bridge through the public prompt route and real client."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from tests.factories import create_full_stack
from yinshi.api.stream import ExecutionContext
from yinshi.services.sidecar import SidecarClient


def test_prompt_runs_harmless_bridge_without_persisting_internal_frames(auth_client, git_repo):
    stack = create_full_stack(auth_client, git_repo, name="bridge-prompt")
    session_id = stack["session"]["id"]
    query_frames = []
    bridge_responses = []

    async def create_connection(*args, **kwargs):
        client = SidecarClient()
        reader = asyncio.StreamReader()
        writer = MagicMock()
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()
        client._connected = True
        client._reader = reader
        client._writer = writer

        def feed(frame):
            reader.feed_data(json.dumps(frame).encode() + b"\n")

        def receive(encoded):
            frame = json.loads(encoded)
            if frame["type"] == "resolve":
                feed(
                    {
                        "type": "resolved",
                        "id": frame["id"],
                        "provider": "minimax",
                        "model": "MiniMax-M2.7",
                    }
                )
            elif frame["type"] == "warmup":
                feed({"type": "warmup_status", "id": session_id, "success": True})
            elif frame["type"] == "query":
                query_frames.append(frame)
                orchestration = frame["options"].get("orchestration")
                if orchestration is None:
                    feed(
                        {
                            "type": "message",
                            "id": session_id,
                            "data": {"type": "result", "usage": {}},
                        }
                    )
                    return
                feed(
                    {
                        "type": "orchestration_request",
                        "id": session_id,
                        "request_id": "bridge-1",
                        "capability": orchestration["capability"],
                        "operation": "ping_thread_bridge",
                        "arguments": {"message": "hello"},
                    }
                )
            elif frame["type"] == "orchestration_response":
                bridge_responses.append(frame)
                feed({"type": "message", "id": session_id, "data": {"type": "result", "usage": {}}})
            else:
                raise AssertionError(f"Unexpected test-peer frame: {frame['type']}")

        writer.write.side_effect = receive
        return client

    context = ExecutionContext(
        sidecar_socket=None,
        effective_cwd=stack["workspace"]["path"],
        key_source="platform",
        provider="test-provider",
        provider_auth=None,
        provider_config=None,
    )
    with (
        patch("yinshi.api.stream.create_sidecar_connection", side_effect=create_connection),
        patch("yinshi.api.stream._resolve_execution_context", new=AsyncMock(return_value=context)),
    ):
        response = auth_client.post(f"/api/sessions/{session_id}/prompt", json={"prompt": "hello"})

    assert response.status_code == 200
    assert bridge_responses and bridge_responses[0]["ok"] is True
    assert bridge_responses[0]["result"]["echo"] == "hello"
    token = query_frames[0]["options"]["orchestration"]["capability"]
    assert token not in response.text
    assert "orchestration_request" not in response.text
    assert "orchestration_response" not in response.text
    messages = auth_client.get(f"/api/sessions/{session_id}/messages")
    assert messages.status_code == 200
    assert token not in messages.text
    assert "orchestration_request" not in messages.text
    assert "orchestration_response" not in messages.text
