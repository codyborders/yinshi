"""Hosted account API for registered desktop devices."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from yinshi.api.deps import require_tenant
from yinshi.rate_limit import limiter
from yinshi.services.desktop_devices import list_desktop_devices, revoke_desktop_device

router = APIRouter(prefix="/api/account/desktop-devices", tags=["desktop-devices"])


class DesktopDeviceOut(BaseModel):
    """Account-visible metadata for one registered desktop device."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    created_at: int
    last_seen_at: int | None
    revoked_at: int | None


@router.get("", response_model=list[DesktopDeviceOut])
@limiter.limit("60/minute")
async def get_desktop_devices(request: Request) -> list[DesktopDeviceOut]:
    """List desktop registrations belonging to the authenticated account."""
    tenant = require_tenant(request)
    devices = list_desktop_devices(user_id=tenant.user_id)
    return [DesktopDeviceOut.model_validate(device, from_attributes=True) for device in devices]


@router.delete("/{device_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_desktop_device(request: Request, device_id: str) -> Response:
    """Revoke one owned desktop registration without exposing foreign IDs."""
    tenant = require_tenant(request)
    if len(device_id) != 32:
        raise HTTPException(status_code=404, detail="Desktop device not found")
    revoked = revoke_desktop_device(user_id=tenant.user_id, device_id=device_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Desktop device not found")
    return Response(status_code=204)
