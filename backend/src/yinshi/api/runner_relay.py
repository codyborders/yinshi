"""Opaque WebSocket relay between browser clients and outbound BYOC runners."""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from yinshi.exceptions import RunnerAuthenticationError
from yinshi.services.runner_relay import (
    RunnerRelayAuthorizationError,
    claim_runner_transfer_grant,
    runner_relay_broker,
)
from yinshi.services.runners import authenticate_runner_token

router = APIRouter(tags=["runner-relay"])
_CLIENT_AUTH_TIMEOUT_SECONDS = 10.0
_CLIENT_CONNECTION_MAX_SECONDS = 3_600.0
_CLIENT_CONNECTION_LIMIT = asyncio.Semaphore(128)
_CLIENT_SLOT_TIMEOUT_SECONDS = 0.05


def _runner_bearer_token(websocket: WebSocket) -> str:
    """Extract one runner bearer token without accepting the WebSocket first."""
    authorization = websocket.headers.get("authorization")
    if authorization is None or not authorization.startswith("Bearer "):
        raise RunnerAuthenticationError("Runner bearer token is required")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise RunnerAuthenticationError("Runner bearer token is required")
    return token


@router.websocket("/runner/relay")
async def runner_relay_socket(websocket: WebSocket) -> None:
    """Accept one authenticated outbound runner and route only framed ciphertext."""
    try:
        runner = authenticate_runner_token(_runner_bearer_token(websocket))
    except (RunnerAuthenticationError, TypeError, ValueError):
        await websocket.close(code=4401, reason="Runner authentication failed")
        return

    runner_id = runner["runner_id"]
    await websocket.accept()
    await runner_relay_broker.register_runner(runner_id, websocket)
    await websocket.send_json(
        {
            "runner_id": runner_id,
            "type": "welcome",
        }
    )
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            control_text = message.get("text")
            if isinstance(control_text, str):
                try:
                    control = json.loads(control_text)
                    if not isinstance(control, dict):
                        raise ValueError("invalid runner relay control")
                    if (
                        set(control) == {"transfer_id", "type"}
                        and control.get("type") == "close"
                        and isinstance(control.get("transfer_id"), str)
                    ):
                        await runner_relay_broker.runner_closed_transfer(
                            runner_id,
                            control["transfer_id"],
                        )
                        continue
                    if (
                        set(control) == {"job_id", "type"}
                        and control.get("type") == "quiesced"
                        and isinstance(control.get("job_id"), str)
                    ):
                        await runner_relay_broker.runner_quiesced(
                            runner_id,
                            control["job_id"],
                        )
                        continue
                    raise ValueError("invalid runner relay control")
                except (json.JSONDecodeError, RunnerRelayAuthorizationError, ValueError):
                    await websocket.close(code=4400, reason="Runner relay control was rejected")
                    break
            ciphertext = message.get("bytes")
            if not isinstance(ciphertext, bytes):
                await websocket.close(code=4400, reason="Binary relay frames are required")
                break
            try:
                await runner_relay_broker.runner_frame(runner_id, ciphertext)
            except (RunnerRelayAuthorizationError, TypeError, ValueError):
                await websocket.close(code=4400, reason="Runner relay frame was rejected")
                break
    except WebSocketDisconnect:
        pass
    finally:
        await runner_relay_broker.unregister_runner(runner_id, websocket)


@router.websocket("/api/runner/relay/{transfer_id}")
async def client_relay_socket(websocket: WebSocket, transfer_id: str) -> None:
    """Authorize one capability and relay bounded ciphertext without persistence."""
    try:
        await asyncio.wait_for(
            _CLIENT_CONNECTION_LIMIT.acquire(),
            timeout=_CLIENT_SLOT_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        await websocket.close(code=4429, reason="Runner relay connection limit reached")
        return
    sender_task: asyncio.Task[None] | None = None
    attached = False
    try:
        await websocket.accept()
        async with asyncio.timeout(_CLIENT_AUTH_TIMEOUT_SECONDS):
            capability = await websocket.receive_text()
        grant = claim_runner_transfer_grant(transfer_id, capability)
        await runner_relay_broker.attach_client(grant, websocket)
        attached = True
        await websocket.send_json({"type": "ready"})
        sender_task = asyncio.create_task(
            runner_relay_broker.send_client_frames(transfer_id),
            name=f"runner-relay-client-{transfer_id}",
        )
        async with asyncio.timeout(_CLIENT_CONNECTION_MAX_SECONDS):
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                ciphertext = message.get("bytes")
                if not isinstance(ciphertext, bytes):
                    await websocket.close(code=4400, reason="Binary relay frames are required")
                    break
                await runner_relay_broker.client_frame(transfer_id, ciphertext)
    except TimeoutError:
        await websocket.close(code=4408, reason="Runner relay timed out")
    except (RunnerRelayAuthorizationError, TypeError, ValueError):
        await websocket.close(code=4403, reason="Runner relay authorization failed")
    except WebSocketDisconnect:
        pass
    finally:
        if attached:
            await runner_relay_broker.detach_client(transfer_id, websocket)
        if sender_task is not None:
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task
        _CLIENT_CONNECTION_LIMIT.release()
