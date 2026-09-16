"""Chunked session attachment routes for every execution runtime."""

from __future__ import annotations

import base64
import binascii
import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from yinshi.api.deps import check_session_owner, get_tenant, run_db_operation_for_request
from yinshi.config import get_settings
from yinshi.services.attachments import (
    ATTACHMENT_BYTES_MAX,
    SessionAttachment,
    append_attachment_chunk,
    complete_attachment,
    delete_attachment,
    start_attachment,
)

router = APIRouter(tags=["attachments"])


class AttachmentStartRequest(BaseModel):
    """Immutable metadata declared before chunk transfer starts."""

    filename: str = Field(..., min_length=1, max_length=255)
    size_bytes: int = Field(..., ge=1, le=ATTACHMENT_BYTES_MAX)
    sha256: str = Field(..., min_length=64, max_length=64)


class AttachmentChunkRequest(BaseModel):
    """One canonical unpadded base64url attachment chunk."""

    data: str = Field(..., min_length=1, max_length=32_000)


class AttachmentResponse(BaseModel):
    """Safe attachment metadata returned to the browser."""

    id: str
    session_id: str
    filename: str
    media_type: str
    size_bytes: int
    status: Literal["uploading", "ready"]
    next_chunk_index: int
    received_bytes: int


def _data_dir(request: Request) -> str:
    tenant = get_tenant(request)
    return tenant.data_dir if tenant is not None else get_settings().user_data_dir


def _response(attachment: SessionAttachment) -> AttachmentResponse:
    return AttachmentResponse(
        id=attachment.id,
        session_id=attachment.session_id,
        filename=attachment.filename,
        media_type=attachment.media_type,
        size_bytes=attachment.size_bytes,
        status=attachment.status,  # type: ignore[arg-type]
        next_chunk_index=attachment.next_chunk_index,
        received_bytes=attachment.received_bytes,
    )


def _decode_chunk(value: str) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise ValueError("attachment chunk encoding is invalid")
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("attachment chunk encoding is invalid") from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != value:
        raise ValueError("attachment chunk encoding is not canonical")
    return decoded


async def _require_session(request: Request, session_id: str) -> None:
    def require(database: sqlite3.Connection) -> None:
        row = database.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found")
        check_session_owner(database, session_id, request)

    await run_db_operation_for_request(request, require)


@router.post(
    "/api/sessions/{session_id}/attachments",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_session_attachment(
    session_id: str,
    body: AttachmentStartRequest,
    request: Request,
) -> AttachmentResponse:
    """Reserve one session-owned attachment upload."""
    await _require_session(request, session_id)
    try:
        attachment = await run_db_operation_for_request(
            request,
            lambda database: start_attachment(
                database,
                data_dir=_data_dir(request),
                session_id=session_id,
                filename=body.filename,
                size_bytes=body.size_bytes,
                sha256_hex=body.sha256,
            ),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _response(attachment)


@router.post(
    "/api/sessions/{session_id}/attachments/{attachment_id}/chunks/{chunk_index}",
    response_model=AttachmentResponse,
)
async def append_session_attachment_chunk(
    session_id: str,
    body: AttachmentChunkRequest,
    request: Request,
    attachment_id: str = Path(..., pattern=r"^[0-9a-f]{32}$"),
    chunk_index: int = Path(..., ge=0, le=9999),
) -> AttachmentResponse:
    """Append or retry one exact attachment chunk."""
    await _require_session(request, session_id)
    try:
        chunk = _decode_chunk(body.data)
        attachment = await run_db_operation_for_request(
            request,
            lambda database: append_attachment_chunk(
                database,
                data_dir=_data_dir(request),
                session_id=session_id,
                attachment_id=attachment_id,
                chunk_index=chunk_index,
                chunk=chunk,
            ),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Attachment not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _response(attachment)


@router.post(
    "/api/sessions/{session_id}/attachments/{attachment_id}/complete",
    response_model=AttachmentResponse,
)
async def complete_session_attachment(
    session_id: str,
    request: Request,
    attachment_id: str = Path(..., pattern=r"^[0-9a-f]{32}$"),
) -> AttachmentResponse:
    """Verify and publish one attachment."""
    await _require_session(request, session_id)
    try:
        attachment = await run_db_operation_for_request(
            request,
            lambda database: complete_attachment(
                database,
                data_dir=_data_dir(request),
                session_id=session_id,
                attachment_id=attachment_id,
            ),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Attachment not found") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail="Attachment upload is incomplete") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _response(attachment)


@router.delete(
    "/api/sessions/{session_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_session_attachment(
    session_id: str,
    request: Request,
    attachment_id: str = Path(..., pattern=r"^[0-9a-f]{32}$"),
) -> None:
    """Delete one attachment that no prompt has claimed."""
    await _require_session(request, session_id)
    try:
        await run_db_operation_for_request(
            request,
            lambda database: delete_attachment(
                database,
                data_dir=_data_dir(request),
                session_id=session_id,
                attachment_id=attachment_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
