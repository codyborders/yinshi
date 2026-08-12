"""Cancellation acknowledgement tests for the sidecar client."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_cancel_during_warmup_is_acknowledged_and_cancels_next_query() -> None:
    """Warmup should route cancellation before completing its own response."""
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()
    reader = asyncio.StreamReader()
    client._reader = reader

    warmup_task = asyncio.create_task(client.warmup("sess-1"))
    await asyncio.sleep(0)
    cancel_tasks = [
        asyncio.create_task(client.cancel("sess-1")),
        asyncio.create_task(client.cancel("sess-1")),
    ]
    await asyncio.sleep(0)

    reader.feed_data(b'{"id":"sess-1","type":"cancel_status","success":true}\n')
    reader.feed_data(b'{"id":"sess-1","type":"warmup_status","success":true}\n')

    await asyncio.gather(warmup_task, *cancel_tasks)
    events = [event async for event in client.query("sess-1", "must not run")]

    written = [json.loads(call.args[0].decode()) for call in client._writer.write.call_args_list]
    assert [message["type"] for message in written] == ["warmup", "cancel"]
    assert events == [{"id": "sess-1", "type": "cancelled"}]
