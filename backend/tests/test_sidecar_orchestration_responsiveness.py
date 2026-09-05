"""Reader-responsiveness tests for the sidecar orchestration loop.

The query reader must continue processing sidecar frames while an
orchestration handler is still running. This is the deadlock-freedom
requirement from the Phase 4 contract. The handler registry is injected
through the constructor, so tests can supply a slow handler through the
public interface.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from yinshi.exceptions import SidecarError
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


async def test_reader_yields_events_while_handler_pending() -> None:
    capability = generate_orchestration_capability("sess-1", run_id="run-1")
    release = asyncio.Event()

    async def slow_handler(arguments: dict[str, Any], *, session_id: str) -> dict[str, Any]:
        await release.wait()
        return {"status": "ok", "echo": "slow"}

    client = make_connected_client({"ping_thread_bridge": slow_handler})
    frame = {
        "type": "orchestration_request",
        "id": "sess-1",
        "request_id": uuid.uuid4().hex,
        "capability": capability.token,
        "operation": "ping_thread_bridge",
        "arguments": {"message": "ping"},
    }
    message_event = {"id": "sess-1", "type": "message", "data": {"type": "assistant"}}
    frames = iter([frame, message_event])

    async def read_line() -> dict[str, Any]:
        return next(frames)

    client._read_line = read_line

    generator = client.query("sess-1", "hello", orchestration_capability=capability)
    try:
        first_event = await asyncio.wait_for(generator.__anext__(), timeout=1.0)
        assert first_event == message_event
    finally:
        release.set()
        await generator.aclose()


async def test_connection_lost_drains_pending_handler_without_response() -> None:
    capability = generate_orchestration_capability("sess-1", run_id="run-1")
    release = asyncio.Event()

    async def slow_handler(arguments: dict[str, Any], *, session_id: str) -> dict[str, Any]:
        await release.wait()
        return {"status": "ok", "echo": "slow"}

    client = make_connected_client({"ping_thread_bridge": slow_handler})
    frame = {
        "type": "orchestration_request",
        "id": "sess-1",
        "request_id": uuid.uuid4().hex,
        "capability": capability.token,
        "operation": "ping_thread_bridge",
        "arguments": {"message": "ping"},
    }
    pending = iter([frame, None])

    async def read_line() -> dict[str, Any] | None:
        await asyncio.sleep(0)
        return next(pending)

    client._read_line = read_line

    with pytest.raises(SidecarError, match="connection lost"):
        async for _ in client.query("sess-1", "hello", orchestration_capability=capability):
            pass
    release.set()
    await asyncio.sleep(0.05)

    # The drained handler never answered: no response frame was written.
    messages: list[dict[str, Any]] = []
    for call in client._writer.write.call_args_list:
        payload = call.args[0]
        messages.extend(json.loads(line) for line in payload.decode().splitlines() if line.strip())
    assert not [message for message in messages if message.get("type") == "orchestration_response"]
