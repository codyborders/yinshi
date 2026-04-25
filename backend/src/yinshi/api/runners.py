"""Cloud runner registration and heartbeat API routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from yinshi.api.deps import require_tenant
from yinshi.exceptions import RunnerAuthenticationError, RunnerRegistrationError
from yinshi.models import (
    CloudRunnerCreate,
    CloudRunnerOut,
    CloudRunnerRegistrationOut,
    RunnerHeartbeatIn,
    RunnerHeartbeatOut,
    RunnerRegisterIn,
    RunnerRegisterOut,
)
from yinshi.rate_limit import limiter
from yinshi.services.runners import (
    create_runner_registration,
    get_runner_for_user,
    record_runner_heartbeat,
    register_runner,
    revoke_runner_for_user,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["runners"])
_RUNNER_BEARER_REQUIRED = "Runner bearer token is required"


def _forwarded_header_value(value: str | None, fallback: str) -> str:
    """Return the first proxy header value, or the request-derived fallback."""
    if value is None:
        return fallback
    return value.split(",", maxsplit=1)[0].strip()


def _request_control_url(request: Request) -> str:
    """Return the externally visible API base URL for runner callbacks."""
    normalized_proto = _forwarded_header_value(
        request.headers.get("x-forwarded-proto"),
        request.url.scheme,
    )
    normalized_host = _forwarded_header_value(
        request.headers.get("x-forwarded-host"),
        request.url.netloc,
    )

    if not normalized_proto:
        raise HTTPException(status_code=400, detail="Could not determine control URL scheme")
    if not normalized_host:
        raise HTTPException(status_code=400, detail="Could not determine control URL host")
    return f"{normalized_proto}://{normalized_host}"


def _bearer_token(request: Request) -> str:
    """Extract a bearer token from a runner Authorization header."""
    authorization = request.headers.get("authorization")
    if authorization is None:
        raise HTTPException(status_code=401, detail=_RUNNER_BEARER_REQUIRED)
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail=_RUNNER_BEARER_REQUIRED)
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail=_RUNNER_BEARER_REQUIRED)
    return token


@router.get("/api/settings/runner", response_model=CloudRunnerOut | None)
def get_cloud_runner(request: Request) -> dict[str, Any] | None:
    """Return the current user's cloud runner status, if configured."""
    tenant = require_tenant(request)
    runner = get_runner_for_user(tenant.user_id)
    return runner


@router.post(
    "/api/settings/runner",
    response_model=CloudRunnerRegistrationOut,
    status_code=201,
)
def create_cloud_runner(
    body: CloudRunnerCreate,
    request: Request,
) -> dict[str, Any]:
    """Create a one-time registration token for a user-owned cloud runner."""
    tenant = require_tenant(request)
    try:
        return create_runner_registration(
            tenant.user_id,
            name=body.name,
            cloud_provider=body.cloud_provider,
            region=body.region,
            control_url=_request_control_url(request),
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/api/settings/runner", status_code=204)
def revoke_cloud_runner(request: Request) -> Response:
    """Revoke the current user's cloud runner and all runner bearer tokens."""
    tenant = require_tenant(request)
    revoked = revoke_runner_for_user(tenant.user_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Cloud runner not found")
    return Response(status_code=204)


@router.post("/runner/register", response_model=RunnerRegisterOut, status_code=201)
@limiter.limit("30/minute")
def register_cloud_runner(body: RunnerRegisterIn, request: Request) -> dict[str, Any]:
    """Consume a one-time registration token from a freshly booted runner."""
    try:
        registered = register_runner(
            body.registration_token,
            runner_version=body.runner_version,
            capabilities=body.capabilities,
            data_dir=body.data_dir,
            sqlite_dir=body.sqlite_dir,
            shared_files_dir=body.shared_files_dir,
        )
    except RunnerRegistrationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    logger.info("Cloud runner registered: runner=%s", registered["runner_id"])
    return registered


@router.post("/runner/heartbeat", response_model=RunnerHeartbeatOut)
@limiter.limit("120/minute")
def heartbeat_cloud_runner(body: RunnerHeartbeatIn, request: Request) -> dict[str, Any]:
    """Record liveness and capabilities from a registered cloud runner."""
    runner_token = _bearer_token(request)
    try:
        heartbeat = record_runner_heartbeat(
            runner_token,
            runner_version=body.runner_version,
            capabilities=body.capabilities,
            data_dir=body.data_dir,
            sqlite_dir=body.sqlite_dir,
            shared_files_dir=body.shared_files_dir,
        )
    except RunnerAuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return heartbeat
