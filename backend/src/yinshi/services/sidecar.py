"""Async Unix socket client for communicating with the Node.js pi sidecar."""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from yinshi.config import get_settings
from yinshi.exceptions import SidecarError, SidecarNotConnectedError
from yinshi.model_catalog import DEFAULT_SESSION_MODEL

logger = logging.getLogger(__name__)

_SIDECAR_MESSAGE_LIMIT_BYTES = 1024 * 1024 * 8


class SidecarClient:
    """Async client for a single sidecar connection via Unix domain socket.

    Each instance owns one socket connection. Use one per active session
    to avoid message interleaving between concurrent sessions.

    Supports ``async with`` for automatic disconnect::

        async with await create_sidecar_connection() as sidecar:
            await sidecar.warmup(...)
    """

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def __aenter__(self) -> "SidecarClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.disconnect()

    async def connect(self, socket_path: str | None = None) -> None:
        """Connect to a sidecar Unix socket.

        Args:
            socket_path: Explicit path to the Unix socket.  When *None*,
                falls back to the global ``sidecar_socket_path`` setting
                used by host-side execution.
        """
        if socket_path is None:
            settings = get_settings()
            socket_path = settings.sidecar_socket_path
        assert isinstance(socket_path, str)
        assert socket_path
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                socket_path,
                limit=_SIDECAR_MESSAGE_LIMIT_BYTES,
            )
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
            except OSError:
                pass
        self._connected = False
        self._reader = None
        self._writer = None

    async def _send(self, message: dict[str, Any]) -> None:
        """Send a JSON message to the sidecar."""
        if not self._connected or not self._writer:
            raise SidecarNotConnectedError("Not connected to sidecar")
        data = json.dumps(message) + "\n"
        self._writer.write(data.encode())
        await self._writer.drain()

    async def _read_line(self) -> dict[str, Any] | None:
        """Read a single JSON line from the sidecar."""
        if not self._reader:
            return None
        try:
            line = await self._reader.readline()
        except ValueError as exc:
            message_text = str(exc)
            if "longer than limit" in message_text:
                raise SidecarError("Sidecar message exceeded the configured read limit") from exc
            raise
        if not line:
            return None
        if len(line) > _SIDECAR_MESSAGE_LIMIT_BYTES:
            raise SidecarError("Sidecar message exceeded the configured read limit")
        message = json.loads(line.decode())
        if not isinstance(message, dict):
            raise SidecarError("Sidecar returned a non-object response")
        return message

    @staticmethod
    def _build_options(
        model: str,
        cwd: str,
        provider_auth: dict[str, Any] | None = None,
        provider_config: dict[str, Any] | None = None,
        agent_dir: str | None = None,
        settings_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the options dict sent with warmup/query messages."""
        options: dict[str, Any] = {"model": model, "cwd": cwd}
        if provider_auth:
            options["providerAuth"] = provider_auth
        if provider_config:
            options["providerConfig"] = provider_config
        if agent_dir:
            options["agentDir"] = agent_dir
        if settings_payload:
            options["settings"] = settings_payload
        return options

    async def warmup(
        self,
        session_id: str,
        model: str = DEFAULT_SESSION_MODEL,
        cwd: str = ".",
        provider_auth: dict[str, Any] | None = None,
        provider_config: dict[str, Any] | None = None,
        agent_dir: str | None = None,
        settings_payload: dict[str, Any] | None = None,
    ) -> None:
        """Pre-create a pi session on the sidecar."""
        await self._send(
            {
                "type": "warmup",
                "id": session_id,
                "options": self._build_options(
                    model,
                    cwd,
                    provider_auth,
                    provider_config,
                    agent_dir=agent_dir,
                    settings_payload=settings_payload,
                ),
            }
        )

    async def query(
        self,
        session_id: str,
        prompt: str,
        model: str = DEFAULT_SESSION_MODEL,
        cwd: str = ".",
        provider_auth: dict[str, Any] | None = None,
        provider_config: dict[str, Any] | None = None,
        agent_dir: str | None = None,
        settings_payload: dict[str, Any] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Send a prompt and yield streaming events from the sidecar."""
        await self._send(
            {
                "type": "query",
                "id": session_id,
                "prompt": prompt,
                "options": self._build_options(
                    model,
                    cwd,
                    provider_auth,
                    provider_config,
                    agent_dir=agent_dir,
                    settings_payload=settings_payload,
                ),
            }
        )

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

    async def resolve_model(
        self,
        model_key: str,
        *,
        agent_dir: str | None = None,
        provider_auth: dict[str, Any] | None = None,
        provider_config: dict[str, Any] | None = None,
    ) -> dict[str, str | None]:
        """Ask the sidecar to resolve a model key.

        Returns {'provider': '...', 'model': '...'}.
        """
        request_id = f"resolve-{model_key}"
        await self._send(
            {
                "type": "resolve",
                "id": request_id,
                "model": model_key,
                "options": self._build_options(
                    model_key,
                    ".",
                    provider_auth,
                    provider_config,
                    agent_dir=agent_dir,
                ),
            }
        )

        msg = await self._read_line()
        if msg is None:
            raise SidecarError("Sidecar connection lost during model resolve")
        if msg.get("type") == "error":
            raise SidecarError(f"Model resolve failed: {msg.get('error', 'unknown')}")
        if msg.get("type") != "resolved":
            raise SidecarError(f"Unexpected response type: {msg.get('type')}")

        provider = msg.get("provider")
        if provider is not None and not isinstance(provider, str):
            raise SidecarError("Resolved provider must be a string or null")
        model = msg.get("model")
        if not isinstance(model, str) or not model:
            raise SidecarError("Resolved model must be a non-empty string")

        return {"provider": provider, "model": model}

    async def get_catalog(self, *, agent_dir: str | None = None) -> dict[str, Any]:
        """Request the provider/model catalog from the sidecar."""
        request_id = "catalog"
        await self._send(
            {
                "type": "catalog",
                "id": request_id,
                "options": {"agentDir": agent_dir} if agent_dir else {},
            }
        )
        msg = await self._read_line()
        if msg is None:
            raise SidecarError("Sidecar connection lost during catalog request")
        if msg.get("type") == "error":
            raise SidecarError(f"Catalog request failed: {msg.get('error', 'unknown')}")
        if msg.get("type") != "catalog":
            raise SidecarError(f"Unexpected response type: {msg.get('type')}")
        return msg

    async def resolve_provider_auth(
        self,
        *,
        provider: str,
        model: str,
        provider_auth: dict[str, Any],
        provider_config: dict[str, Any] | None = None,
        agent_dir: str | None = None,
    ) -> dict[str, Any]:
        """Resolve one provider auth payload into runtime-ready values."""
        request_id = f"auth-resolve-{provider}"
        await self._send(
            {
                "type": "auth_resolve",
                "id": request_id,
                "provider": provider,
                "model": model,
                "providerAuth": provider_auth,
                "providerConfig": provider_config,
                "agentDir": agent_dir,
            }
        )
        msg = await self._read_line()
        if msg is None:
            raise SidecarError("Sidecar connection lost during auth resolve")
        if msg.get("type") == "error":
            raise SidecarError(f"Provider auth resolve failed: {msg.get('error', 'unknown')}")
        if msg.get("type") != "auth_resolved":
            raise SidecarError(f"Unexpected response type: {msg.get('type')}")
        return msg

    async def start_oauth_flow(self, provider: str) -> dict[str, Any]:
        """Start an OAuth connection flow for a provider."""
        request_id = f"oauth-start-{provider}"
        await self._send({"type": "oauth_start", "id": request_id, "provider": provider})
        msg = await self._read_line()
        if msg is None:
            raise SidecarError("Sidecar connection lost during OAuth start")
        if msg.get("type") == "error":
            raise SidecarError(f"OAuth start failed: {msg.get('error', 'unknown')}")
        if msg.get("type") != "oauth_started":
            raise SidecarError(f"Unexpected response type: {msg.get('type')}")
        return msg

    async def get_oauth_flow_status(self, flow_id: str) -> dict[str, Any]:
        """Query the state of a started OAuth flow."""
        request_id = f"oauth-status-{flow_id}"
        await self._send({"type": "oauth_status", "id": request_id, "flowId": flow_id})
        msg = await self._read_line()
        if msg is None:
            raise SidecarError("Sidecar connection lost during OAuth status check")
        if msg.get("type") == "error":
            raise SidecarError(f"OAuth status failed: {msg.get('error', 'unknown')}")
        if msg.get("type") != "oauth_status":
            raise SidecarError(f"Unexpected response type: {msg.get('type')}")
        return msg

    async def submit_oauth_flow_input(
        self,
        flow_id: str,
        authorization_input: str,
    ) -> dict[str, Any]:
        """Submit a pasted OAuth callback URL or authorization code."""
        if not isinstance(flow_id, str):
            raise TypeError("flow_id must be a string")
        normalized_flow_id = flow_id.strip()
        if not normalized_flow_id:
            raise ValueError("flow_id must not be empty")
        if not isinstance(authorization_input, str):
            raise TypeError("authorization_input must be a string")
        normalized_authorization_input = authorization_input.strip()
        if not normalized_authorization_input:
            raise ValueError("authorization_input must not be empty")

        request_id = f"oauth-submit-{normalized_flow_id}"
        await self._send(
            {
                "type": "oauth_submit",
                "id": request_id,
                "flowId": normalized_flow_id,
                "authorizationInput": normalized_authorization_input,
            }
        )
        msg = await self._read_line()
        if msg is None:
            raise SidecarError("Sidecar connection lost during OAuth input submission")
        if msg.get("type") == "error":
            raise SidecarError(f"OAuth input submission failed: {msg.get('error', 'unknown')}")
        if msg.get("type") != "oauth_submitted":
            raise SidecarError(f"Unexpected response type: {msg.get('type')}")
        return msg

    async def clear_oauth_flow(self, flow_id: str) -> None:
        """Clear one completed or failed OAuth flow from the sidecar."""
        request_id = f"oauth-clear-{flow_id}"
        await self._send({"type": "oauth_clear", "id": request_id, "flowId": flow_id})
        msg = await self._read_line()
        if msg is None:
            raise SidecarError("Sidecar connection lost during OAuth cleanup")
        if msg.get("type") == "error":
            raise SidecarError(f"OAuth cleanup failed: {msg.get('error', 'unknown')}")
        if msg.get("type") != "oauth_cleared":
            raise SidecarError(f"Unexpected response type: {msg.get('type')}")

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


async def create_sidecar_connection(
    socket_path: str | None = None,
) -> SidecarClient:
    """Create a new sidecar connection.

    Args:
        socket_path: Explicit Unix socket path.  *None* uses the global
            host-sidecar setting.
    """
    client = SidecarClient()
    await client.connect(socket_path)
    return client
