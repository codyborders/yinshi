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
async def test_sidecar_client_warmup_with_agent_dir_and_settings():
    """warmup should include Pi config options when provided."""
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()

    await client.warmup(
        "sess-2",
        model="opus",
        cwd="/tmp/repo",
        agent_dir="/data/pi-config/agent",
        settings_payload={"retry": {"enabled": False}},
    )

    written = client._writer.write.call_args[0][0].decode()
    msg = json.loads(written.strip())
    assert msg["options"]["agentDir"] == "/data/pi-config/agent"
    assert msg["options"]["settings"] == {"retry": {"enabled": False}}


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


@pytest.mark.asyncio
async def test_sidecar_connect_uses_large_line_limit(monkeypatch: pytest.MonkeyPatch):
    """Connect should raise the stream limit for large catalog payloads.

    The sidecar catalog now includes enough model metadata that the response can
    exceed asyncio's default 64 KiB line limit. This test verifies that the Unix
    stream connection is created with an explicit higher limit.
    """
    from yinshi.services.sidecar import SidecarClient, _SIDECAR_MESSAGE_LIMIT_BYTES

    recorded_kwargs: dict[str, object] = {}

    async def fake_open_unix_connection(path: str, **kwargs: object):
        recorded_kwargs["path"] = path
        recorded_kwargs.update(kwargs)
        reader = AsyncMock()
        reader.readline = AsyncMock(
            return_value=b'{"type":"init_status","success":true}\n'
        )
        writer = MagicMock()
        return reader, writer

    monkeypatch.setattr("asyncio.open_unix_connection", fake_open_unix_connection)

    client = SidecarClient()
    await client.connect("/tmp/test-sidecar.sock")

    assert recorded_kwargs["path"] == "/tmp/test-sidecar.sock"
    assert recorded_kwargs["limit"] == _SIDECAR_MESSAGE_LIMIT_BYTES
    assert client.connected is True


@pytest.mark.asyncio
async def test_sidecar_read_line_converts_limit_errors() -> None:
    """Oversized sidecar messages should raise SidecarError instead of ValueError.

    The socket reader can still reject lines if a future payload exceeds the
    configured cap. This test keeps that failure path stable and domain-specific.
    """
    from yinshi.exceptions import SidecarError
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._reader = AsyncMock()
    client._reader.readline = AsyncMock(
        side_effect=ValueError("Separator is found, but chunk is longer than limit")
    )

    with pytest.raises(SidecarError, match="configured read limit"):
        await client._read_line()


@pytest.mark.asyncio
async def test_sidecar_client_submit_oauth_flow_input() -> None:
    """OAuth manual input submission should use the dedicated sidecar message."""
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()
    client._read_line = AsyncMock(
        return_value={"type": "oauth_submitted", "flow_id": "flow-1"}
    )

    await client.submit_oauth_flow_input("flow-1", "http://localhost:1455/auth/callback?code=abc")

    written = client._writer.write.call_args[0][0].decode()
    msg = json.loads(written.strip())
    assert msg["type"] == "oauth_submit"
    assert msg["flowId"] == "flow-1"
    assert msg["authorizationInput"] == "http://localhost:1455/auth/callback?code=abc"
