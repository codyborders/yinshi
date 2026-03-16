"""Tests for sidecar client."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_sidecar_client_send():
    """SidecarClient should serialize messages as newline-delimited JSON."""
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()

    await client._send({"type": "ping"})

    written = client._writer.write.call_args[0][0]
    assert written == b'{"type": "ping"}\n'


@pytest.mark.asyncio
async def test_sidecar_client_warmup():
    """warmup should send the correct message format."""
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()

    await client.warmup("sess-1", model="opus", cwd="/tmp/repo")

    written = client._writer.write.call_args[0][0].decode()
    msg = json.loads(written.strip())
    assert msg["type"] == "warmup"
    assert msg["id"] == "sess-1"
    assert msg["options"]["model"] == "opus"
    assert msg["options"]["cwd"] == "/tmp/repo"


@pytest.mark.asyncio
async def test_sidecar_client_cancel():
    """cancel should send cancel message."""
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()

    await client.cancel("sess-1")

    written = client._writer.write.call_args[0][0].decode()
    msg = json.loads(written.strip())
    assert msg["type"] == "cancel"
    assert msg["id"] == "sess-1"


@pytest.mark.asyncio
async def test_sidecar_not_connected_raises():
    """Operations on disconnected client should raise."""
    from yinshi.services.sidecar import SidecarClient
    from yinshi.exceptions import SidecarNotConnectedError

    client = SidecarClient()
    with pytest.raises(SidecarNotConnectedError):
        await client._send({"type": "ping"})
