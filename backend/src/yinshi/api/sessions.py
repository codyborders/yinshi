"""Endpoints for agent sessions."""

import base64
import binascii
import gzip
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Literal, NoReturn

from fastapi import APIRouter, HTTPException, Query, Request

from yinshi.api.deps import (
    check_session_owner,
    check_workspace_owner,
    get_db_for_request,
    run_db_operation_for_request,
)
from yinshi.model_catalog import normalize_model_ref
from yinshi.models import (
    MessageHistoryBundleOut,
    MessageHistoryFieldChunkOut,
    MessageHistoryPageOut,
    MessageOut,
    SessionCreate,
    SessionOut,
    SessionUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sessions"])

_UPDATABLE_COLUMNS = {"model", "title"}
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
_MESSAGE_HISTORY_FIELD_CHARS_MAX = 262_144
_MESSAGE_HISTORY_FIELD_BYTES_MAX = 900_000
_MESSAGE_HISTORY_CURSOR_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MESSAGE_HISTORY_CURSOR_VERSION = 1
_MESSAGE_HISTORY_BUNDLE_PAGE_LIMIT = 64
_MESSAGE_HISTORY_BUNDLE_RAW_BYTES_MAX = 4 * 1_024 * 1_024
_MESSAGE_HISTORY_BUNDLE_RESPONSE_BYTES_MAX = 900_000
_MESSAGE_HISTORY_SNAPSHOT_MAX = 9_007_199_254_740_991


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
            """INSERT INTO sessions (workspace_id, status, model, pi_context_version, title)
               VALUES (?, 'idle', ?, 1, ?)""",
            (workspace_id, body.model, body.title),
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


def _message_history_field_response(
    field_value: str,
    offset: int,
    chunk_length: int,
) -> dict[str, Any]:
    """Build one exact field response for bounded byte measurement."""
    field_length = len(field_value)
    if chunk_length < 0 or offset + chunk_length > field_length:
        raise ValueError("message history field chunk length is invalid")
    chunk_end = offset + chunk_length
    next_offset: int | None = chunk_end if chunk_end < field_length else None
    return {
        "value": field_value[offset:chunk_end],
        "offset": offset,
        "next_offset": next_offset,
    }


def _message_history_field_response_bytes(response: dict[str, Any]) -> int:
    """Return bytes from Starlette's compact UTF-8 JSON response encoding."""
    return len(
        json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _fit_message_history_field_response(field_value: str, offset: int) -> dict[str, Any]:
    """Select the longest candidate field prefix within the response byte cap."""
    remaining = len(field_value) - offset
    candidate_length = min(remaining, _MESSAGE_HISTORY_FIELD_CHARS_MAX)
    candidate = _message_history_field_response(field_value, offset, candidate_length)
    if _message_history_field_response_bytes(candidate) <= _MESSAGE_HISTORY_FIELD_BYTES_MAX:
        return candidate
    if candidate_length == 0:
        raise RuntimeError("empty message history field response exceeded the size limit")

    best_response: dict[str, Any] | None = None
    low = 1
    high = candidate_length - 1
    while low <= high:
        chunk_length = (low + high) // 2
        response = _message_history_field_response(field_value, offset, chunk_length)
        if _message_history_field_response_bytes(response) <= _MESSAGE_HISTORY_FIELD_BYTES_MAX:
            best_response = response
            low = chunk_length + 1
        else:
            high = chunk_length - 1
    if best_response is None:
        raise RuntimeError("message history field response exceeded the size limit")
    return best_response


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
    return _fit_message_history_field_response(field_value, offset)


def _history_bundle_json_bytes(value: Any) -> bytes:
    """Encode JSON exactly like Starlette while preserving UTF-8 text."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _history_bundle_envelope(
    records: list[dict[str, Any]],
    *,
    cursor: str | None,
    next_cursor: str | None,
    through: str | None,
    snapshot: int,
    snapshot_count: int,
    snapshot_tail: str | None,
    active_run_id: str | None,
) -> dict[str, Any]:
    """Compress complete message records into one deterministic envelope."""
    payload = _history_bundle_json_bytes(records)
    compressed = gzip.compress(payload, compresslevel=6, mtime=0)
    encoded = base64.urlsafe_b64encode(compressed).rstrip(b"=").decode("ascii")
    return {
        "version": 1,
        "encoding": "gzip+base64url",
        "raw_bytes": len(payload),
        "message_count": len(records),
        "cursor": cursor,
        "next_cursor": next_cursor,
        "through": through,
        "snapshot": snapshot,
        "snapshot_count": snapshot_count,
        "snapshot_tail": snapshot_tail,
        "active_run_id": active_run_id,
        "data": encoded,
    }


def _raise_history_bundle_snapshot_changed() -> NoReturn:
    """Reject a continuation whose durable snapshot no longer exists."""
    raise HTTPException(
        status_code=409,
        detail={
            "code": "history_bundle_snapshot_changed",
            "message": "Stored message history changed during bundle loading",
        },
    )


def _message_history_bundle(
    db: Any,
    session_id: str,
    request: Request,
    cursor: tuple[str, str] | None,
    cursor_encoded: str | None,
    through: tuple[str, str] | None,
    through_encoded: str | None,
    snapshot: int | None,
    snapshot_count: int | None,
    snapshot_tail: tuple[str, str] | None,
    snapshot_tail_encoded: str | None,
    bundled_active_run_id: str | None,
) -> dict[str, Any]:
    """Read one snapshot-bound compressed page from one database connection."""
    db.execute("BEGIN")
    session = db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    check_session_owner(db, session_id, request)
    if cursor is None:
        active_run_row = db.execute(
            "SELECT id FROM prompt_runs WHERE session_id = ? "
            "AND status IN ('starting', 'running', 'stopping')",
            (session_id,),
        ).fetchone()
        active_run_id = str(active_run_row["id"]) if active_run_row is not None else None
    else:
        active_run_id = bundled_active_run_id
        if active_run_id is not None:
            active_run_row = db.execute(
                "SELECT 1 FROM prompt_runs WHERE id = ? AND session_id = ?",
                (active_run_id, session_id),
            ).fetchone()
            if active_run_row is None:
                _raise_history_bundle_snapshot_changed()

    if cursor is None:
        snapshot_row = db.execute(
            "SELECT coalesce(max(rowid), 0) AS snapshot, count(*) AS snapshot_count "
            "FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        assert snapshot_row is not None
        snapshot = int(snapshot_row["snapshot"])
        snapshot_count = int(snapshot_row["snapshot_count"])
        if snapshot > _MESSAGE_HISTORY_SNAPSHOT_MAX:
            raise HTTPException(
                status_code=422,
                detail="Message history snapshot exceeds supported range",
            )
        if snapshot_count > _MESSAGE_HISTORY_SNAPSHOT_MAX:
            raise HTTPException(
                status_code=422,
                detail="Message history snapshot exceeds supported range",
            )
        through_row = db.execute(
            "SELECT created_at, id FROM messages WHERE session_id = ? AND rowid <= ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (session_id, snapshot),
        ).fetchone()
        snapshot_tail_row = db.execute(
            "SELECT created_at, id FROM messages WHERE session_id = ? AND rowid = ?",
            (session_id, snapshot),
        ).fetchone()
        if through_row is None or snapshot_tail_row is None:
            return _history_bundle_envelope(
                [],
                cursor=None,
                next_cursor=None,
                through=None,
                snapshot=0,
                snapshot_count=0,
                snapshot_tail=None,
                active_run_id=active_run_id,
            )
        through = (str(through_row["created_at"]), str(through_row["id"]))
        through_encoded = _encode_message_history_cursor(*through)
        snapshot_tail = (
            str(snapshot_tail_row["created_at"]),
            str(snapshot_tail_row["id"]),
        )
        snapshot_tail_encoded = _encode_message_history_cursor(*snapshot_tail)
    else:
        assert through is not None
        assert snapshot is not None
        assert snapshot_count is not None
        assert snapshot_tail is not None
        assert snapshot_tail_encoded is not None
        count_row = db.execute(
            "SELECT count(*) AS snapshot_count FROM messages "
            "WHERE session_id = ? AND rowid <= ?",
            (session_id, snapshot),
        ).fetchone()
        assert count_row is not None
        through_row = db.execute(
            "SELECT 1 FROM messages WHERE session_id = ? AND rowid <= ? "
            "AND created_at = ? AND id = ?",
            (session_id, snapshot, through[0], through[1]),
        ).fetchone()
        snapshot_tail_row = db.execute(
            "SELECT 1 FROM messages WHERE session_id = ? AND rowid = ? "
            "AND created_at = ? AND id = ?",
            (session_id, snapshot, snapshot_tail[0], snapshot_tail[1]),
        ).fetchone()
        if (
            int(count_row["snapshot_count"]) != snapshot_count
            or through_row is None
            or snapshot_tail_row is None
        ):
            _raise_history_bundle_snapshot_changed()
    assert through is not None
    assert through_encoded is not None
    assert snapshot is not None
    assert snapshot_count is not None
    assert snapshot_tail_encoded is not None

    parameters: list[Any] = [session_id, snapshot]
    cursor_clause = ""
    if cursor is not None:
        cursor_clause = "AND (created_at > ? OR (created_at = ? AND id > ?)) "
        parameters.extend((cursor[0], cursor[0], cursor[1]))
    parameters.extend((through[0], through[0], through[1]))
    range_parameters = tuple(parameters)
    metadata_rows = db.execute(
        "SELECT created_at, id, "
        "coalesce(length(CAST(content AS BLOB)), 0) + "
        "coalesce(length(CAST(full_message AS BLOB)), 0) AS field_bytes "
        "FROM messages WHERE session_id = ? AND rowid <= ? "
        f"{cursor_clause}"  # noqa: S608
        "AND (created_at < ? OR (created_at = ? AND id <= ?)) "
        "ORDER BY created_at, id LIMIT ?",
        (*range_parameters, _MESSAGE_HISTORY_BUNDLE_PAGE_LIMIT + 1),
    ).fetchall()
    candidate_count = 0
    estimated_raw_bytes = 2
    for row in metadata_rows[:_MESSAGE_HISTORY_BUNDLE_PAGE_LIMIT]:
        candidate_bytes = int(row["field_bytes"])
        if estimated_raw_bytes + candidate_bytes > _MESSAGE_HISTORY_BUNDLE_RAW_BYTES_MAX:
            break
        estimated_raw_bytes += candidate_bytes
        candidate_count += 1
    if metadata_rows and candidate_count == 0:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "history_bundle_message_too_large",
                "message": "Stored message does not fit a bounded history bundle",
            },
        )
    candidate_rows = db.execute(
        "SELECT * FROM messages WHERE session_id = ? AND rowid <= ? "
        f"{cursor_clause}"  # noqa: S608
        "AND (created_at < ? OR (created_at = ? AND id <= ?)) "
        "ORDER BY created_at, id LIMIT ?",
        (*range_parameters, candidate_count),
    ).fetchall()
    candidates = [
        MessageOut.model_validate(dict(row)).model_dump(mode="json") for row in candidate_rows
    ]

    raw_prefix_count = 0
    raw_bytes = 2
    for candidate in candidates:
        candidate_bytes = len(_history_bundle_json_bytes(candidate))
        separator_bytes = 1 if raw_prefix_count else 0
        if raw_bytes + separator_bytes + candidate_bytes > _MESSAGE_HISTORY_BUNDLE_RAW_BYTES_MAX:
            break
        raw_bytes += separator_bytes + candidate_bytes
        raw_prefix_count += 1
    if candidates and raw_prefix_count == 0:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "history_bundle_message_too_large",
                "message": "Stored message does not fit a bounded history bundle",
            },
        )

    best: dict[str, Any] | None = None
    low = 1
    high = raw_prefix_count
    while low <= high:
        count = (low + high) // 2
        has_more = len(metadata_rows) > count
        next_cursor = None
        if has_more:
            row = candidate_rows[count - 1]
            next_cursor = _encode_message_history_cursor(str(row["created_at"]), str(row["id"]))
        envelope = _history_bundle_envelope(
            candidates[:count],
            cursor=cursor_encoded,
            next_cursor=next_cursor,
            through=through_encoded,
            snapshot=snapshot,
            snapshot_count=snapshot_count,
            snapshot_tail=snapshot_tail_encoded,
            active_run_id=active_run_id,
        )
        if len(_history_bundle_json_bytes(envelope)) <= _MESSAGE_HISTORY_BUNDLE_RESPONSE_BYTES_MAX:
            best = envelope
            low = count + 1
        else:
            high = count - 1
    if best is None and candidates:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "history_bundle_message_too_large",
                "message": "Stored message does not fit a bounded history bundle",
            },
        )
    if best is not None:
        return best
    return _history_bundle_envelope(
        [],
        cursor=cursor_encoded,
        next_cursor=None,
        through=through_encoded,
        snapshot=snapshot,
        snapshot_count=snapshot_count,
        snapshot_tail=snapshot_tail_encoded,
        active_run_id=active_run_id,
    )


@router.get(
    "/api/sessions/{session_id}/messages/bundle",
    response_model=MessageHistoryBundleOut,
)
async def get_message_history_bundle(
    session_id: str,
    request: Request,
    cursor: str | None = Query(default=None, min_length=1, max_length=128),
    through: str | None = Query(default=None, min_length=1, max_length=128),
    snapshot: int | None = Query(
        default=None,
        ge=0,
        le=_MESSAGE_HISTORY_SNAPSHOT_MAX,
    ),
    snapshot_count: int | None = Query(
        default=None,
        ge=0,
        le=_MESSAGE_HISTORY_SNAPSHOT_MAX,
    ),
    snapshot_tail: str | None = Query(default=None, min_length=1, max_length=128),
    active_run_id: str | None = Query(
        default=None,
        min_length=4,
        max_length=32,
        pattern=r"^(?:none|[0-9a-f]{32})$",
    ),
) -> dict[str, Any]:
    """Return complete messages in a bounded compressed snapshot page."""
    continuation_values = (
        cursor,
        through,
        snapshot,
        snapshot_count,
        snapshot_tail,
        active_run_id,
    )
    if any(value is not None for value in continuation_values) and not all(
        value is not None for value in continuation_values
    ):
        raise HTTPException(status_code=422, detail="Invalid message history bundle cursor")
    if cursor is not None and snapshot == 0:
        raise HTTPException(status_code=422, detail="Invalid message history bundle cursor")
    decoded_cursor = _decode_message_history_cursor(cursor) if cursor is not None else None
    decoded_through = _decode_message_history_cursor(through) if through is not None else None
    decoded_snapshot_tail = (
        _decode_message_history_cursor(snapshot_tail) if snapshot_tail is not None else None
    )
    if decoded_cursor is not None and decoded_through is not None:
        if decoded_cursor >= decoded_through:
            raise HTTPException(status_code=422, detail="Invalid message history bundle cursor")
    return await run_db_operation_for_request(
        request,
        lambda db: _message_history_bundle(
            db,
            session_id,
            request,
            decoded_cursor,
            cursor,
            decoded_through,
            through,
            snapshot,
            snapshot_count,
            decoded_snapshot_tail,
            snapshot_tail,
            None if active_run_id == "none" else active_run_id,
        ),
    )


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
