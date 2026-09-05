"""Capability-gate tests for the sidecar query loop.

A request whose capability does not match the active query binding must be
rejected with a safe bounded error and must never reach the event stream.
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
        # Real socket reads always suspend, giving handler tasks a run slot.
        await asyncio.sleep(0)
        return next(pending)

    client._read_line = read_line
    return [
        event
        async for event in client.query("sess-1", "hello", orchestration_capability=capability)
    ]


async def test_forged_capability_is_rejected_without_yield() -> None:
    client = make_connected_client()
    capability = generate_orchestration_capability("sess-1", run_id="run-1")
    frame = {
        "type": "orchestration_request",
        "id": "sess-1",
        "request_id": uuid.uuid4().hex,
        "capability": "forged-token",
        "operation": "ping_thread_bridge",
        "arguments": {"message": "ping"},
    }

    events = await run_query_with_frames(client, capability, [frame])

    assert [event.get("type") for event in events] == ["message"]
    responses = orchestration_responses(client)
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "capability_invalid"
    assert capability.token not in json.dumps(events)
