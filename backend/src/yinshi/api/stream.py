"""SSE streaming endpoint for agent interaction.

Tests: test_prompt_session_not_found, test_prompt_streams_sidecar_events,
       test_prompt_saves_partial_on_sidecar_error, test_cancel_session_not_found,
       test_cancel_no_active_stream in tests/test_api.py
"""

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from yinshi.api.deps import check_owner, get_db_for_request, get_tenant, get_user_email
from yinshi.config import get_settings
from yinshi.exceptions import (
    ContainerNotReadyError,
    ContainerStartError,
    CreditExhaustedError,
    GitError,
    KeyNotFoundError,
    SidecarError,
)
from yinshi.services.keys import record_usage, resolve_api_key_for_prompt
from yinshi.services.sidecar import SidecarClient, create_sidecar_connection
from yinshi.utils.paths import is_path_inside

logger = logging.getLogger(__name__)
router = APIRouter()

# Active sessions: maps session_id -> SidecarClient for cancel support
_active_sessions: dict[str, SidecarClient] = {}
_active_sessions_lock = asyncio.Lock()

# Batch DB writes every N chunks to reduce I/O
_PERSIST_BATCH_SIZE = 10


class PromptRequest(BaseModel):
    prompt: str = Field(..., max_length=100_000)
    model: str | None = None


_FILLER_PREFIXES = [
    "please ", "can you ", "could you ", "would you ",
    "i want you to ", "i need you to ", "help me ",
    "i'd like you to ", "i would like you to ",
    "go ahead and ", "let's ", "we need to ", "we should ",
]

_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "this", "that", "it", "its",
    "my", "your", "our", "their", "some", "all", "any", "so", "up", "out",
    "about", "into", "me", "him", "her", "us", "them", "i", "you", "he",
    "she", "we", "they", "just", "also", "very", "really", "actually",
    "basically", "need", "needs", "want", "make", "sure", "there", "using",
    "how", "what", "when", "where", "which", "who", "why", "new", "now",
})


def _summarize_prompt(prompt: str, max_words: int = 3) -> str:
    """Derive a 2-3 word workspace name from a user prompt."""
    text = prompt.strip()
    if not text:
        return ""

    lower = text.lower()
    for prefix in _FILLER_PREFIXES:
        if lower.startswith(prefix):
            text = text[len(prefix):]
            break

    words = [w.strip(".,;:!?-\"\'()[]{}") for w in text.split()]
    words = [w for w in words if w]
    significant = [w for w in words if w.lower() not in _STOP_WORDS]

    if not significant:
        significant = words[:max_words] if words else [text[:30]]

    result = significant[:max_words]
    summary = "-".join(w.lower() for w in result)

    if len(summary) > 50:
        summary = summary[:50].rsplit("-", 1)[0]
    if not summary:
        summary = "-".join(text.lower().split())[:30]
    return summary


def _validate_workspace_path(tenant: Any, workspace_path: str) -> None:
    """Reject workspace paths that are outside trusted directories.

    Trusted directories are the tenant's own data_dir and, when
    configured, the global ``allowed_repo_base``.
    """
    if is_path_inside(workspace_path, tenant.data_dir):
        return

    settings = get_settings()
    if settings.allowed_repo_base and is_path_inside(workspace_path, settings.allowed_repo_base):
        return

    raise HTTPException(
        status_code=403,
        detail="Workspace path outside allowed directories",
    )


def _remap_path(
    host_path: str, data_dir: str, mount: str = "/data",
) -> str:
    """Translate a host workspace path to the container's mount namespace."""
    resolved = os.path.realpath(host_path)
    base = os.path.realpath(data_dir)
    if not is_path_inside(host_path, data_dir):
        raise ValueError("Path outside user data directory")
    if resolved == base:
        return mount
    relative = os.path.relpath(resolved, base)
    return os.path.join(mount, relative)


def _lookup_session(
    db: sqlite3.Connection, session_id: str, request: Request,
) -> sqlite3.Row | None:
    """Look up a session with workspace info, including owner_email in legacy mode."""
    tenant = get_tenant(request)
    if tenant:
        row = db.execute(
            "SELECT s.*, w.path as workspace_path, w.id as workspace_id, "
            "w.name as workspace_name, w.branch as workspace_branch "
            "FROM sessions s "
            "JOIN workspaces w ON s.workspace_id = w.id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    row = db.execute(
        "SELECT s.*, w.path as workspace_path, w.id as workspace_id, "
        "w.name as workspace_name, w.branch as workspace_branch, "
        "r.owner_email "
        "FROM sessions s "
        "JOIN workspaces w ON s.workspace_id = w.id "
        "JOIN repos r ON w.repo_id = r.id "
        "WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


async def _resolve_execution_context(
    request: Request,
    tenant: Any,
    workspace_path: str,
    model: str,
) -> tuple[str | None, str, str | None, str, str | None]:
    """Resolve container socket, effective cwd, API key, key source, and provider.

    Returns (sidecar_socket, effective_cwd, api_key, key_source, provider).
    Only performs container/key resolution in tenant mode; returns defaults
    for legacy (non-tenant) mode.
    """
    if not tenant:
        return None, workspace_path, None, "platform", None

    _validate_workspace_path(tenant, workspace_path)

    container_mgr = getattr(request.app.state, "container_manager", None)
    sidecar_socket: str | None = None
    effective_cwd = workspace_path

    if container_mgr and is_path_inside(workspace_path, tenant.data_dir):
        try:
            container_info = await container_mgr.ensure_container(
                tenant.user_id, tenant.data_dir,
            )
            sidecar_socket = container_info.socket_path
            effective_cwd = _remap_path(workspace_path, tenant.data_dir)
        except (ContainerStartError, ContainerNotReadyError):
            logger.exception("Container start failed for user %s", tenant.user_id[:8])
            raise HTTPException(
                status_code=503,
                detail="Agent environment temporarily unavailable",
            )
    elif container_mgr:
        logger.warning(
            "Container isolation bypassed: workspace %s is outside data_dir",
            workspace_path,
        )

    sidecar_tmp = await create_sidecar_connection(sidecar_socket)
    try:
        resolved = await sidecar_tmp.resolve_model(model)
        provider: str | None = resolved["provider"]
    finally:
        await sidecar_tmp.disconnect()

    if not provider:
        raise HTTPException(
            status_code=400,
            detail="Could not determine provider for model",
        )

    settings = get_settings()
    platform_key = (
        settings.platform_minimax_api_key if provider == "minimax" else None
    )
    try:
        api_key, key_source = resolve_api_key_for_prompt(
            tenant.user_id, provider, platform_key,
        )
    except (CreditExhaustedError, KeyNotFoundError) as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc

    return sidecar_socket, effective_cwd, api_key, key_source, provider


@router.post("/api/sessions/{session_id}/prompt")
async def prompt_session(
    session_id: str, body: PromptRequest, request: Request,
) -> StreamingResponse:
    """Send a prompt and stream agent events as SSE."""
    with get_db_for_request(request) as db:
        session = _lookup_session(db, session_id, request)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    tenant = get_tenant(request)
    if not tenant:
        check_owner(session["owner_email"], get_user_email(request))

    if session["status"] == "running":
        raise HTTPException(status_code=409, detail="Session already has an active stream")

    workspace_path = session["workspace_path"]
    model = body.model or session["model"]
    prompt = body.prompt
    turn_id = uuid.uuid4().hex

    sidecar_socket, effective_cwd, api_key, key_source, provider = (
        await _resolve_execution_context(request, tenant, workspace_path, model)
    )

    container_mgr = getattr(request.app.state, "container_manager", None)

    logger.info(
        "Prompt received: session=%s prompt_len=%d model=%s provider=%s key_source=%s",
        session_id, len(prompt), model, provider, key_source,
    )

    # Save user message + set status to running
    with get_db_for_request(request) as db:
        db.execute(
            "INSERT INTO messages (session_id, role, content, turn_id) VALUES (?, 'user', ?, ?)",
            (session_id, prompt, turn_id),
        )
        db.execute(
            "UPDATE sessions SET status = 'running' WHERE id = ?",
            (session_id,),
        )
        # Update workspace name on first prompt (when name == branch)
        if session["workspace_name"] == session["workspace_branch"]:
            display_name = _summarize_prompt(prompt)
            db.execute(
                "UPDATE workspaces SET name = ? WHERE id = ?",
                (display_name, session["workspace_id"]),
            )
        db.commit()

    async def event_stream() -> AsyncGenerator[str, None]:
        sidecar: SidecarClient | None = None
        accumulated = ""
        assistant_msg_id: str | None = None
        chunk_count = 0
        usage_data: dict[str, Any] = {}
        result_provider = provider or ""

        try:
            sidecar = await create_sidecar_connection(sidecar_socket)
            async with _active_sessions_lock:
                _active_sessions[session_id] = sidecar
            await sidecar.warmup(
                session_id, model=model, cwd=effective_cwd, api_key=api_key,
            )

            logger.info("Streaming started: session=%s turn_id=%s", session_id, turn_id)

            async for event in sidecar.query(
                session_id, prompt, model=model, cwd=effective_cwd,
                api_key=api_key,
            ):
                event_type = event.get("type")
                logger.debug(
                    "Sidecar event: type=%s keys=%s",
                    event_type,
                    list(event.keys()),
                )

                if event_type == "message":
                    data = event.get("data", {})
                    logger.debug("SSE data: type=%s keys=%s", data.get("type"), list(data.keys()))

                    # Extract assistant text for persistence
                    if data.get("type") == "assistant":
                        content_blocks = data.get("message", {}).get("content", [])
                        for block in content_blocks:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                if text:
                                    accumulated += text
                                    chunk_count += 1

                        # Batched incremental persistence
                        if accumulated and chunk_count % _PERSIST_BATCH_SIZE == 0:
                            with get_db_for_request(request) as db:
                                if assistant_msg_id is None:
                                    assistant_msg_id = uuid.uuid4().hex
                                    db.execute(
                                        (
                                            "INSERT INTO messages "
                                            "(id, session_id, role, content, turn_id) "
                                            "VALUES (?, ?, 'assistant', ?, ?)"
                                        ),
                                        (assistant_msg_id, session_id, accumulated, turn_id),
                                    )
                                else:
                                    db.execute(
                                        "UPDATE messages SET content = ? WHERE id = ?",
                                        (accumulated, assistant_msg_id),
                                    )
                                db.commit()

                    # On result, capture usage and finalize with full_message
                    if data.get("type") == "result":
                        usage_data = data.get("usage", {})
                        result_provider = data.get("provider", result_provider)
                        if assistant_msg_id:
                            with get_db_for_request(request) as db:
                                db.execute(
                                    "UPDATE messages SET full_message = ? WHERE id = ?",
                                    (json.dumps(event), assistant_msg_id),
                                )
                                db.commit()

                    # Yield the SSE event with the inner data
                    yield f"data: {json.dumps(data)}\n\n"

                elif event_type == "error":
                    error_msg = event.get("error", "Unknown sidecar error")
                    yield f"data: {json.dumps({'type': 'error', 'error': error_msg})}\n\n"

                else:
                    # Forward any other event types (content_block_start, tool_use, etc.)
                    yield f"data: {json.dumps(event)}\n\n"

        except (ConnectionError, OSError, GitError, SidecarError) as e:
            logger.error(
                "Sidecar error: session=%s turn_id=%s error=%s",
                session_id, turn_id, e, exc_info=True,
            )
            error_event = {
                "type": "error",
                "error": "An internal error occurred",
            }
            yield f"data: {json.dumps(error_event)}\n\n"

        finally:
            with get_db_for_request(request) as db:
                # Save any unsaved partial content
                if accumulated and assistant_msg_id is None:
                    db.execute(
                        "INSERT INTO messages (session_id, role, content, turn_id) "
                        "VALUES (?, 'assistant', ?, ?)",
                        (session_id, accumulated, turn_id),
                    )
                elif accumulated and assistant_msg_id:
                    # Final flush of accumulated content
                    db.execute(
                        "UPDATE messages SET content = ? WHERE id = ?",
                        (accumulated, assistant_msg_id),
                    )
                # Reset session status
                db.execute(
                    "UPDATE sessions SET status = 'idle' WHERE id = ?",
                    (session_id,),
                )
                db.commit()

            # Record usage if in tenant mode
            if tenant and usage_data:
                try:
                    record_usage(
                        user_id=tenant.user_id,
                        session_id=session_id,
                        provider=result_provider,
                        model=model,
                        usage=usage_data,
                        key_source=key_source,
                    )
                except Exception:
                    logger.exception("Failed to record usage: session=%s", session_id)

            # Keep container alive after activity
            if container_mgr and tenant:
                container_mgr.touch(tenant.user_id)

            async with _active_sessions_lock:
                _active_sessions.pop(session_id, None)
            if sidecar:
                await sidecar.disconnect()

            logger.info(
                "Turn complete: session=%s turn_id=%s chunks=%d content_len=%d",
                session_id, turn_id, chunk_count, len(accumulated),
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/sessions/{session_id}/cancel")
async def cancel_session(session_id: str, request: Request) -> dict[str, str]:
    """Cancel the active sidecar operation for a session."""
    with get_db_for_request(request) as db:
        session = _lookup_session(db, session_id, request)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not get_tenant(request):
        check_owner(session["owner_email"], get_user_email(request))

    sidecar = _active_sessions.get(session_id)
    if not sidecar:
        raise HTTPException(status_code=409, detail="No active stream for this session")

    await sidecar.cancel(session_id)
    logger.info("Cancel requested: session=%s", session_id)
    return {"status": "cancelled"}
