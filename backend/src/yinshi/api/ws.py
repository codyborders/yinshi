"""WebSocket endpoint for streaming agent interaction."""

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from yinshi.auth import _auth_disabled, verify_session_token
from yinshi.db import get_db
from yinshi.services.sidecar import create_sidecar_connection

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/{session_id}")
async def websocket_agent(websocket: WebSocket, session_id: str) -> None:
    """WebSocket endpoint for agent streaming.

    Client sends:
        { "type": "prompt", "prompt": "..." }
        { "type": "cancel" }

    Server sends sidecar events:
        { "type": "message", "data": { "type": "assistant", ... } }
        { "type": "message", "data": { "type": "tool_use", ... } }
        { "type": "message", "data": { "type": "result", ... } }
        { "type": "error", "error": "..." }
    """
    await websocket.accept()

    # -- Authenticate the WebSocket connection --
    if not _auth_disabled():
        token = websocket.cookies.get("yinshi_session")
        if not token or not verify_session_token(token):
            await websocket.send_json({"type": "error", "error": "Not authenticated"})
            await websocket.close(code=4001, reason="Not authenticated")
            return

    # -- Look up session and workspace path --
    with get_db() as db:
        session = db.execute(
            "SELECT s.*, w.path as workspace_path FROM sessions s "
            "JOIN workspaces w ON s.workspace_id = w.id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()

    if not session:
        await websocket.send_json({"type": "error", "error": "Session not found"})
        await websocket.close()
        return

    workspace_path = session["workspace_path"]
    model = session["model"]

    # -- Connect to sidecar (dedicated connection for this session) --
    sidecar = None
    try:
        sidecar = await create_sidecar_connection()
        await sidecar.warmup(session_id, model=model, cwd=workspace_path)
    except Exception as e:
        logger.error("Sidecar connection failed: %s", e)
        await websocket.send_json({
            "type": "error",
            "error": "Agent service not available",
        })
        await websocket.close()
        return

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)

            if msg.get("type") == "cancel":
                await sidecar.cancel(session_id)
                continue

            if msg.get("type") == "prompt":
                prompt = msg["prompt"]
                use_model = msg.get("model", model)

                # Save user message + update status in one transaction
                with get_db() as db:
                    db.execute(
                        """INSERT INTO messages (session_id, role, content)
                           VALUES (?, 'user', ?)""",
                        (session_id, prompt),
                    )
                    db.execute(
                        "UPDATE sessions SET status = 'running' WHERE id = ?",
                        (session_id,),
                    )
                    db.commit()

                # Stream sidecar events to client
                assistant_chunks: list[str] = []
                async for event in sidecar.query(
                    session_id, prompt, model=use_model, cwd=workspace_path
                ):
                    event_type = event.get("type")

                    if event_type == "message":
                        data = event.get("data", {})
                        await websocket.send_json(event)

                        # Accumulate assistant text
                        if data.get("type") == "assistant":
                            content = data.get("message", {}).get("content", [])
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block.get("text", "")
                                    if text:
                                        assistant_chunks.append(text)

                        # On result, save assistant message + reset status
                        if data.get("type") == "result":
                            assistant_text = "".join(assistant_chunks)
                            with get_db() as db:
                                if assistant_text:
                                    db.execute(
                                        """INSERT INTO messages
                                           (session_id, role, content, full_message)
                                           VALUES (?, 'assistant', ?, ?)""",
                                        (session_id, assistant_text, json.dumps(event)),
                                    )
                                db.execute(
                                    "UPDATE sessions SET status = 'idle' WHERE id = ?",
                                    (session_id,),
                                )
                                db.commit()
                            assistant_chunks = []

                    elif event_type == "error":
                        await websocket.send_json(event)

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for session %s", session_id)
    except Exception as e:
        logger.error("WebSocket error for session %s: %s", session_id, e)
        try:
            await websocket.send_json({"type": "error", "error": "Internal error"})
        except Exception:
            pass
    finally:
        if sidecar:
            await sidecar.disconnect()
