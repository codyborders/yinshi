"""Safe error-mapping tests for the sidecar orchestration loop.

Handler failures must become bounded response frames. Raw exception text
never reaches the sidecar, and the query stream itself must stay healthy.
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


def make_connected_client(
    handlers: dict[str, Any] | None = None,
) -> SidecarClient:
    client = SidecarClient(orchestration_handlers=handlers)
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()
    return client


def orchestration_responses(client: SidecarClient) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for call in client._writer.write.call_args_list:
        payload = call.args[0]
        messages.extend(json.loads(line) for line in payload.decode().splitlines() if line.strip())
    return [message for message in messages if message.get("type") == "orchestration_response"]


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
        await asyncio.sleep(0)
        return next(pending)

    client._read_line = read_line
    return [
        event
        async for event in client.query("sess-1", "hello", orchestration_capability=capability)
    ]


def request_frame(capability: str, operation: str = "ping_thread_bridge") -> dict[str, Any]:
    return {
        "type": "orchestration_request",
        "id": "sess-1",
        "request_id": uuid.uuid4().hex,
        "capability": capability,
        "operation": operation,
        "arguments": {"message": "ping"},
    }


async def test_handler_exception_becomes_bounded_error() -> None:
    async def exploding_handler(arguments: dict[str, Any], *, session_id: str) -> dict[str, Any]:
        raise RuntimeError("secret database path /private/hidden.db exploded")

    client = make_connected_client({"ping_thread_bridge": exploding_handler})
    capability = generate_orchestration_capability("sess-1", run_id="run-1")

    events = await run_query_with_frames(client, capability, [request_frame(capability.token)])
    await asyncio.sleep(0.05)

    assert [event.get("type") for event in events] == ["message"]
    responses = orchestration_responses(client)
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "handler_failed"
    serialized = json.dumps(responses)
    assert "secret" not in serialized
    assert "/private/hidden.db" not in serialized


async def test_stalled_handler_times_out_with_bounded_error() -> None:
    stalled = asyncio.Event()

    async def stalled_handler(arguments: dict[str, Any], *, session_id: str) -> dict[str, Any]:
        await stalled.wait()
        return {"status": "ok"}

    client = SidecarClient(
        orchestration_handlers={"ping_thread_bridge": stalled_handler},
        orchestration_handler_timeout=0.05,
    )
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()
    capability = generate_orchestration_capability("sess-1", run_id="run-1")

    frame = request_frame(capability.token)
    pending_frames = iter([frame])

    async def read_line() -> dict[str, Any]:
        await asyncio.sleep(0)
        if (next_frame := next(pending_frames, None)) is not None:
            return next_frame
        # Keep the stream open until the handler deadline has fired and the
        # timeout response has been written.
        while not orchestration_responses(client):
            await asyncio.sleep(0.01)
        return RESULT_EVENT

    client._read_line = read_line
    events = [
        event
        async for event in client.query("sess-1", "hello", orchestration_capability=capability)
    ]
    stalled.set()

    assert [event.get("type") for event in events] == ["message"]
    responses = orchestration_responses(client)
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "handler_timeout"


async def test_oversized_result_is_replaced_with_bounded_error() -> None:
    async def huge_handler(arguments: dict[str, Any], *, session_id: str) -> dict[str, Any]:
        return {"blob": "x" * (300 * 1024)}

    client = make_connected_client({"ping_thread_bridge": huge_handler})
    capability = generate_orchestration_capability("sess-1", run_id="run-1")

    events = await run_query_with_frames(client, capability, [request_frame(capability.token)])
    await asyncio.sleep(0.05)

    assert [event.get("type") for event in events] == ["message"]
    responses = orchestration_responses(client)
    assert len(json.dumps(responses[0])) < 64 * 1024
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "response_too_large"
