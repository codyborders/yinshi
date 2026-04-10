"""Shared tenant sidecar runtime resolution for containerized execution."""

from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import Request

from yinshi.config import get_settings
from yinshi.exceptions import ContainerStartError
from yinshi.services.pi_config import resolve_pi_runtime
from yinshi.tenant import TenantContext
from yinshi.utils.paths import is_path_inside


@dataclass(frozen=True, slots=True)
class TenantSidecarContext:
    """Resolved sidecar runtime inputs for one tenant-scoped request."""

    socket_path: str | None
    agent_dir: str | None
    settings_payload: dict[str, object] | None


def remap_path_for_container(
    host_path: str,
    data_dir: str,
    *,
    mount_path: str = "/data",
) -> str:
    """Translate a host path into the tenant container mount namespace."""
    if not isinstance(host_path, str):
        raise TypeError("host_path must be a string")
    normalized_host_path = host_path.strip()
    if not normalized_host_path:
        raise ValueError("host_path must not be empty")
    if not isinstance(data_dir, str):
        raise TypeError("data_dir must be a string")
    normalized_data_dir = data_dir.strip()
    if not normalized_data_dir:
        raise ValueError("data_dir must not be empty")
    if not isinstance(mount_path, str):
        raise TypeError("mount_path must be a string")
    normalized_mount_path = mount_path.strip()
    if not normalized_mount_path:
        raise ValueError("mount_path must not be empty")

    if not is_path_inside(normalized_host_path, normalized_data_dir):
        raise ValueError("Path outside user data directory")

    resolved_host_path = os.path.realpath(normalized_host_path)
    resolved_data_dir = os.path.realpath(normalized_data_dir)
    if resolved_host_path == resolved_data_dir:
        return normalized_mount_path

    relative_path = os.path.relpath(resolved_host_path, resolved_data_dir)
    return os.path.join(normalized_mount_path, relative_path)


def _resolve_agent_dir_for_runtime(
    agent_dir: str | None,
    data_dir: str,
    *,
    container_enabled: bool,
) -> str | None:
    """Return the runtime-visible agent dir for the current execution mode."""
    if agent_dir is None:
        return None
    if not isinstance(agent_dir, str):
        raise TypeError("agent_dir must be a string or None")
    normalized_agent_dir = agent_dir.strip()
    if not normalized_agent_dir:
        raise ValueError("agent_dir must not be empty when provided")

    if not container_enabled:
        return normalized_agent_dir
    if not is_path_inside(normalized_agent_dir, data_dir):
        return normalized_agent_dir
    return remap_path_for_container(normalized_agent_dir, data_dir)


async def resolve_tenant_sidecar_context(
    request: Request,
    tenant: TenantContext | None,
) -> TenantSidecarContext:
    """Resolve the socket path and Pi runtime inputs for one request."""
    if tenant is None:
        return TenantSidecarContext(
            socket_path=None,
            agent_dir=None,
            settings_payload=None,
        )

    settings = get_settings()
    host_agent_dir, settings_payload = resolve_pi_runtime(tenant.user_id, tenant.data_dir)
    runtime_agent_dir = _resolve_agent_dir_for_runtime(
        host_agent_dir,
        tenant.data_dir,
        container_enabled=settings.container_enabled,
    )

    if not settings.container_enabled:
        return TenantSidecarContext(
            socket_path=None,
            agent_dir=runtime_agent_dir,
            settings_payload=settings_payload,
        )

    container_manager = getattr(request.app.state, "container_manager", None)
    if container_manager is None:
        raise ContainerStartError("Container manager is not initialized")

    container_info = await container_manager.ensure_container(tenant.user_id, tenant.data_dir)
    return TenantSidecarContext(
        socket_path=container_info.socket_path,
        agent_dir=runtime_agent_dir,
        settings_payload=settings_payload,
    )


def touch_tenant_container(request: Request, tenant: TenantContext | None) -> None:
    """Mark one tenant container as recently used when container mode is active."""
    if tenant is None:
        return

    settings = get_settings()
    if not settings.container_enabled:
        return

    container_manager = getattr(request.app.state, "container_manager", None)
    if container_manager is None:
        return
    container_manager.touch(tenant.user_id)
