"""Staging-only operator boundary for destructive managed recovery drills."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/internal/managed-recovery", tags=["managed-recovery"])


class ManagedRecoveryDrillStartIn(BaseModel):
    """Bounded source revision attached to one retained drill result."""

    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


def _require_operator(request: Request) -> None:
    """Require one exact dedicated staging bearer token."""
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    configured_hash = request.app.state.managed_recovery_operator_token_hash
    presented_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if (
        scheme.lower() != "bearer"
        or separator != " "
        or not token
        or not hmac.compare_digest(presented_hash, configured_hash)
    ):
        raise HTTPException(status_code=401, detail="Invalid operator token")


@router.post("/drills", status_code=202, dependencies=[Depends(_require_operator)])
async def start_managed_recovery_drill(
    body: ManagedRecoveryDrillStartIn,
    request: Request,
) -> dict[str, object]:
    """Start one application-owned staging recovery drill."""
    controller = getattr(request.app.state, "managed_recovery_drill_controller", None)
    if controller is None:
        raise HTTPException(status_code=503, detail="Managed recovery drill is unavailable")
    try:
        result = await controller.start(commit_sha=body.commit_sha)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail="Managed recovery drill is active") from error
    if not isinstance(result, dict):
        raise HTTPException(status_code=503, detail="Managed recovery drill is unavailable")
    return result


@router.get("/drills/latest", dependencies=[Depends(_require_operator)])
def get_managed_recovery_drill_status(request: Request) -> dict[str, object]:
    """Return the latest sanitized aggregate drill state."""
    controller = getattr(request.app.state, "managed_recovery_drill_controller", None)
    if controller is None:
        raise HTTPException(status_code=503, detail="Managed recovery drill is unavailable")
    result = controller.status()
    if not isinstance(result, dict):
        raise HTTPException(status_code=503, detail="Managed recovery drill is unavailable")
    return result
