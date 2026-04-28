"""Shared tenant sidecar runtime resolution for containerized execution."""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Request

from yinshi.config import get_settings
from yinshi.exceptions import ContainerStartError
from yinshi.services.container import ContainerMount
from yinshi.services.pi_config import resolve_effective_pi_runtime
from yinshi.tenant import TenantContext
from yinshi.utils.paths import is_path_inside


@dataclass(frozen=True, slots=True)
class TenantSidecarContext:
    """Resolved sidecar runtime inputs for one tenant-scoped request."""

    socket_path: str | None
    agent_dir: str | None
    settings_payload: dict[str, object] | None
    runtime_id: str | None = None
    pi_session_file: str | None = None


_RUNTIME_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_WORKSPACE_HOME_TARGET = "/home/yinshi"
_PI_SESSION_DIRECTORY = ".yinshi/pi-sessions"
_WORKSPACE_PATH = (
    f"{_WORKSPACE_HOME_TARGET}/bin:"
    f"{_WORKSPACE_HOME_TARGET}/.local/bin:"
    f"{_WORKSPACE_HOME_TARGET}/.npm-global/bin:"
    "/app/node_modules/.bin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)


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


def _runtime_safe_id(value: str | None, name: str) -> str | None:
    """Return a stable 32-character hex id for one runtime-owned path segment."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string or None")
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"{name} must not be empty when provided")
    if _RUNTIME_ID_RE.match(normalized_value):
        return normalized_value
    return hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()[:32]


def _workspace_runtime_id(workspace_id: str | None) -> str | None:
    """Return stable container id for one workspace, or None for legacy runtime."""
    return _runtime_safe_id(workspace_id, "workspace_id")


def _pi_session_file_name(session_id: str) -> str:
    """Return path-safe Pi session file name for one Yinshi session id."""
    normalized_session_id = _runtime_safe_id(session_id, "session_id")
    assert normalized_session_id is not None, "session_id must be normalized"
    return f"{normalized_session_id}.jsonl"


def _workspace_pi_session_host_file(
    tenant: TenantContext,
    workspace_id: str,
    session_id: str,
) -> str:
    """Return host path for a workspace-scoped durable Pi session file."""
    if tenant is None:
        raise ValueError("tenant is required")
    if not tenant.data_dir:
        raise ValueError("tenant data_dir must not be empty")
    home_path = _workspace_home_source(tenant, workspace_id)
    session_directory = os.path.join(home_path, *_PI_SESSION_DIRECTORY.split("/"))
    os.makedirs(session_directory, mode=0o700, exist_ok=True)
    return os.path.join(session_directory, _pi_session_file_name(session_id))


def _workspace_pi_session_runtime_file(
    tenant: TenantContext,
    workspace_id: str,
    session_id: str,
    *,
    container_enabled: bool,
    narrow_mounts: bool,
) -> str:
    """Return the path a sidecar process should use for Pi session persistence."""
    host_file = _workspace_pi_session_host_file(tenant, workspace_id, session_id)
    if not container_enabled:
        return host_file
    if narrow_mounts:
        return posixpath.join(
            _WORKSPACE_HOME_TARGET,
            _PI_SESSION_DIRECTORY,
            _pi_session_file_name(session_id),
        )
    return remap_path_for_container(host_file, tenant.data_dir)


def _local_pi_session_directory(*, create: bool) -> str:
    """Return the legacy no-tenant Pi session directory."""
    settings = get_settings()
    db_path = os.path.abspath(settings.db_path)
    if not db_path:
        raise ValueError("settings.db_path must not be empty")
    session_directory = os.path.join(os.path.dirname(db_path), "pi-sessions")
    if create:
        os.makedirs(session_directory, mode=0o700, exist_ok=True)
    return session_directory


def local_pi_session_file(session_id: str) -> str:
    """Return host path for durable Pi sessions in legacy no-tenant mode."""
    return os.path.join(
        _local_pi_session_directory(create=True),
        _pi_session_file_name(session_id),
    )


def delete_local_pi_session_file(session_id: str) -> None:
    """Delete one durable Pi session file in legacy no-tenant mode."""
    session_directory = _local_pi_session_directory(create=False)
    session_file = os.path.join(session_directory, _pi_session_file_name(session_id))
    if os.path.exists(session_file):
        os.unlink(session_file)


def delete_workspace_pi_sessions(tenant: TenantContext | None, workspace_id: str) -> None:
    """Delete durable Pi session files for one workspace runtime."""
    if tenant is None:
        return
    if not workspace_id:
        raise ValueError("workspace_id must not be empty")
    home_path = _workspace_home_path(tenant, workspace_id)
    session_directory = os.path.join(home_path, *_PI_SESSION_DIRECTORY.split("/"))
    if os.path.exists(session_directory):
        shutil.rmtree(session_directory)


def _workspace_home_path(tenant: TenantContext, workspace_id: str) -> str:
    """Return the persistent host home path for one workspace runtime."""
    normalized_workspace_id = _workspace_runtime_id(workspace_id)
    assert normalized_workspace_id is not None, "workspace_id must be normalized"
    return os.path.realpath(
        os.path.join(
            tenant.data_dir,
            "runtime",
            "workspaces",
            normalized_workspace_id,
            "home",
        )
    )


def _workspace_home_source(tenant: TenantContext, workspace_id: str) -> str:
    """Return and create the persistent host home for one workspace runtime."""
    home_path = _workspace_home_path(tenant, workspace_id)
    os.makedirs(home_path, mode=0o700, exist_ok=True)
    for subdirectory in ("bin", ".local/bin", ".npm-global/bin"):
        os.makedirs(os.path.join(home_path, subdirectory), mode=0o700, exist_ok=True)
    return home_path


def workspace_runtime_environment(workspace_id: str | None) -> dict[str, str] | None:
    """Return container environment variables for a workspace runtime."""
    normalized_workspace_id = _workspace_runtime_id(workspace_id)
    if normalized_workspace_id is None:
        return None
    return {
        "HOME": _WORKSPACE_HOME_TARGET,
        "PATH": _WORKSPACE_PATH,
        "NPM_CONFIG_PREFIX": f"{_WORKSPACE_HOME_TARGET}/.npm-global",
        "PIPX_HOME": f"{_WORKSPACE_HOME_TARGET}/.local/pipx",
        "PIPX_BIN_DIR": f"{_WORKSPACE_HOME_TARGET}/.local/bin",
        "YINSHI_WORKSPACE_ID": normalized_workspace_id,
    }


def _append_runtime_mount(
    mounts: list[ContainerMount],
    mounts_by_target: dict[str, ContainerMount],
    *,
    source_path: str,
    target_path: str,
    read_only: bool,
) -> None:
    """Append one mount while preventing ambiguous target overlays."""
    if not os.path.isabs(source_path):
        raise ValueError("source_path must be absolute")
    if not os.path.isabs(target_path):
        raise ValueError("target_path must be absolute")

    mount = ContainerMount(
        source_path=source_path,
        target_path=target_path,
        read_only=read_only,
    )
    existing_mount = mounts_by_target.get(target_path)
    if existing_mount is not None:
        if existing_mount == mount:
            return
        raise ContainerStartError("Sidecar mount targets must be unique")

    mounts.append(mount)
    mounts_by_target[target_path] = mount


def _container_mounts_for_runtime(
    tenant: TenantContext,
    *,
    agent_dir: str | None,
    repo_root_path: str | None,
    workspace_path: str | None,
    workspace_id: str | None,
) -> tuple[ContainerMount, ...]:
    """Build the narrow mount set required by one sidecar operation."""
    mounts: list[ContainerMount] = []
    mounts_by_target: dict[str, ContainerMount] = {}
    normalized_workspace_id = _workspace_runtime_id(workspace_id)
    if normalized_workspace_id is not None:
        _append_runtime_mount(
            mounts,
            mounts_by_target,
            source_path=_workspace_home_source(tenant, normalized_workspace_id),
            target_path=_WORKSPACE_HOME_TARGET,
            read_only=False,
        )

    for source_path, read_only, mount_at_host_path in (
        (repo_root_path, False, True),
        (workspace_path, False, False),
        (agent_dir, True, False),
    ):
        if source_path is None:
            continue
        normalized_source_path = os.path.realpath(source_path)
        if not is_path_inside(normalized_source_path, tenant.data_dir):
            message = "Sidecar mount path is outside tenant data"
            raise ContainerStartError(message)
        _append_runtime_mount(
            mounts,
            mounts_by_target,
            source_path=normalized_source_path,
            target_path=remap_path_for_container(
                normalized_source_path,
                tenant.data_dir,
            ),
            read_only=read_only,
        )
        if mount_at_host_path:
            # Git worktrees store absolute gitdir pointers into the repo's
            # metadata. Mounting the repo at that same absolute path lets Git
            # resolve those pointers inside the sidecar while cwd stays in
            # /data.
            _append_runtime_mount(
                mounts,
                mounts_by_target,
                source_path=normalized_source_path,
                target_path=normalized_source_path,
                read_only=read_only,
            )
    return tuple(mounts)


async def resolve_tenant_sidecar_context(
    request: Request,
    tenant: TenantContext | None,
    runtime_session_id: str | None = None,
    repo_agents_md: str | None = None,
    repo_root_path: str | None = None,
    workspace_path: str | None = None,
    workspace_id: str | None = None,
) -> TenantSidecarContext:
    """Resolve the socket path and Pi runtime inputs for one request."""
    if tenant is None:
        return TenantSidecarContext(
            socket_path=None,
            agent_dir=None,
            settings_payload=None,
        )

    settings = get_settings()
    runtime_inputs = resolve_effective_pi_runtime(
        tenant.user_id,
        tenant.data_dir,
        runtime_session_id=runtime_session_id,
        repo_agents_md=repo_agents_md,
    )
    narrow_mounts = settings.container_mount_mode == "narrow"
    runtime_id = _workspace_runtime_id(workspace_id)
    pi_session_file = None
    if runtime_session_id is not None and workspace_id is not None:
        pi_session_file = _workspace_pi_session_runtime_file(
            tenant,
            workspace_id,
            runtime_session_id,
            container_enabled=settings.container_enabled,
            narrow_mounts=narrow_mounts,
        )
    runtime_agent_dir = _resolve_agent_dir_for_runtime(
        runtime_inputs.agent_dir,
        tenant.data_dir,
        container_enabled=settings.container_enabled,
    )

    if not settings.container_enabled:
        return TenantSidecarContext(
            socket_path=None,
            agent_dir=runtime_agent_dir,
            settings_payload=runtime_inputs.settings_payload,
            runtime_id=runtime_id,
            pi_session_file=pi_session_file,
        )

    container_manager = getattr(request.app.state, "container_manager", None)
    if container_manager is None:
        raise ContainerStartError("Container manager is not initialized")

    container_mounts = None
    if narrow_mounts:
        container_mounts = _container_mounts_for_runtime(
            tenant,
            agent_dir=runtime_inputs.agent_dir,
            repo_root_path=repo_root_path,
            workspace_path=workspace_path,
            workspace_id=runtime_id,
        )
    container_info = await container_manager.ensure_container(
        tenant.user_id,
        tenant.data_dir,
        mounts=container_mounts,
        runtime_id=runtime_id,
        environment=workspace_runtime_environment(runtime_id),
    )
    return TenantSidecarContext(
        socket_path=container_info.socket_path,
        agent_dir=runtime_agent_dir,
        settings_payload=runtime_inputs.settings_payload,
        runtime_id=runtime_id,
        pi_session_file=pi_session_file,
    )


def _call_container_method(
    method: object,
    user_id: str,
    *args: object,
    runtime_id: str | None = None,
) -> None:
    """Call a container manager lifecycle method for one runtime."""
    if not callable(method):
        return
    method(user_id, *args, runtime_id=runtime_id)


@asynccontextmanager
async def tenant_container_activity(
    request: Request,
    tenant: TenantContext | None,
    *,
    runtime_id: str | None = None,
    protect_lease_key: str | None = None,
    protect_timeout_s: int | None = None,
) -> AsyncIterator[None]:
    """Mark a container busy, then optionally keep it alive after work ends."""
    if protect_lease_key is not None and protect_timeout_s is None:
        raise ValueError("protect_timeout_s is required when protect_lease_key is set")
    if protect_timeout_s is not None and protect_timeout_s < 0:
        raise ValueError("protect_timeout_s must not be negative")

    begin_tenant_container_activity(request, tenant, runtime_id=runtime_id)
    try:
        yield
    finally:
        end_tenant_container_activity(request, tenant, runtime_id=runtime_id)
        if protect_lease_key is not None:
            assert protect_timeout_s is not None, "protect timeout must be validated"
            protect_tenant_container(
                request,
                tenant,
                lease_key=protect_lease_key,
                timeout_s=protect_timeout_s,
                runtime_id=runtime_id,
            )


def touch_tenant_container(
    request: Request,
    tenant: TenantContext | None,
    *,
    runtime_id: str | None = None,
) -> None:
    """Mark one tenant container as recently used when container mode is active."""
    container_manager, user_id = _tenant_container_manager(request, tenant)
    if container_manager is None or user_id is None:
        return
    _call_container_method(
        getattr(container_manager, "touch", None), user_id, runtime_id=runtime_id
    )


def begin_tenant_container_activity(
    request: Request,
    tenant: TenantContext | None,
    *,
    runtime_id: str | None = None,
) -> None:
    """Mark one tenant container as busy for the duration of a request step."""
    container_manager, user_id = _tenant_container_manager(request, tenant)
    if container_manager is None or user_id is None:
        return
    _call_container_method(
        getattr(container_manager, "begin_activity", None),
        user_id,
        runtime_id=runtime_id,
    )


def end_tenant_container_activity(
    request: Request,
    tenant: TenantContext | None,
    *,
    runtime_id: str | None = None,
) -> None:
    """Release one in-flight request marker from a tenant container."""
    container_manager, user_id = _tenant_container_manager(request, tenant)
    if container_manager is None or user_id is None:
        return
    _call_container_method(
        getattr(container_manager, "end_activity", None),
        user_id,
        runtime_id=runtime_id,
    )


def protect_tenant_container(
    request: Request,
    tenant: TenantContext | None,
    *,
    lease_key: str,
    timeout_s: int,
    runtime_id: str | None = None,
) -> None:
    """Keep one tenant container alive for a named long-lived operation."""
    container_manager, user_id = _tenant_container_manager(request, tenant)
    if container_manager is None or user_id is None:
        return
    _call_container_method(
        getattr(container_manager, "protect", None),
        user_id,
        lease_key,
        timeout_s,
        runtime_id=runtime_id,
    )


def release_tenant_container(
    request: Request,
    tenant: TenantContext | None,
    *,
    lease_key: str,
    runtime_id: str | None = None,
) -> None:
    """Remove one named long-lived keepalive lease from a tenant container."""
    container_manager, user_id = _tenant_container_manager(request, tenant)
    if container_manager is None or user_id is None:
        return
    _call_container_method(
        getattr(container_manager, "unprotect", None),
        user_id,
        lease_key,
        runtime_id=runtime_id,
    )


def _tenant_container_manager(
    request: Request,
    tenant: TenantContext | None,
) -> tuple[object | None, str | None]:
    """Return the container manager plus tenant user id when container mode is active."""
    if tenant is None:
        return None, None

    settings = get_settings()
    if not settings.container_enabled:
        return None, None

    container_manager = getattr(request.app.state, "container_manager", None)
    if container_manager is None:
        return None, None
    return container_manager, tenant.user_id
