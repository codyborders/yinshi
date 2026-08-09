"""Chunked encrypted upload routes for browser-to-runner file operations."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from yinshi.api.deps import require_tenant
from yinshi.exceptions import PiConfigError
from yinshi.services.encrypted_uploads import (
    EncryptedUpload,
    EncryptedUploadManager,
)
from yinshi.services.pi_config import import_from_upload

router = APIRouter()


class EncryptedUploadStartRequest(BaseModel):
    purpose: str = Field(..., min_length=1, max_length=32)
    filename: str = Field(..., min_length=1, max_length=255)
    size_bytes: int = Field(..., ge=1, le=50 * 1024 * 1024)
    sha256: str = Field(..., min_length=64, max_length=64)


class EncryptedUploadChunkRequest(BaseModel):
    data: str = Field(..., min_length=1, max_length=32_000)


class EncryptedUploadResponse(BaseModel):
    id: str
    purpose: str
    filename: str
    size_bytes: int
    next_chunk_index: int


def _manager(request: Request) -> EncryptedUploadManager:
    manager = getattr(request.app.state, "encrypted_upload_manager", None)
    if not isinstance(manager, EncryptedUploadManager):
        raise RuntimeError("encrypted upload manager is unavailable")
    return manager


def _response(upload: EncryptedUpload) -> EncryptedUploadResponse:
    return EncryptedUploadResponse(
        id=upload.id,
        purpose=upload.purpose,
        filename=upload.filename,
        size_bytes=upload.size_bytes,
        next_chunk_index=upload.next_chunk_index,
    )


def _decode_chunk(value: str) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise ValueError("encrypted upload chunk encoding is invalid")
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("encrypted upload chunk encoding is invalid") from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != value:
        raise ValueError("encrypted upload chunk encoding is not canonical")
    return decoded


@router.post(
    "/api/settings/pi-config/uploads",
    response_model=EncryptedUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_encrypted_upload(
    body: EncryptedUploadStartRequest,
    request: Request,
) -> EncryptedUploadResponse:
    """Reserve one bounded owner-scoped Pi config upload."""
    tenant = require_tenant(request)
    try:
        upload = _manager(request).start(
            user_id=tenant.user_id,
            purpose=body.purpose,
            filename=body.filename,
            size_bytes=body.size_bytes,
            sha256_hex=body.sha256,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _response(upload)


@router.post(
    "/api/settings/pi-config/uploads/{upload_id}/chunks/{chunk_index}",
    response_model=EncryptedUploadResponse,
)
def append_encrypted_upload_chunk(
    upload_id: str,
    chunk_index: int,
    body: EncryptedUploadChunkRequest,
    request: Request,
) -> EncryptedUploadResponse:
    """Append or idempotently retry one exact encrypted upload chunk."""
    tenant = require_tenant(request)
    try:
        upload = _manager(request).append(
            user_id=tenant.user_id,
            upload_id=upload_id,
            chunk_index=chunk_index,
            chunk=_decode_chunk(body.data),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Encrypted upload not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _response(upload)


@router.post(
    "/api/settings/pi-config/uploads/{upload_id}/complete",
    status_code=status.HTTP_201_CREATED,
)
async def complete_encrypted_upload(upload_id: str, request: Request) -> dict[str, Any]:
    """Verify and consume one upload before invoking the existing Pi importer."""
    tenant = require_tenant(request)
    try:
        completed = _manager(request).complete(
            user_id=tenant.user_id,
            upload_id=upload_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Encrypted upload not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if completed.purpose != "pi_config":
        raise RuntimeError("encrypted upload purpose changed before completion")
    try:
        return await import_from_upload(
            user_id=tenant.user_id,
            data_dir=tenant.data_dir,
            zip_data=completed.data,
            filename=completed.filename,
        )
    except PiConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/api/settings/pi-config/uploads/{upload_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def cancel_encrypted_upload(upload_id: str, request: Request) -> None:
    """Discard one incomplete upload idempotently."""
    tenant = require_tenant(request)
    try:
        _manager(request).cancel(user_id=tenant.user_id, upload_id=upload_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Encrypted upload not found") from exc
