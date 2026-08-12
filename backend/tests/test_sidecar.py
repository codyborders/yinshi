"""Tests for sidecar client and runtime packaging."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


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
async def test_sidecar_client_release_session():
    """release_session should ask the sidecar to drop one pi session."""
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()

    await client.release_session("sess-9")

    written = client._writer.write.call_args[0][0].decode()
    msg = json.loads(written.strip())
    assert msg["type"] == "session_release"
    assert msg["id"] == "sess-9"


@pytest.mark.asyncio
async def test_release_sessions_releases_each_session_then_disconnects(monkeypatch):
    """release_sessions should free every named session over one connection."""
    from yinshi.services import sidecar as sidecar_module

    client = AsyncMock()
    monkeypatch.setattr(
        sidecar_module,
        "create_sidecar_connection",
        AsyncMock(return_value=client),
    )

    await sidecar_module.release_sessions("/tmp/sidecar.sock", ["a", "b"])

    assert [call.args[0] for call in client.release_session.await_args_list] == ["a", "b"]
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_sessions_survives_any_sidecar_failure(monkeypatch):
    """Releasing is an optimisation, so a broken sidecar must not fail the caller."""
    from yinshi.services import sidecar as sidecar_module

    monkeypatch.setattr(
        sidecar_module,
        "create_sidecar_connection",
        AsyncMock(side_effect=RuntimeError("sidecar exploded")),
    )

    await sidecar_module.release_sessions("/tmp/sidecar.sock", ["a"])


@pytest.mark.asyncio
async def test_sidecar_client_warmup_waits_for_matching_success() -> None:
    """warmup should return only after its matching success acknowledgement."""
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()
    client._read_line = AsyncMock(
        side_effect=[
            {"id": "other-session", "type": "warmup_status", "success": True},
            {"id": "sess-1", "type": "warmup_status", "success": True},
        ]
    )

    await client.warmup("sess-1", model="opus", cwd="/tmp/repo")

    written = client._writer.write.call_args[0][0].decode()
    msg = json.loads(written.strip())
    assert msg["type"] == "warmup"
    assert msg["id"] == "sess-1"
    assert msg["options"]["model"] == "opus"
    assert msg["options"]["cwd"] == "/tmp/repo"
    assert client._read_line.await_count == 2


@pytest.mark.asyncio
async def test_sidecar_client_warmup_raises_for_failure_acknowledgement() -> None:
    """warmup should reject the sidecar's matching failure acknowledgement."""
    from yinshi.exceptions import SidecarError
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()
    client._read_line = AsyncMock(
        return_value={
            "id": "sess-1",
            "type": "warmup_status",
            "success": False,
            "error": "Failed to warm up session",
        }
    )

    with pytest.raises(SidecarError, match="Failed to warm up session"):
        await client.warmup("sess-1")


@pytest.mark.asyncio
async def test_sidecar_client_warmup_has_a_total_timeout(monkeypatch) -> None:
    """warmup should time out while unrelated acknowledgements keep arriving."""
    from yinshi.exceptions import SidecarError
    from yinshi.services import sidecar as sidecar_module

    client = sidecar_module.SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()

    async def unrelated_acknowledgement():
        await asyncio.sleep(0)
        return {"id": "other-session", "type": "warmup_status", "success": True}

    client._read_line = AsyncMock(side_effect=unrelated_acknowledgement)
    monkeypatch.setattr(sidecar_module, "_SIDECAR_WARMUP_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(SidecarError, match="warmup timed out"):
        await client.warmup("sess-1")


@pytest.mark.asyncio
async def test_sidecar_client_warmup_with_agent_dir_and_settings():
    """warmup should include Pi config options when provided."""
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()
    client._read_line = AsyncMock(
        return_value={"id": "sess-2", "type": "warmup_status", "success": True}
    )

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
async def test_sidecar_client_warmup_with_pi_session_file() -> None:
    """warmup should include the durable Pi session file when provided."""
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()
    client._read_line = AsyncMock(
        return_value={"id": "sess-4", "type": "warmup_status", "success": True}
    )

    await client.warmup(
        "sess-4",
        model="opus",
        cwd="/tmp/repo",
        pi_session_file="/home/yinshi/.yinshi/pi-sessions/sess-4.jsonl",
    )

    written = client._writer.write.call_args[0][0].decode()
    msg = json.loads(written.strip())
    assert msg["options"]["piSessionFile"] == "/home/yinshi/.yinshi/pi-sessions/sess-4.jsonl"


@pytest.mark.asyncio
async def test_sidecar_client_warmup_with_git_auth() -> None:
    """warmup should include runtime git auth when present."""
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()
    client._read_line = AsyncMock(
        return_value={"id": "sess-3", "type": "warmup_status", "success": True}
    )

    await client.warmup(
        "sess-3",
        model="opus",
        cwd="/tmp/repo",
        git_auth={
            "strategy": "github_app_https",
            "host": "github.com",
            "accessToken": "installation-token",
        },
    )

    written = client._writer.write.call_args[0][0].decode()
    msg = json.loads(written.strip())
    assert msg["options"]["gitAuth"] == {
        "strategy": "github_app_https",
        "host": "github.com",
        "accessToken": "installation-token",
    }


@pytest.mark.asyncio
async def test_sidecar_client_cancel():
    """Cancel should wait for the matching success acknowledgement."""
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()

    cancellation = asyncio.create_task(client.cancel("sess-1"))
    await asyncio.sleep(0)

    written = client._writer.write.call_args[0][0].decode()
    msg = json.loads(written.strip())
    assert msg == {"type": "cancel", "id": "sess-1"}
    assert not cancellation.done()

    routed = client._route_cancellation(
        "sess-1",
        {
            "id": "sess-1",
            "type": "cancel_status",
            "success": True,
        },
    )
    assert routed is True
    await cancellation


@pytest.mark.asyncio
async def test_sidecar_client_get_runtime_version() -> None:
    """get_runtime_version should validate the dedicated sidecar version response."""
    from yinshi.services.sidecar import SidecarClient

    client = SidecarClient()
    client._connected = True
    client._writer = MagicMock()
    client._writer.drain = AsyncMock()
    client._read_line = AsyncMock(
        return_value={
            "type": "version",
            "package_name": "@earendil-works/pi-coding-agent",
            "installed_version": "0.80.6",
            "node_version": "v20.20.1",
        }
    )

    payload = await client.get_runtime_version()

    written = client._writer.write.call_args[0][0].decode()
    msg = json.loads(written.strip())
    assert msg == {"type": "version", "id": "version"}
    assert payload == {
        "package_name": "@earendil-works/pi-coding-agent",
        "installed_version": "0.80.6",
        "node_version": "v20.20.1",
    }


@pytest.mark.asyncio
async def test_sidecar_not_connected_raises():
    """Operations on disconnected client should raise."""
    from yinshi.exceptions import SidecarNotConnectedError
    from yinshi.services.sidecar import SidecarClient

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
    from yinshi.services.sidecar import _SIDECAR_MESSAGE_LIMIT_BYTES, SidecarClient

    recorded_kwargs: dict[str, object] = {}

    async def fake_open_unix_connection(path: str, **kwargs: object):
        recorded_kwargs["path"] = path
        recorded_kwargs.update(kwargs)
        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=b'{"type":"init_status","success":true}\n')
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
    client._read_line = AsyncMock(return_value={"type": "oauth_submitted", "flow_id": "flow-1"})

    await client.submit_oauth_flow_input("flow-1", "http://localhost:1455/auth/callback?code=abc")

    written = client._writer.write.call_args[0][0].decode()
    msg = json.loads(written.strip())
    assert msg["type"] == "oauth_submit"
    assert msg["flowId"] == "flow-1"
    assert msg["authorizationInput"] == "http://localhost:1455/auth/callback?code=abc"


def test_sidecar_dockerfile_installs_ssh_client() -> None:
    """The sidecar image must include SSH for git remote operations.

    The coding tools invoke git with ``GIT_SSH_COMMAND`` for SSH remotes. If
    the image only includes git, those remote operations fail inside the
    per-user container even though local git commands still work.
    """
    dockerfile_path = Path(__file__).resolve().parents[2] / "sidecar" / "Dockerfile"
    dockerfile_content = dockerfile_path.read_text(encoding="utf-8")

    assert "apt-get install -y --no-install-recommends" in dockerfile_content
    assert "git openssh-client" in dockerfile_content
