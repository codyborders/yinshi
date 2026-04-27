"""Authenticated browser terminal WebSocket endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any, cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from yinshi.auth import auth_disabled, resolve_tenant_from_session_token
from yinshi.config import get_settings
from yinshi.exceptions import ContainerNotReadyError, ContainerStartError, GitError
from yinshi.services.sidecar_runtime import (
    begin_tenant_container_activity,
    end_tenant_container_activity,
    protect_tenant_container,
    remap_path_for_container,
    resolve_tenant_sidecar_context,
)
from yinshi.services.workspace import ensure_workspace_checkout_for_tenant
from yinshi.services.workspace_files import ensure_secret_guardrails
from yinshi.tenant import TenantContext, get_user_db
from yinshi.utils.paths import is_path_inside

logger = logging.getLogger(__name__)
router = APIRouter()

_TERMINAL_CLOSE_POLICY = 1008
_TERMINAL_CLOSE_UNAVAILABLE = 1011


def _allowed_origins() -> set[str]:
    """Return browser origins allowed to open terminal WebSockets."""
    settings = get_settings()
    origins = {settings.frontend_url.rstrip("/")}
    if settings.debug:
        origins.add("http://localhost:5173")
        origins.add("http://127.0.0.1:5173")
    return origins


def _origin_allowed(origin: str | None) -> bool:
    """Return whether a WebSocket Origin header is acceptable."""
    if origin is None:
        return False
    return origin.rstrip("/") in _allowed_origins()


def _tenant_from_websocket(websocket: WebSocket) -> TenantContext | None:
    """Resolve the authenticated tenant from a WebSocket cookie."""
    if auth_disabled():
        return None
    token = websocket.cookies.get("yinshi_session")
    if not token:
        return None
    return resolve_tenant_from_session_token(token)


def _workspace_row(db: sqlite3.Connection, workspace_id: str) -> sqlite3.Row | None:
    """Return the workspace row needed to start a terminal."""
    row = db.execute(
        "SELECT w.id, w.path, w.repo_id, r.root_path, r.agents_md "
        "FROM workspaces w JOIN repos r ON w.repo_id = r.id WHERE w.id = ?",
        (workspace_id,),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


async def _send_sidecar(
    writer: asyncio.StreamWriter,
    message: dict[str, Any],
) -> None:
    """Send one JSON line to the sidecar."""
    writer.write((json.dumps(message) + "\n").encode("utf-8"))
    await writer.drain()


async def _read_sidecar(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read one JSON line from the sidecar."""
    line = await reader.readline()
    if not line:
        return None
    message = json.loads(line.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("sidecar terminal message must be an object")
    return message


async def _proxy_browser_to_sidecar(
    websocket: WebSocket,
    writer: asyncio.StreamWriter,
    terminal_id: str,
    attach_options: dict[str, Any],
) -> None:
    """Forward browser terminal input and control messages to the sidecar."""
    while True:
        payload = await websocket.receive_json()
        if not isinstance(payload, dict):
            continue
        message_type = payload.get("type")
        if message_type == "input":
            data = payload.get("data", "")
            if isinstance(data, str):
                await _send_sidecar(
                    writer,
                    {"type": "terminal_input", "id": terminal_id, "data": data},
                )
        elif message_type == "resize":
            await _send_sidecar(
                writer,
                {
                    "type": "terminal_resize",
                    "id": terminal_id,
                    "cols": payload.get("cols"),
                    "rows": payload.get("rows"),
                },
            )
        elif message_type == "restart":
            await _send_sidecar(
                writer,
                {
                    "type": "terminal_restart",
                    "id": terminal_id,
                    "options": attach_options,
                },
            )
        elif message_type == "kill":
            await _send_sidecar(writer, {"type": "terminal_kill", "id": terminal_id})
        elif message_type == "ping":
            await websocket.send_json({"type": "pong"})
        else:
            await websocket.send_json({"type": "error", "error": "Unknown terminal message"})


async def _proxy_sidecar_to_browser(
    websocket: WebSocket,
    reader: asyncio.StreamReader,
) -> None:
    """Forward sidecar terminal events to the browser."""
    while True:
        message = await _read_sidecar(reader)
        if message is None:
            await websocket.send_json({"type": "error", "error": "Terminal runtime disconnected"})
            return
        if message.get("type") == "init_status":
            continue
        await websocket.send_json(message)


@router.websocket("/api/workspaces/{workspace_id}/terminal")
async def workspace_terminal(websocket: WebSocket, workspace_id: str) -> None:
    """Attach the browser to the persistent terminal for one workspace runtime."""
    if not _origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=_TERMINAL_CLOSE_POLICY)
        return

    tenant = _tenant_from_websocket(websocket)
    if tenant is None:
        await websocket.close(code=_TERMINAL_CLOSE_POLICY)
        return

    settings = get_settings()
    if not settings.container_enabled:
        await websocket.close(code=_TERMINAL_CLOSE_UNAVAILABLE)
        return

    try:
        with get_user_db(tenant) as db:
            await ensure_workspace_checkout_for_tenant(db, tenant, workspace_id)
            row = _workspace_row(db, workspace_id)
            if row is None:
                await websocket.close(code=_TERMINAL_CLOSE_POLICY)
                return
            workspace_path = str(row["path"])
            repo_root_path = str(row["root_path"])
            agents_md = row["agents_md"] if "agents_md" in row.keys() else None
            ensure_secret_guardrails(repo_root_path)
    except (GitError, OSError):
        logger.exception("Failed to prepare terminal workspace: workspace=%s", workspace_id)
        await websocket.close(code=_TERMINAL_CLOSE_UNAVAILABLE)
        return

    if not is_path_inside(workspace_path, tenant.data_dir):
        await websocket.close(code=_TERMINAL_CLOSE_POLICY)
        return

    try:
        runtime = await resolve_tenant_sidecar_context(
            cast(Any, websocket),
            tenant,
            repo_agents_md=agents_md,
            repo_root_path=repo_root_path,
            workspace_path=workspace_path,
            workspace_id=workspace_id,
        )
    except (ContainerStartError, ContainerNotReadyError):
        logger.exception("Failed to start terminal runtime: workspace=%s", workspace_id)
        await websocket.close(code=_TERMINAL_CLOSE_UNAVAILABLE)
        return

    if runtime.socket_path is None:
        await websocket.close(code=_TERMINAL_CLOSE_UNAVAILABLE)
        return

    try:
        effective_cwd = remap_path_for_container(workspace_path, tenant.data_dir)
    except ValueError:
        await websocket.close(code=_TERMINAL_CLOSE_POLICY)
        return

    terminal_id = runtime.runtime_id or workspace_id
    await websocket.accept()
    begin_tenant_container_activity(cast(Any, websocket), tenant, runtime_id=runtime.runtime_id)
    logger.info("Terminal attached: workspace=%s", workspace_id)

    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_unix_connection(runtime.socket_path)
        init_message = await _read_sidecar(reader)
        if init_message is not None and init_message.get("type") != "init_status":
            await websocket.send_json(init_message)

        attach_options = {
            "workspaceId": terminal_id,
            "cwd": effective_cwd,
            "cols": 100,
            "rows": 30,
            "scrollbackLines": settings.terminal_scrollback_lines,
        }
        await _send_sidecar(
            writer,
            {"type": "terminal_attach", "id": terminal_id, "options": attach_options},
        )
        browser_task = asyncio.create_task(
            _proxy_browser_to_sidecar(websocket, writer, terminal_id, attach_options)
        )
        sidecar_task = asyncio.create_task(_proxy_sidecar_to_browser(websocket, reader))
        done, pending = await asyncio.wait(
            {browser_task, sidecar_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()
    except WebSocketDisconnect:
        logger.info("Terminal detached: workspace=%s", workspace_id)
    except (ConnectionError, OSError, ValueError, json.JSONDecodeError):
        logger.exception("Terminal proxy failed: workspace=%s", workspace_id)
        try:
            await websocket.send_json({"type": "error", "error": "Terminal proxy failed"})
        except RuntimeError:
            pass
    finally:
        end_tenant_container_activity(cast(Any, websocket), tenant, runtime_id=runtime.runtime_id)
        protect_tenant_container(
            cast(Any, websocket),
            tenant,
            lease_key=f"terminal:{workspace_id}",
            timeout_s=settings.terminal_keepalive_s,
            runtime_id=runtime.runtime_id,
        )
        if writer is not None:
            try:
                await _send_sidecar(writer, {"type": "terminal_detach", "id": terminal_id})
            except (ConnectionError, OSError):
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
