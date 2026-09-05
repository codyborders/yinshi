"""Fail-closed frame-handling tests for the sidecar query loop.

Every orchestration_request frame that arrives during a query must either be
answered through the bounded handler path or rejected with a safe error. No
internal frame may ever reach the model-facing event stream.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from yinshi.services.orchestration_bridge import (
    generate_orchestration_capability,
)
from yinshi.services.sidecar import SidecarClient


def make_connected_client() -> SidecarClient:
    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()
    return client


def written_messages(client: SidecarClient) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for call in client._writer.write.call_args_list:
        payload = call.args[0]
        messages.extend(json.loads(line) for line in payload.decode().splitlines() if line.strip())
    return messages


def orchestration_responses(client: SidecarClient) -> list[dict[str, Any]]:
    return [
        message
        for message in written_messages(client)
        if message.get("type") == "orchestration_response"
    ]


def request_frame(
    capability: str,
    *,
    session_id: str = "sess-1",
    request_id: str | None = None,
    operation: str = "ping_thread_bridge",
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "orchestration_request",
        "id": session_id,
        "request_id": request_id or uuid.uuid4().hex,
        "capability": capability,
        "operation": operation,
        "arguments": arguments if arguments is not None else {"message": "ping"},
    }


RESULT_EVENT: dict[str, Any] = {
    "id": "sess-1",
    "type": "message",
    "data": {"type": "result"},
}


async def run_query_with_frames(
    client: SidecarClient,
    capability: Any,
    frames: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pending = iter([*frames, RESULT_EVENT])

    async def read_line() -> dict[str, Any]:
        # Real socket reads always suspend, giving handler tasks a run slot.
        await asyncio.sleep(0)
        return next(pending)

    client._read_line = read_line
    return [
        event
        async for event in client.query("sess-1", "hello", orchestration_capability=capability)
    ]


async def test_valid_request_is_answered_and_never_yielded() -> None:
    client = make_connected_client()
    capability = generate_orchestration_capability("sess-1", run_id="run-1")

    events = await run_query_with_frames(client, capability, [request_frame(capability.token)])

    assert [event.get("type") for event in events] == ["message"]
    responses = orchestration_responses(client)
    assert len(responses) == 1
    assert responses[0]["ok"] is True
    assert responses[0]["result"]["status"] == "ok"
    assert responses[0]["result"]["echo"] == "ping"
    assert capability.token not in json.dumps(events)


async def test_unknown_operation_fails_closed_without_yield() -> None:
    client = make_connected_client()
    capability = generate_orchestration_capability("sess-1", run_id="run-1")

    events = await run_query_with_frames(
        client,
        capability,
        [request_frame(capability.token, operation="spawn_thread")],
    )

    assert [event.get("type") for event in events] == ["message"]
    responses = orchestration_responses(client)
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "unknown_operation"


async def test_malformed_frame_is_consumed_without_yield() -> None:
    client = make_connected_client()
    capability = generate_orchestration_capability("sess-1", run_id="run-1")
    malformed = request_frame(capability.token)
    malformed["unexpected"] = True

    events = await run_query_with_frames(client, capability, [malformed])

    assert [event.get("type") for event in events] == ["message"]
    responses = orchestration_responses(client)
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "invalid_request"


async def test_oversized_frame_is_rejected_without_yield() -> None:
    client = make_connected_client()
    capability = generate_orchestration_capability("sess-1", run_id="run-1")
    pending_frames = iter([request_frame(capability.token), RESULT_EVENT])

    async def sized_read_line() -> dict[str, Any]:
        # Real socket reads record the wire size of every frame.
        await asyncio.sleep(0)
        frame = next(pending_frames)
        if frame.get("type") == "orchestration_request":
            client._last_frame_bytes = 64 * 1024 + 1
        return frame

    client._read_line = sized_read_line
    events = [
        event
        async for event in client.query("sess-1", "hello", orchestration_capability=capability)
    ]

    assert [event.get("type") for event in events] == ["message"]
    responses = orchestration_responses(client)
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "request_too_large"


async def test_oversized_frame_then_small_frame_uses_per_frame_size() -> None:
    """Frame size is captured at dispatch, so one oversized frame cannot poison
    the size check of the next, smaller frame."""
    client = make_connected_client()
    capability = generate_orchestration_capability("sess-1", run_id="run-1")
    oversized = request_frame(capability.token)
    small = request_frame(capability.token)
    pending_frames = iter([oversized, small, RESULT_EVENT])

    async def sized_read_line() -> dict[str, Any]:
        await asyncio.sleep(0)
        frame = next(pending_frames)
        if frame is oversized:
            client._last_frame_bytes = 64 * 1024 + 1
        else:
            client._last_frame_bytes = len(json.dumps(frame).encode()) + 1
        return frame

    client._read_line = sized_read_line
    events = [
        event
        async for event in client.query("sess-1", "hello", orchestration_capability=capability)
    ]

    assert [event.get("type") for event in events] == ["message"]
    responses = orchestration_responses(client)
    assert [response.get("ok") for response in responses] == [False, True]
    assert responses[0]["error"]["code"] == "request_too_large"
    assert responses[1]["result"]["echo"] == "ping"


async def test_internal_frames_are_consumed_before_event_yield() -> None:
    """Every internal orchestration frame type is consumed, malformed included,
    and only ordinary Pi events reach the model-facing stream."""
    client = make_connected_client()
    capability = generate_orchestration_capability("sess-1", run_id="run-1")
    stray_response = {
        "type": "orchestration_response",
        "id": "sess-1",
        "request_id": uuid.uuid4().hex,
        "ok": True,
        "result": {"status": "ok"},
    }
    typeless_request = request_frame(capability.token)
    del typeless_request["type"]

    events = await run_query_with_frames(
        client,
        capability,
        [stray_response, typeless_request],
    )

    assert [event.get("type") for event in events] == ["message"]
    responses = orchestration_responses(client)
    assert len(responses) == 1
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "invalid_request"
