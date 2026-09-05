"""Query-loop integration tests for the orchestration bridge.

These tests drive ``SidecarClient.query`` with a mock transport and pin the
duplex protocol behavior: capability transport, internal-frame filtering,
bounded handler dispatch, fail-closed errors, and teardown guarantees.
"""

from __future__ import annotations

import json
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


async def test_query_with_capability_sends_it_in_query_options_only() -> None:
    client = make_connected_client()
    capability = generate_orchestration_capability("sess-1", run_id="run-1")
    client._read_line = AsyncMock(
        return_value={"id": "sess-1", "type": "message", "data": {"type": "result"}}
    )

    events = [
        event
        async for event in client.query("sess-1", "hello", orchestration_capability=capability)
    ]

    query_frame = next(
        message for message in written_messages(client) if message.get("type") == "query"
    )
    assert query_frame["options"]["orchestration"] == {"capability": capability.token}
    assert [event.get("type") for event in events] == ["message"]
    assert client._orchestration_capability is None
