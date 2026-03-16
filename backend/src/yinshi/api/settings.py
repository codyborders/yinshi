"""API key management endpoints (BYOK -- Bring Your Own Key)."""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from yinshi.api.deps import require_tenant
from yinshi.db import get_control_db
from yinshi.models import ApiKeyCreate, ApiKeyOut
from yinshi.services.crypto import encrypt_api_key
from yinshi.services.keys import get_user_dek

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/keys", response_model=list[ApiKeyOut])
def list_keys(request: Request) -> list[dict[str, Any]]:
    """List API keys (provider + label only, never the key value)."""
    tenant = require_tenant(request)
    with get_control_db() as db:
        rows = db.execute(
            "SELECT id, created_at, provider, label, last_used_at "
            "FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (tenant.user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


@router.post("/keys", response_model=ApiKeyOut, status_code=201)
def add_key(body: ApiKeyCreate, request: Request) -> dict[str, Any]:
    """Store an encrypted API key."""
    tenant = require_tenant(request)
    dek = get_user_dek(tenant.user_id)

    encrypted = encrypt_api_key(body.key, dek)

    with get_control_db() as db:
        cursor = db.execute(
            "INSERT INTO api_keys (user_id, provider, encrypted_key, label) "
            "VALUES (?, ?, ?, ?)",
            (tenant.user_id, body.provider, encrypted, body.label),
        )
        db.commit()
        row = db.execute(
            "SELECT id, created_at, provider, label, last_used_at "
            "FROM api_keys WHERE rowid = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return dict(row)


@router.delete("/keys/{key_id}", status_code=204)
def delete_key(key_id: str, request: Request) -> None:
    """Revoke an API key."""
    tenant = require_tenant(request)
    with get_control_db() as db:
        row = db.execute(
            "SELECT id FROM api_keys WHERE id = ? AND user_id = ?",
            (key_id, tenant.user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Key not found")
        db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        db.commit()
