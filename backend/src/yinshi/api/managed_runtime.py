"""Authenticated managed runtime status and capability routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from yinshi.api.deps import require_tenant
from yinshi.api.runners import _request_relay_url
from yinshi.models import RunnerCapabilityCreateIn, RunnerCapabilityOut
from yinshi.rate_limit import limiter
from yinshi.services.managed_backups import (
    ManagedBackupConflictError,
    get_managed_backup_operation,
    list_managed_backup_archives,
)
from yinshi.services.managed_runners import (
    ManagedRuntimeStatus,
    get_managed_runtime_status,
)
from yinshi.services.managed_runtime_manager import (
    ManagedRuntimeIdentityError,
    ManagedRuntimeProviderError,
    ManagedRuntimeStateError,
    ManagedRuntimeTimeoutError,
)
from yinshi.services.runner_capabilities import (
    RUNNER_PROTOCOL_VERSION,
    create_runner_capability,
)
from yinshi.services.runner_relay import store_runner_transfer_grant
from yinshi.services.runners import get_managed_runner_for_user


class ManagedBackupOut(BaseModel):
    """Safe archive state without storage location or key material."""

    id: str
    status: str
    size_bytes: int | None
    created_at: str
    completed_at: str | None
    last_error: str | None


class ManagedBackupJobOut(BaseModel):
    """Safe maintenance progress without worker or provider ownership."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(validation_alias=AliasChoices("id", "job_id"))
    archive_id: str
    operation: str
    status: str
    phase: str
    started_at: str
    updated_at: str
    last_error: str | None


class ManagedRuntimeOut(BaseModel):
    """Public runtime state without provider authority or tenant identity."""

    provider: Literal["local", "fly_sprites"]
    status: str
    artifact_version: str | None = None
    last_error: str | None = None
    runner_public_key: str | None = None


router = APIRouter(tags=["runtime"])


def _safe_runtime_status(
    user_id: str,
    runtime: ManagedRuntimeStatus | None,
) -> ManagedRuntimeOut:
    """Convert persisted state to fixed public response fields."""
    if runtime is None:
        return ManagedRuntimeOut(provider="fly_sprites", status="absent")
    runner = get_managed_runner_for_user(user_id)
    runner_public_key = None
    if (
        runner is not None
        and runner.get("id") == runtime.runner_id
        and runner.get("kind") == "managed"
        and runner.get("cloud_provider") == runtime.provider_name
        and runner.get("noise_key_confirmed")
    ):
        candidate = runner.get("noise_public_key")
        if isinstance(candidate, str) and candidate:
            runner_public_key = candidate
    return ManagedRuntimeOut(
        provider=runtime.provider_name,
        status=runtime.lifecycle_status,
        artifact_version=runtime.artifact_version,
        last_error=runtime.last_error,
        runner_public_key=runner_public_key,
    )


@router.get("/api/runtime/backups", response_model=list[ManagedBackupOut])
def list_runtime_backups(request: Request) -> list[ManagedBackupOut]:
    """Return bounded safe archive states owned by the authenticated tenant."""
    tenant = require_tenant(request)
    return [
        ManagedBackupOut(
            id=archive.id,
            status=archive.status,
            size_bytes=archive.size_bytes,
            created_at=archive.created_at,
            completed_at=archive.completed_at,
            last_error=archive.last_error,
        )
        for archive in list_managed_backup_archives(tenant.user_id)
    ]


@router.post(
    "/api/runtime/backups",
    response_model=ManagedBackupJobOut,
    status_code=202,
)
@limiter.limit("5/hour")
def create_runtime_backup(request: Request) -> ManagedBackupJobOut:
    """Queue one encrypted managed backup for the authenticated tenant."""
    tenant = require_tenant(request)
    manager = getattr(request.app.state, "managed_backup_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Managed backups are unavailable")
    try:
        job = manager.enqueue_create(tenant.user_id)
    except (ManagedBackupConflictError, ValueError) as error:
        raise HTTPException(status_code=409, detail="Managed backup state is invalid") from error
    manager.wake()
    return ManagedBackupJobOut.model_validate(job)


def _queue_backup_mutation(
    request: Request,
    archive_id: str,
    method_name: str,
) -> ManagedBackupJobOut:
    tenant = require_tenant(request)
    manager = getattr(request.app.state, "managed_backup_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Managed backups are unavailable")
    try:
        job = getattr(manager, method_name)(tenant.user_id, archive_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail="Managed backup was not found") from error
    except (ManagedBackupConflictError, ValueError) as error:
        raise HTTPException(status_code=409, detail="Managed backup state is invalid") from error
    manager.wake()
    return ManagedBackupJobOut.model_validate(job)


@router.post(
    "/api/runtime/backups/{archive_id}/restore",
    response_model=ManagedBackupJobOut,
    status_code=202,
)
@limiter.limit("3/hour")
def restore_runtime_backup(archive_id: str, request: Request) -> ManagedBackupJobOut:
    """Queue one tenant-owned replacement restore."""
    return _queue_backup_mutation(request, archive_id, "enqueue_restore")


@router.delete(
    "/api/runtime/backups/{archive_id}",
    response_model=ManagedBackupJobOut,
    status_code=202,
)
@limiter.limit("5/hour")
def delete_runtime_backup(archive_id: str, request: Request) -> ManagedBackupJobOut:
    """Queue one tenant-owned exact-version archive deletion."""
    return _queue_backup_mutation(request, archive_id, "enqueue_delete")


@router.get(
    "/api/runtime/backup-jobs/{job_id}",
    response_model=ManagedBackupJobOut,
)
def get_runtime_backup_job(job_id: str, request: Request) -> ManagedBackupJobOut:
    """Return safe progress for one tenant-owned managed maintenance job."""
    tenant = require_tenant(request)
    operation = get_managed_backup_operation(tenant.user_id, job_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Managed backup job was not found")
    return ManagedBackupJobOut(
        id=operation.job_id,
        archive_id=operation.archive_id,
        operation=operation.operation,
        status=operation.status,
        phase=operation.phase,
        started_at=operation.started_at,
        updated_at=operation.updated_at,
        last_error=operation.last_error,
    )


@router.get("/api/runtime", response_model=ManagedRuntimeOut)
def get_runtime(request: Request) -> ManagedRuntimeOut:
    """Return the authenticated tenant's safe runtime state."""
    tenant = require_tenant(request)
    manager = getattr(request.app.state, "managed_runtime_manager", None)
    if manager is None:
        return ManagedRuntimeOut(provider="local", status="ready")
    return _safe_runtime_status(
        tenant.user_id,
        get_managed_runtime_status(tenant.user_id),
    )


@router.post("/api/runtime/provision", response_model=ManagedRuntimeOut)
@limiter.limit("10/minute")
async def provision_runtime(request: Request) -> ManagedRuntimeOut:
    """Provision the tenant's managed runtime and return safe state."""
    tenant = require_tenant(request)
    public_launch_enabled = getattr(
        request.app.state,
        "sprites_public_launch_enabled",
        False,
    )
    if public_launch_enabled is not True:
        raise HTTPException(
            status_code=503,
            detail="Managed runtime public launch is disabled",
        )
    manager = getattr(request.app.state, "managed_runtime_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Managed runtime is unavailable")
    try:
        runtime = await manager.provision(tenant.user_id)
    except ManagedRuntimeStateError as error:
        raise HTTPException(
            status_code=409,
            detail="Managed runtime state is invalid",
        ) from error
    except ManagedRuntimeIdentityError as error:
        raise HTTPException(
            status_code=409,
            detail="Managed runtime identity changed",
        ) from error
    except ManagedRuntimeProviderError as error:
        raise HTTPException(
            status_code=503,
            detail="Managed runtime provider unavailable",
        ) from error
    except ManagedRuntimeTimeoutError as error:
        raise HTTPException(
            status_code=503,
            detail="Managed runtime wake timed out",
        ) from error
    return _safe_runtime_status(tenant.user_id, runtime)


@router.post(
    "/api/runtime/capabilities",
    response_model=RunnerCapabilityOut,
    status_code=201,
)
@limiter.limit("60/minute")
async def issue_managed_runtime_capability(
    body: RunnerCapabilityCreateIn,
    request: Request,
) -> RunnerCapabilityOut:
    """Wake and revalidate one managed runner before signing authority."""
    tenant = require_tenant(request)
    manager = getattr(request.app.state, "managed_runtime_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Managed runtime is unavailable")
    try:
        runner = await manager.ensure_online(tenant.user_id)
    except ManagedRuntimeStateError as error:
        raise HTTPException(status_code=409, detail="Managed runtime state is invalid") from error
    except ManagedRuntimeIdentityError as error:
        raise HTTPException(status_code=409, detail="Managed runtime identity changed") from error
    except ManagedRuntimeProviderError as error:
        raise HTTPException(
            status_code=503,
            detail="Managed runtime provider unavailable",
        ) from error
    except ManagedRuntimeTimeoutError as error:
        raise HTTPException(status_code=503, detail="Managed runtime wake timed out") from error

    try:
        capability, claims = create_runner_capability(
            user_id=tenant.user_id,
            runner_id=runner.runner_id,
            runner_public_key=runner.runner_public_key,
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
