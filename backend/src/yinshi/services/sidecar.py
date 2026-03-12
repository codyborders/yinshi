"""Async Unix socket client for communicating with the Node.js pi sidecar."""

import asyncio
import json
import logging
from typing import AsyncIterator

from yinshi.config import get_settings
from yinshi.exceptions import SidecarError, SidecarNotConnectedError

logger = logging.getLogger(__name__)


class SidecarClient:
    """Async client for a single sidecar connection via Unix domain socket.

    Each instance owns one socket connection. Use one per active session
    to avoid message interleaving between concurrent sessions.
    """

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Connect to the sidecar Unix socket."""
        settings = get_settings()
        socket_path = settings.sidecar_socket_path
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(socket_path)
            self._connected = True

            init_line = await self._reader.readline()
            if init_line:
                init_msg = json.loads(init_line.decode())
                if init_msg.get("type") == "init_status" and init_msg.get("success"):
                    logger.info("Connected to sidecar at %s", socket_path)
                else:
                    raise SidecarError(f"Sidecar init failed: {init_msg}")
        except FileNotFoundError:
            raise SidecarNotConnectedError(
                f"Sidecar socket not found at {socket_path}. Is the sidecar running?"
            )
        except ConnectionRefusedError:
            raise SidecarNotConnectedError("Sidecar connection refused")

    async def disconnect(self) -> None:
        """Close the connection."""
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._connected = False
        self._reader = None
        self._writer = None

    async def _send(self, message: dict) -> None:
        """Send a JSON message to the sidecar."""
        if not self._connected or not self._writer:
            raise SidecarNotConnectedError("Not connected to sidecar")
        data = json.dumps(message) + "\n"
        self._writer.write(data.encode())
        await self._writer.drain()

    async def _read_line(self) -> dict | None:
        """Read a single JSON line from the sidecar."""
        if not self._reader:
            return None
        line = await self._reader.readline()
        if not line:
            return None
        return json.loads(line.decode())

    async def warmup(
        self,
        session_id: str,
        model: str = "minimax",
        cwd: str = ".",
        api_key: str | None = None,
    ) -> None:
        """Pre-create a pi session on the sidecar."""
        options: dict = {"model": model, "cwd": cwd}
        if api_key:
            options["apiKey"] = api_key
        await self._send({
            "type": "warmup",
            "id": session_id,
            "options": options,
        })

    async def query(
        self,
        session_id: str,
        prompt: str,
        model: str = "minimax",
        cwd: str = ".",
        api_key: str | None = None,
    ) -> AsyncIterator[dict]:
        """Send a prompt and yield streaming events from the sidecar."""
        options: dict = {"model": model, "cwd": cwd}
        if api_key:
            options["apiKey"] = api_key
        await self._send({
            "type": "query",
            "id": session_id,
            "prompt": prompt,
            "options": options,
        })

        while True:
            msg = await self._read_line()
            if msg is None:
                raise SidecarError("Sidecar connection lost")

            # On a dedicated connection, all messages should be for this session,
            # but filter defensively in case of protocol quirks.
            if msg.get("id") and msg.get("id") != session_id:
                continue

            yield msg

            msg_type = msg.get("type")
            if msg_type == "error":
                break
            if msg_type == "message":
                data = msg.get("data", {})
                if data.get("type") == "result":
                    break

    async def cancel(self, session_id: str) -> None:
        """Cancel an active session."""
        await self._send({"type": "cancel", "id": session_id})

    async def ping(self) -> bool:
        """Health check the sidecar."""
        try:
            await self._send({"type": "ping"})
            msg = await self._read_line()
            return msg is not None and msg.get("type") == "pong"
        except Exception:
            return False


async def create_sidecar_connection() -> SidecarClient:
    """Create a new sidecar connection. Each caller gets its own socket."""
    client = SidecarClient()
    await client.connect()
    return client
