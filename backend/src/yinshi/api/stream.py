"""SSE streaming endpoint for agent interaction.

Tests: test_prompt_session_not_found, test_prompt_streams_sidecar_events,
       test_prompt_saves_partial_on_sidecar_error, test_cancel_session_not_found,
       test_cancel_no_active_stream in tests/test_api.py
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from yinshi.db import get_db
from yinshi.exceptions import GitError, SidecarError
from yinshi.services.sidecar import SidecarClient, create_sidecar_connection

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


def _summarize_prompt(prompt: str, max_len: int = 50) -> str:
    """Derive a short workspace display name from a user prompt."""
    # Strip leading filler words
    text = prompt.strip()
    # Truncate to max_len at a word boundary
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0]
    # Clean up trailing punctuation
    text = text.rstrip(".,;:!?-")
    return text or prompt[:max_len]


@router.post("/api/sessions/{session_id}/prompt")
async def prompt_session(session_id: str, body: PromptRequest) -> StreamingResponse:
    """Send a prompt and stream agent events as SSE."""

    # Look up session + workspace path (merged query)
    with get_db() as db:
        session = db.execute(
            "SELECT s.*, w.path as workspace_path, w.id as workspace_id, "
            "w.name as workspace_name, w.branch as workspace_branch "
            "FROM sessions s JOIN workspaces w ON s.workspace_id = w.id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Concurrency guard: reject if already streaming
    if session["status"] == "running":
        raise HTTPException(status_code=409, detail="Session already has an active stream")

    workspace_path = session["workspace_path"]
    model = body.model or session["model"]
    prompt = body.prompt
    turn_id = uuid.uuid4().hex

    logger.info(
        "Prompt received: session=%s prompt_len=%d model=%s",
        session_id, len(prompt), model,
    )

    # Save user message + set status to running
    with get_db() as db:
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

        try:
            sidecar = await create_sidecar_connection()
            async with _active_sessions_lock:
                _active_sessions[session_id] = sidecar
            await sidecar.warmup(session_id, model=model, cwd=workspace_path)

            logger.info("Streaming started: session=%s turn_id=%s", session_id, turn_id)

            async for event in sidecar.query(
                session_id, prompt, model=model, cwd=workspace_path
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
                            with get_db() as db:
                                if assistant_msg_id is None:
                                    assistant_msg_id = uuid.uuid4().hex
                                    db.execute(
                                        "INSERT INTO messages (id, session_id, role, content, turn_id) "
                                        "VALUES (?, ?, 'assistant', ?, ?)",
                                        (assistant_msg_id, session_id, accumulated, turn_id),
                                    )
                                else:
                                    db.execute(
                                        "UPDATE messages SET content = ? WHERE id = ?",
                                        (accumulated, assistant_msg_id),
                                    )
                                db.commit()

                    # On result, finalize with full_message
                    if data.get("type") == "result":
                        if assistant_msg_id:
                            with get_db() as db:
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
            yield f"data: {json.dumps({'type': 'error', 'error': 'An internal error occurred'})}\n\n"

        finally:
            with get_db() as db:
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
async def cancel_session(session_id: str) -> dict[str, str]:
    """Cancel the active sidecar operation for a session."""

    # Verify session exists
    with get_db() as db:
        session = db.execute(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    sidecar = _active_sessions.get(session_id)
    if not sidecar:
        raise HTTPException(status_code=409, detail="No active stream for this session")

    await sidecar.cancel(session_id)
    logger.info("Cancel requested: session=%s", session_id)
    return {"status": "cancelled"}
