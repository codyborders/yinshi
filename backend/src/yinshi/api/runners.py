"""Cloud runner registration and heartbeat API routes."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response

from yinshi.api.deps import require_tenant
from yinshi.config import get_settings
from yinshi.exceptions import (
    GitHubAccessError,
    GitHubAppError,
    RunnerAuthenticationError,
    RunnerRegistrationError,
)
from yinshi.models import (
    CloudRunnerCreate,
    CloudRunnerOut,
    CloudRunnerRegistrationOut,
    RunnerCapabilityCreateIn,
    RunnerCapabilityOut,
    RunnerGitHubAccessErrorDetail,
    RunnerGitHubAccessIn,
    RunnerGitHubAccessOut,
    RunnerHeartbeatIn,
    RunnerHeartbeatOut,
    RunnerNoiseKeyConfirmationIn,
    RunnerRegisterIn,
    RunnerRegisterOut,
)
from yinshi.rate_limit import limiter
from yinshi.services.github_app import resolve_github_clone_access as _resolve_github_clone_access
from yinshi.services.runner_capabilities import (
    RUNNER_PROTOCOL_VERSION,
    create_runner_capability,
)
from yinshi.services.runner_relay import (
    runner_relay_broker,
    store_runner_transfer_grant,
)
from yinshi.services.runners import (
    authenticate_runner_token,
    confirm_runner_noise_key,
    create_runner_registration,
    get_runner_for_user,
    record_runner_heartbeat,
    register_runner,
    revoke_runner_for_user,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["runners"])
_RUNNER_BEARER_REQUIRED = "Runner bearer token is required"


def _request_control_url(request: Request) -> str:
    """Return the external origin, honoring forwarding only from configured proxies."""
    scheme = request.url.scheme
    netloc = request.url.netloc
    client_host = request.client.host.lower() if request.client is not None else ""
    if client_host in get_settings().trusted_proxy_ip_set:
        forwarded_scheme = request.headers.get("x-forwarded-proto")
        forwarded_host = request.headers.get("x-forwarded-host")
        if forwarded_scheme:
            scheme = forwarded_scheme.split(",", maxsplit=1)[0].strip().lower()
        if forwarded_host:
            netloc = forwarded_host.split(",", maxsplit=1)[0].strip()
    if scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Could not determine control URL scheme")
    candidate = urlsplit(f"{scheme}://{netloc}")
    try:
        candidate_port = candidate.port
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail="Could not determine control URL host"
        ) from error
    if (
        not candidate.hostname
        or candidate.username is not None
        or candidate.password is not None
        or candidate.path
        or candidate.query
        or candidate.fragment
        or any(character.isspace() for character in netloc)
    ):
        raise HTTPException(status_code=400, detail="Could not determine control URL host")
    if candidate_port is not None and not 1 <= candidate_port <= 65_535:
        raise HTTPException(status_code=400, detail="Could not determine control URL host")
    return f"{scheme}://{netloc}"


def _request_relay_url(request: Request, transfer_id: str) -> str:
    """Return an externally visible WebSocket URL without embedding credentials."""
    control_url = _request_control_url(request)
    if control_url.startswith("https://"):
        relay_origin = f"wss://{control_url.removeprefix('https://')}"
    elif control_url.startswith("http://"):
        relay_origin = f"ws://{control_url.removeprefix('http://')}"
    else:
        raise HTTPException(status_code=400, detail="Could not determine relay URL scheme")
    return f"{relay_origin}/api/runner/relay/{transfer_id}"


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
            storage_profile=body.storage_profile,
            control_url=_request_control_url(request),
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post(
    "/api/settings/runner/noise-key/confirm",
    response_model=CloudRunnerOut,
)
@limiter.limit("10/minute")
def confirm_cloud_runner_noise_key(
    body: RunnerNoiseKeyConfirmationIn,
    request: Request,
) -> dict[str, Any]:
    """Record explicit confirmation of the runner's displayed Noise fingerprint."""
    tenant = require_tenant(request)
    try:
        return confirm_runner_noise_key(tenant.user_id, body.noise_public_key)
    except (RunnerRegistrationError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/api/settings/runner/capabilities",
    response_model=RunnerCapabilityOut,
    status_code=201,
)
@limiter.limit("60/minute")
def issue_cloud_runner_capability(
    body: RunnerCapabilityCreateIn,
    request: Request,
) -> RunnerCapabilityOut:
    """Issue least-privilege authority for one paired encrypted runner session."""
    tenant = require_tenant(request)
    runner = get_runner_for_user(tenant.user_id)
    if runner is None:
        raise HTTPException(status_code=404, detail="Runner not configured")
    if runner["status"] != "online":
        raise HTTPException(status_code=409, detail="Runner is not online")
    if not runner["noise_key_confirmed"]:
        raise HTTPException(status_code=409, detail="Runner Noise key must be confirmed")
    runner_public_key = runner["noise_public_key"]
    if not isinstance(runner_public_key, str) or not runner_public_key:
        raise HTTPException(status_code=409, detail="Runner Noise key is not available")
    try:
        capability, claims = create_runner_capability(
            user_id=tenant.user_id,
            runner_id=str(runner["id"]),
            runner_public_key=runner_public_key,
            initiator_public_key=body.initiator_public_key,
            scopes=body.scopes,
            max_session_bytes=body.max_session_bytes,
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    store_runner_transfer_grant(capability, claims)
    return RunnerCapabilityOut(
        capability=capability,
        transfer_id=claims.transfer_id,
        runner_id=claims.runner_id,
        runner_public_key=claims.runner_public_key,
        protocol=RUNNER_PROTOCOL_VERSION,
        issued_at=claims.issued_at,
        expires_at=claims.expires_at,
        max_frame_bytes=claims.max_frame_bytes,
        max_session_bytes=claims.max_session_bytes,
        relay_url=_request_relay_url(request, claims.transfer_id),
    )


@router.delete("/api/settings/runner", status_code=204)
async def revoke_cloud_runner(request: Request) -> Response:
    """Revoke all runner authority and immediately close its outbound relay."""
    tenant = require_tenant(request)
    runner = get_runner_for_user(tenant.user_id)
    revoked = revoke_runner_for_user(tenant.user_id)
    if not revoked or runner is None:
        raise HTTPException(status_code=404, detail="Cloud runner not found")
    await runner_relay_broker.disconnect_runner(str(runner["id"]))
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
            storage_profile=body.storage_profile,
            noise_public_key=body.noise_public_key,
        )
    except RunnerRegistrationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    logger.info("Cloud runner registered")
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
            storage_profile=body.storage_profile,
        )
    except RunnerAuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return heartbeat


@router.post("/runner/github-access", response_model=RunnerGitHubAccessOut | None)
async def resolve_runner_github_access(
    body: RunnerGitHubAccessIn,
    request: Request,
) -> RunnerGitHubAccessOut | None:
    """Resolve GitHub clone access for the runner owner using the bearer token."""
    runner_token = _bearer_token(request)
    try:
        runner_info = authenticate_runner_token(runner_token)
    except RunnerAuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error

    user_id = runner_info["user_id"]
    try:
        clone_access = await _resolve_github_clone_access(user_id, body.remote_url)
    except GitHubAccessError as error:
        error_detail = RunnerGitHubAccessErrorDetail(
            code=error.code,
            message=str(error),
            connect_url=error.connect_url,
            manage_url=error.manage_url,
        )
        raise HTTPException(
            status_code=400,
            detail=error_detail.model_dump(mode="json"),
        ) from error
    except GitHubAppError as error:
        logger.error("GitHub integration failed")
        raise HTTPException(status_code=502, detail="GitHub integration error") from error
    if clone_access is None:
        return None
    return RunnerGitHubAccessOut.model_validate(clone_access)
