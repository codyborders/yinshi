"""Endpoints for agent sessions."""

import base64
import binascii
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from yinshi.api.deps import (
    check_session_owner,
    check_workspace_owner,
    get_db_for_request,
    run_db_operation_for_request,
)
from yinshi.model_catalog import normalize_model_ref
from yinshi.models import (
    MessageHistoryFieldChunkOut,
    MessageHistoryPageOut,
    MessageOut,
    SessionCreate,
    SessionOut,
    SessionUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sessions"])

_UPDATABLE_COLUMNS = {"model"}
_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        "vendor",
        ".next",
        "dist",
        "build",
    }
)
_TREE_FILE_LIMIT = 5000
_MESSAGE_HISTORY_PAGE_LIMIT = 64
_MESSAGE_HISTORY_PAGE_BYTES_MAX = 262_144
_MESSAGE_HISTORY_FIELD_CHARS_MAX = 32_768
_MESSAGE_HISTORY_FIELD_BYTES_MAX = 524_288
_MESSAGE_HISTORY_CURSOR_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MESSAGE_HISTORY_CURSOR_VERSION = 1


def _normalize_session_row(db: Any, row: Any) -> dict[str, Any]:
    """Normalize stored session models and persist repairs on read."""
    normalized_row = dict(row)
    original_model = normalized_row["model"]
    normalized_model = normalize_model_ref(original_model)
    if normalized_model != original_model:
        db.execute(
            "UPDATE sessions SET model = ? WHERE id = ?", (normalized_model, normalized_row["id"])
        )
        db.commit()
        normalized_row["model"] = normalized_model
    if "pi_context_version" not in normalized_row or normalized_row["pi_context_version"] is None:
        normalized_row["pi_context_version"] = 0
    return normalized_row


def _list_workspace_files(workspace_path: str) -> list[str]:
    """List workspace files while excluding bulky build directories."""
    if not os.path.isdir(workspace_path):
        return []

    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(workspace_path):
        dirnames[:] = sorted(dirname for dirname in dirnames if dirname not in _EXCLUDED_DIRS)
        for filename in sorted(filenames):
            relative_path = os.path.relpath(
                os.path.join(dirpath, filename),
                workspace_path,
            )
            files.append(relative_path)
            if len(files) >= _TREE_FILE_LIMIT:
                files.sort()
                return files

    files.sort()
    return files


@router.get("/api/workspaces/{workspace_id}/sessions", response_model=list[SessionOut])
def list_sessions(workspace_id: str, request: Request) -> list[dict[str, Any]]:
    """List all sessions for a workspace."""
    with get_db_for_request(request) as db:
        check_workspace_owner(db, workspace_id, request)
        rows = db.execute(
            "SELECT * FROM sessions WHERE workspace_id = ? ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
        return [_normalize_session_row(db, row) for row in rows]


@router.post(
    "/api/workspaces/{workspace_id}/sessions",
    response_model=SessionOut,
    status_code=201,
)
def create_session(
    workspace_id: str,
    body: SessionCreate,
    request: Request,
) -> dict[str, Any]:
    """Create a new agent session for a workspace."""
    with get_db_for_request(request) as db:
        ws = db.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if not ws:
            raise HTTPException(status_code=404, detail="Workspace not found")
        check_workspace_owner(db, workspace_id, request)

        cursor = db.execute(
            """INSERT INTO sessions (workspace_id, status, model, pi_context_version)
               VALUES (?, 'idle', ?, 1)""",
            (workspace_id, body.model),
        )
        db.commit()
        row = db.execute("SELECT * FROM sessions WHERE rowid = ?", (cursor.lastrowid,)).fetchone()
        assert row is not None, "created session must be queryable"
        return _normalize_session_row(db, row)


@router.get("/api/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: str, request: Request) -> dict[str, Any]:
    """Get a session by ID."""
    with get_db_for_request(request) as db:
        row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        check_session_owner(db, session_id, request)
        return _normalize_session_row(db, row)


@router.patch("/api/sessions/{session_id}", response_model=SessionOut)
def update_session(
    session_id: str,
    body: SessionUpdate,
    request: Request,
) -> dict[str, Any]:
    """Update session fields (currently only model)."""
    with get_db_for_request(request) as db:
        row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        check_session_owner(db, session_id, request)

        updates = {
            k: v for k, v in body.model_dump(exclude_unset=True).items() if k in _UPDATABLE_COLUMNS
        }
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [session_id]
            db.execute(f"UPDATE sessions SET {sets} WHERE id = ?", vals)  # noqa: S608
            db.commit()
        updated = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        assert updated is not None, "updated session must be queryable"
        return _normalize_session_row(db, updated)


@router.get("/api/sessions/{session_id}/messages", response_model=list[MessageOut])
def get_messages(session_id: str, request: Request) -> list[dict[str, Any]]:
    """Get all messages for a session."""
    with get_db_for_request(request) as db:
        sess = db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not sess:
            raise HTTPException(status_code=404, detail="Session not found")
        check_session_owner(db, session_id, request)

        rows = db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _encode_message_history_cursor(created_at: str, message_id: str) -> str:
    """Encode one canonical keyset cursor without exposing message content."""
    created_at_bytes = created_at.encode("utf-8")
    if not 1 <= len(created_at_bytes) <= 64:
        raise RuntimeError("message history timestamp has an invalid length")
    try:
        message_id_bytes = bytes.fromhex(message_id)
    except ValueError as exc:
        raise RuntimeError("message history ID is invalid") from exc
    if len(message_id_bytes) != 16 or message_id_bytes.hex() != message_id:
        raise RuntimeError("message history ID is invalid")
    raw = (
        bytes(
            (
                _MESSAGE_HISTORY_CURSOR_VERSION,
                len(created_at_bytes),
            )
        )
        + created_at_bytes
        + message_id_bytes
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_message_history_cursor(cursor: str) -> tuple[str, str]:
    """Decode and strictly validate one canonical keyset cursor."""
    if _MESSAGE_HISTORY_CURSOR_PATTERN.fullmatch(cursor) is None:
        raise HTTPException(status_code=422, detail="Invalid message history cursor")
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.b64decode(
            cursor.replace("-", "+").replace("_", "/") + padding,
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid message history cursor") from exc
    canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if canonical != cursor or len(raw) < 19:
        raise HTTPException(status_code=422, detail="Invalid message history cursor")
    version = raw[0]
    created_at_length = raw[1]
    if (
        version != _MESSAGE_HISTORY_CURSOR_VERSION
        or not 1 <= created_at_length <= 64
        or len(raw) != 2 + created_at_length + 16
    ):
        raise HTTPException(status_code=422, detail="Invalid message history cursor")
    try:
        created_at = raw[2 : 2 + created_at_length].decode("utf-8")
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid message history cursor") from exc
    message_id = raw[-16:].hex()
    return created_at, message_id


def _utf8_character_length(value: bytes | None) -> int | None:
    """Count Unicode characters without treating embedded nulls as terminators."""
    if value is None:
        return None
    return len(value.decode("utf-8"))


def _message_history_page(
    db: Any,
    session_id: str,
    request: Request,
    cursor: tuple[str, str] | None,
) -> dict[str, Any]:
    """Read one bounded metadata page from a single database connection."""
    session = db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    check_session_owner(db, session_id, request)

    db.create_function(
        "yinshi_character_length",
        1,
        _utf8_character_length,
        deterministic=True,
    )
    parameters: list[Any] = [session_id]
    cursor_clause = ""
    if cursor is not None:
        created_at, message_id = cursor
        cursor_clause = "AND (created_at > ? OR (created_at = ? AND id > ?)) "
        parameters.extend((created_at, created_at, message_id))
    parameters.append(_MESSAGE_HISTORY_PAGE_LIMIT + 1)
    rows = db.execute(
        "SELECT id, created_at, session_id, role, "
        "yinshi_character_length(CAST(content AS BLOB)) AS content_length, "
        "yinshi_character_length(CAST(full_message AS BLOB)) AS full_message_length, "
        "turn_id, turn_status "
        "FROM messages WHERE session_id = ? "
        f"{cursor_clause}"  # noqa: S608
        "ORDER BY created_at, id LIMIT ?",
        parameters,
    ).fetchall()

    messages: list[dict[str, Any]] = []
    page_bytes = 32
    for row in rows[:_MESSAGE_HISTORY_PAGE_LIMIT]:
        message = dict(row)
        message_bytes = len(json.dumps(message, separators=(",", ":"), default=str).encode("utf-8"))
        if page_bytes + message_bytes > _MESSAGE_HISTORY_PAGE_BYTES_MAX:
            if not messages:
                raise RuntimeError("message history metadata exceeded the size limit")
            break
        messages.append(message)
        page_bytes += message_bytes

    has_more = len(rows) > len(messages)
    next_cursor = None
    if has_more:
        last_message = messages[-1]
        next_cursor = _encode_message_history_cursor(
            str(last_message["created_at"]),
            str(last_message["id"]),
        )
    return {"messages": messages, "next_cursor": next_cursor}


@router.get(
    "/api/sessions/{session_id}/messages/page",
    response_model=MessageHistoryPageOut,
)
async def get_message_history_page(
    session_id: str,
    request: Request,
    cursor: str | None = Query(default=None, min_length=1, max_length=128),
) -> dict[str, Any]:
    """Return bounded message metadata using deterministic keyset pagination."""
    decoded_cursor = _decode_message_history_cursor(cursor) if cursor is not None else None
    return await run_db_operation_for_request(
        request,
        lambda db: _message_history_page(db, session_id, request, decoded_cursor),
    )


def _message_history_field_chunk(
    db: Any,
    session_id: str,
    message_id: str,
    request: Request,
    field_name: Literal["content", "full_message"],
    offset: int,
) -> dict[str, Any]:
    """Read one bounded character chunk from an allowlisted message field."""
    session = db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    check_session_owner(db, session_id, request)
    row = db.execute(
        f"SELECT {field_name} AS value "  # noqa: S608
        "FROM messages WHERE id = ? AND session_id = ?",
        (message_id, session_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    field_value = row["value"]
    if field_value is None:
        raise HTTPException(status_code=404, detail="Message field not found")
    assert isinstance(field_value, str)
    field_length = len(field_value)
    if offset > field_length or (field_length > 0 and offset == field_length):
        raise HTTPException(status_code=416, detail="Invalid message field offset")
    value = field_value[offset : offset + _MESSAGE_HISTORY_FIELD_CHARS_MAX]
    chunk_end = offset + len(value)
    next_offset: int | None = chunk_end if chunk_end < field_length else None
    response = {"value": value, "offset": offset, "next_offset": next_offset}
    response_bytes = len(json.dumps(response, separators=(",", ":")).encode("utf-8"))
    if response_bytes > _MESSAGE_HISTORY_FIELD_BYTES_MAX:
        raise RuntimeError("message history field response exceeded the size limit")
    return response


@router.get(
    "/api/sessions/{session_id}/messages/{message_id}/field",
    response_model=MessageHistoryFieldChunkOut,
)
async def get_message_history_field(
    session_id: str,
    message_id: str,
    request: Request,
    name: Literal["content", "full_message"] = Query(),
    offset: int = Query(default=0, ge=0, le=1_000_000_000),
) -> dict[str, Any]:
    """Return one bounded text chunk for a message in the same session."""
    return await run_db_operation_for_request(
        request,
        lambda db: _message_history_field_chunk(
            db,
            session_id,
            message_id,
            request,
            name,
            offset,
        ),
    )


@router.get("/api/sessions/{session_id}/tree")
def get_session_tree(session_id: str, request: Request) -> dict[str, list[str]]:
    """Return the workspace file tree for a session."""
    with get_db_for_request(request) as db:
        row = db.execute(
            "SELECT s.id, w.path as workspace_path "
            "FROM sessions s "
            "JOIN workspaces w ON s.workspace_id = w.id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        check_session_owner(db, session_id, request)

    workspace_path = row["workspace_path"]
    assert isinstance(workspace_path, str)
    return {"files": _list_workspace_files(workspace_path)}
