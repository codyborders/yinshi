"""Per-user Podman container management for sidecar isolation."""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import IO, TYPE_CHECKING, Any

from yinshi.exceptions import ContainerNotReadyError, ContainerStartError

logger = logging.getLogger(__name__)

_SIDECAR_NET = "yinshi-sidecar-net"
_PODMAN_RUN_TIMEOUT_S = 90.0
_USER_ID_RE = re.compile(r"^[0-9a-f]{32}$")

if TYPE_CHECKING:
    from yinshi.config import Settings


@dataclass(frozen=True)
class ContainerMount:
    """One host path exposed to a sidecar container."""

    source_path: str
    target_path: str
    read_only: bool = False


@dataclass
class ContainerInfo:
    """Tracks one sidecar container runtime."""

    container_id: str
    user_id: str
    socket_path: str
    mounts: tuple[ContainerMount, ...] = field(default_factory=tuple)
    runtime_id: str | None = None
    environment: tuple[tuple[str, str], ...] = field(default_factory=lambda: (("HOME", "/tmp"),))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    active_request_count: int = 0
    protected_operation_deadlines: dict[str, datetime] = field(default_factory=dict)


class ContainerManager:
    """Manages per-user Podman containers for sidecar isolation.

    Each user gets a dedicated container with only the paths required for the
    current sidecar operation mounted. Containers are reaped after an idle timeout.
    """

    def __init__(
        self,
        settings: "Settings",  # noqa: F821 -- forward ref avoids circular import
        podman_binary: str = "podman",
    ) -> None:
        self._settings = settings
        self._podman_bin = podman_binary
        self._containers: dict[str, ContainerInfo] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._initialization_lock = asyncio.Lock()
        self._socket_poll_timeout_s: float = 10.0
        self._socket_poll_interval_s: float = 0.1
        self._initialized = False

    @staticmethod
    def _now() -> datetime:
        """Return the current UTC timestamp."""
        return datetime.now(timezone.utc)

    # -- Podman subprocess helper -------------------------------------------

    async def _run_podman(
        self,
        *args: str,
        check: bool = True,
        timeout: float = 30.0,
    ) -> tuple[int, str, str]:
        """Execute a podman command and return (returncode, stdout, stderr).

        Raises ``ContainerStartError`` when *check* is True and the
        command exits non-zero, or when the binary is missing / times out.
        """
        proc = await self._start_podman_process(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._stop_process(proc)
            raise ContainerStartError(
                f"Podman command timed out: podman {' '.join(args)}"
            ) from None

        return self._checked_podman_result(
            args,
            proc.returncode,
            self._decode_process_output(stdout_data),
            self._decode_process_output(stderr_data),
            check=check,
        )

    async def _run_podman_waiting_for_exit(
        self,
        *args: str,
        check: bool = True,
        timeout: float = 30.0,
    ) -> tuple[int, str, str]:
        """Execute Podman and wait on process exit instead of pipe EOF.

        Detached ``podman run`` can leave the captured pipes open in the
        spawned container monitor on some rootless runtimes. Waiting for the
        Podman process exit avoids turning a successful detached start into a
        false timeout while still preserving stdout and stderr for failures.
        """
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            proc = await self._start_podman_process(
                *args,
                stdout=stdout_file,
                stderr=stderr_file,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._stop_process(proc)
                raise ContainerStartError(
                    f"Podman command timed out: podman {' '.join(args)}"
                ) from None

            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = self._decode_process_output(stdout_file.read())
            stderr = self._decode_process_output(stderr_file.read())

        return self._checked_podman_result(args, proc.returncode, stdout, stderr, check=check)

    async def _start_podman_process(
        self,
        *args: str,
        stdout: int | IO[Any] | None,
        stderr: int | IO[Any] | None,
    ) -> asyncio.subprocess.Process:
        """Start one Podman subprocess with explicit output destinations."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._podman_bin,
                *args,
                stdout=stdout,
                stderr=stderr,
            )
        except FileNotFoundError:
            raise ContainerStartError("podman binary not found") from None

        assert proc is not None, "create_subprocess_exec must return a process"
        return proc

    def _checked_podman_result(
        self,
        args: tuple[str, ...],
        returncode: int | None,
        stdout: str,
        stderr: str,
        *,
        check: bool,
    ) -> tuple[int, str, str]:
        """Validate one completed Podman process result."""
        if returncode is None:
            raise ContainerStartError("Podman process exited without a return code")

        if check:
            if returncode != 0:
                raise ContainerStartError(f"podman {args[0]} failed: {stderr}")

        return returncode, stdout, stderr

    @staticmethod
    def _decode_process_output(raw_data: bytes | str | None) -> str:
        """Decode subprocess output without truncating Podman's JSON responses."""
        if raw_data is None:
            return ""
        if isinstance(raw_data, str):
            return raw_data.strip()
        if not raw_data:
            return ""
        return raw_data.decode(errors="replace").strip()

    async def _stop_process(
        self,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """Terminate one subprocess while tolerating races with an already-exited process."""
        if proc.returncode is not None:
            return

        try:
            proc.kill()
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for Podman process shutdown")

    # -- Initialization -----------------------------------------------------

    async def initialize(self) -> None:
        """Create the Podman network and clean up orphaned containers.

        Idempotent -- safe to call more than once.  Called automatically
        on the first ``ensure_container`` invocation.
        """
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            self._ensure_socket_base_dir()
            await self._verify_podman_available()
            await self._ensure_network()
            await self._ensure_image()
            await self._cleanup_orphaned_containers()
            self._initialized = True

    # -- Podman network -----------------------------------------------------

    def _ensure_socket_base_dir(self) -> None:
        """Create the shared socket base directory with restricted permissions."""
        socket_base_dir = self._settings.container_socket_base
        if not isinstance(socket_base_dir, str):
            raise TypeError("container_socket_base must be a string")
        normalized_socket_base_dir = socket_base_dir.strip()
        if not normalized_socket_base_dir:
            raise ValueError("container_socket_base must not be empty")

        try:
            if os.path.exists(normalized_socket_base_dir):
                if not os.path.isdir(normalized_socket_base_dir):
                    raise ContainerStartError("container socket base path must be a directory")
            else:
                os.makedirs(normalized_socket_base_dir, mode=0o700, exist_ok=True)
            os.chmod(normalized_socket_base_dir, 0o700)
        except OSError as exc:
            raise ContainerStartError(
                f"Failed to prepare container socket base: {normalized_socket_base_dir}"
            ) from exc

    async def _verify_podman_available(self) -> None:
        """Fail fast when the Podman CLI is missing or unhealthy."""
        await self._run_podman("--version")

    async def _ensure_network(self) -> None:
        """Create the tenant network and repair old internal-only variants."""
        rc, stdout, _ = await self._run_podman(
            "network",
            "inspect",
            _SIDECAR_NET,
            check=False,
        )
        if rc != 0:
            await self._create_network()
            return

        if self._network_is_internal(stdout):
            await self._run_podman("network", "rm", _SIDECAR_NET)
            await self._create_network()
            logger.info("Recreated Podman network %s without internal isolation", _SIDECAR_NET)

    async def _create_network(self) -> None:
        """Create one Podman network with outbound access for model providers."""
        await self._run_podman(
            "network",
            "create",
            _SIDECAR_NET,
        )
        logger.info("Created Podman network %s", _SIDECAR_NET)

    def _network_is_internal(self, inspect_output: str) -> bool:
        """Return whether one inspected Podman network blocks outbound traffic."""
        if not isinstance(inspect_output, str):
            raise TypeError("inspect_output must be a string")
        normalized_inspect_output = inspect_output.strip()
        if not normalized_inspect_output:
            return False

        try:
            parsed_output = json.loads(normalized_inspect_output)
        except json.JSONDecodeError as exc:
            raise ContainerStartError("Podman network inspect returned invalid JSON") from exc
        if not isinstance(parsed_output, list):
            raise ContainerStartError("Podman network inspect returned an invalid payload")
        if not parsed_output:
            return False

        network_info = parsed_output[0]
        if not isinstance(network_info, dict):
            raise ContainerStartError("Podman network inspect returned a non-object network")

        internal_value = network_info.get("internal")
        if isinstance(internal_value, bool):
            return internal_value
        return False

    async def _ensure_image(self) -> None:
        """Require the configured sidecar image to be present locally."""
        image_name = self._settings.container_image
        if not isinstance(image_name, str):
            raise TypeError("container_image must be a string")
        normalized_image_name = image_name.strip()
        if not normalized_image_name:
            raise ValueError("container_image must not be empty")

        rc, _, _ = await self._run_podman(
            "image",
            "exists",
            normalized_image_name,
            check=False,
        )
        if rc != 0:
            raise ContainerStartError(
                f"Configured sidecar image is not available locally: {normalized_image_name}"
            )

    # -- Orphan cleanup -----------------------------------------------------

    async def _cleanup_orphaned_containers(self) -> None:
        """Remove containers left over from a previous process crash."""
        try:
            rc, stdout, _ = await self._run_podman(
                "ps",
                "-a",
                "--filter",
                "label=yinshi.user_id",
                "--format",
                "json",
                check=False,
            )
            if rc != 0 or not stdout:
                return
            containers = json.loads(stdout)
            for c in containers:
                cid = c.get("Id", c.get("id", ""))
                if cid:
                    await self._run_podman("rm", "-f", cid, check=False)
                    logger.info("Removed orphaned container %s", cid[:12])
        except (json.JSONDecodeError, ContainerStartError):
            logger.warning("Failed to clean up orphaned containers", exc_info=True)

    # -- Per-user locks -----------------------------------------------------

    async def _get_lock(self, user_id: str) -> asyncio.Lock:
        """Get or create a per-user lock."""
        async with self._global_lock:
            if user_id not in self._locks:
                self._locks[user_id] = asyncio.Lock()
            return self._locks[user_id]

    def _container_key(self, user_id: str, runtime_id: str | None = None) -> str:
        """Return the in-memory key for one user or workspace runtime."""
        if not isinstance(user_id, str):
            raise TypeError("user_id must be a string")
        normalized_user_id = user_id.strip()
        if not normalized_user_id:
            raise ValueError("user_id must not be empty")
        if runtime_id is None:
            return normalized_user_id
        if not isinstance(runtime_id, str):
            raise TypeError("runtime_id must be a string or None")
        normalized_runtime_id = runtime_id.strip()
        if not normalized_runtime_id:
            raise ValueError("runtime_id must not be empty when provided")
        if not re.match(r"^[0-9a-f]{32}$", normalized_runtime_id):
            raise ValueError(f"Invalid runtime_id format: {runtime_id!r}")
        return f"{normalized_user_id}:{normalized_runtime_id}"

    def _socket_dir(self, user_id: str, runtime_id: str | None = None) -> str:
        """Return the host socket directory for one sidecar runtime."""
        if runtime_id is None:
            return os.path.join(self._settings.container_socket_base, user_id)
        return os.path.join(self._settings.container_socket_base, user_id, runtime_id)

    def _container_name(self, user_id: str, runtime_id: str | None = None) -> str:
        """Return a Podman-safe, stable container name for one runtime."""
        if runtime_id is None:
            return f"yinshi-sidecar-{user_id}"
        return f"yinshi-sidecar-{user_id[:12]}-{runtime_id}"

    def _default_mounts(self, data_dir: str) -> tuple[ContainerMount, ...]:
        """Return the legacy tenant-data mount for compatibility paths."""
        real_data_dir = os.path.realpath(data_dir)
        return (ContainerMount(source_path=real_data_dir, target_path="/data", read_only=False),)

    def _normalize_mounts(
        self,
        data_dir: str,
        mounts: tuple[ContainerMount, ...] | None,
    ) -> tuple[ContainerMount, ...]:
        """Validate and normalize the host paths mounted into a container."""
        if mounts is None:
            return self._default_mounts(data_dir)
        real_data_dir = os.path.realpath(data_dir)
        normalized_mounts: list[ContainerMount] = []
        seen_targets: set[str] = set()
        for mount in mounts:
            if not isinstance(mount, ContainerMount):
                raise TypeError("mounts must contain ContainerMount values")
            source_path = os.path.realpath(mount.source_path)
            target_path = mount.target_path.strip()
            if not source_path.startswith(real_data_dir + os.sep):
                if source_path != real_data_dir:
                    raise ContainerStartError("container mount source must stay inside tenant data")
            if source_path == real_data_dir:
                raise ContainerStartError(
                    "narrow container mounts must not expose the tenant data root"
                )
            if not os.path.exists(source_path):
                raise ContainerStartError(f"container mount source does not exist: {source_path}")
            if not os.path.isabs(target_path):
                raise ContainerStartError("container mount target must be an absolute path")
            if target_path in seen_targets:
                raise ContainerStartError("container mount targets must be unique")
            seen_targets.add(target_path)
            normalized_mounts.append(
                ContainerMount(
                    source_path=source_path,
                    target_path=target_path,
                    read_only=mount.read_only,
                )
            )
        return tuple(sorted(normalized_mounts, key=lambda value: value.target_path))

    def _normalize_environment(
        self,
        environment: dict[str, str] | None,
    ) -> tuple[tuple[str, str], ...]:
        """Validate and normalize environment variables for one runtime."""
        if environment is None:
            return (("HOME", "/tmp"),)
        normalized_environment: list[tuple[str, str]] = []
        for key, value in environment.items():
            if not isinstance(key, str):
                raise TypeError("environment keys must be strings")
            if not isinstance(value, str):
                raise TypeError("environment values must be strings")
            normalized_key = key.strip()
            if not normalized_key:
                raise ValueError("environment keys must not be empty")
            if "=" in normalized_key or "\x00" in normalized_key:
                raise ValueError("environment keys must not contain '=' or NUL")
            if "\x00" in value:
                raise ValueError("environment values must not contain NUL")
            normalized_environment.append((normalized_key, value))
        return tuple(sorted(normalized_environment, key=lambda item: item[0]))

    def _container_has_busy_state(self, info: ContainerInfo) -> bool:
        """Return whether a container is unsafe to replace for mount changes."""
        if info.active_request_count > 0:
            return True
        return bool(info.protected_operation_deadlines)

    # -- Public API ---------------------------------------------------------

    async def ensure_container(
        self,
        user_id: str,
        data_dir: str,
        mounts: tuple[ContainerMount, ...] | None = None,
        *,
        runtime_id: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> ContainerInfo:
        """Get or create a sidecar container for a user or workspace runtime.

        Raises ``ValueError`` if *user_id* is not a valid 32-char hex string.
        Raises ``ContainerStartError`` if the max container limit is reached.
        """
        if not _USER_ID_RE.match(user_id):
            raise ValueError(f"Invalid user_id format: {user_id!r}")
        container_key = self._container_key(user_id, runtime_id)
        normalized_mounts = self._normalize_mounts(data_dir, mounts)
        normalized_environment = self._normalize_environment(environment)

        if not self._initialized:
            await self.initialize()

        lock = await self._get_lock(container_key)
        async with lock:
            existing = self._containers.get(container_key)
            if existing:
                if await self._is_running(existing.container_id):
                    if (
                        existing.mounts == normalized_mounts
                        and existing.environment == normalized_environment
                    ):
                        existing.last_activity = datetime.now(timezone.utc)
                        return existing
                    if self._container_has_busy_state(existing):
                        raise ContainerStartError(
                            "Existing sidecar container is busy with a different runtime configuration"
                        )
                    await self._remove_container(existing.container_id)
                    del self._containers[container_key]
                else:
                    await self._remove_container(existing.container_id)
                    del self._containers[container_key]

            await self._enforce_container_limit()
            return await self._create_container(
                user_id,
                normalized_mounts,
                runtime_id=runtime_id,
                environment=normalized_environment,
            )

    async def _enforce_container_limit(self) -> None:
        """Fail before creating a new container when the configured quota is full."""
        max_count = getattr(self._settings, "container_max_count", 0)
        if not max_count:
            return
        if len(self._containers) < max_count:
            return
        await self.reap_idle()
        if len(self._containers) >= max_count:
            raise ContainerStartError("Maximum container limit reached")

    def touch(self, user_id: str, *, runtime_id: str | None = None) -> None:
        """Update last activity timestamp for a runtime container."""
        info = self._containers.get(self._container_key(user_id, runtime_id))
        if info:
            info.last_activity = self._now()

    def begin_activity(self, user_id: str, *, runtime_id: str | None = None) -> None:
        """Mark a container as busy for the lifetime of one request."""
        container_key = self._container_key(user_id, runtime_id)
        info = self._containers.get(container_key)
        if info is None:
            logger.warning("Cannot mark activity for missing container: %s", container_key[:41])
            return
        info.active_request_count += 1
        info.last_activity = self._now()

    def end_activity(self, user_id: str, *, runtime_id: str | None = None) -> None:
        """Release one active request marker for a runtime container."""
        container_key = self._container_key(user_id, runtime_id)
        info = self._containers.get(container_key)
        if info is None:
            logger.warning("Cannot end activity for missing container: %s", container_key[:41])
            return
        if info.active_request_count == 0:
            logger.warning(
                "Cannot end activity for container %s without a matching begin",
                container_key[:41],
            )
            return
        info.active_request_count -= 1
        info.last_activity = self._now()

    def protect(
        self,
        user_id: str,
        lease_key: str,
        timeout_s: int,
        *,
        runtime_id: str | None = None,
    ) -> None:
        """Keep one container alive for a named long-lived operation."""
        container_key = self._container_key(user_id, runtime_id)
        if not isinstance(lease_key, str):
            raise TypeError("lease_key must be a string")
        normalized_lease_key = lease_key.strip()
        if not normalized_lease_key:
            raise ValueError("lease_key must not be empty")
        if not isinstance(timeout_s, int):
            raise TypeError("timeout_s must be an integer")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")

        info = self._containers.get(container_key)
        if info is None:
            logger.warning("Cannot protect missing container: %s", container_key[:41])
            return
        info.protected_operation_deadlines[normalized_lease_key] = self._now() + timedelta(
            seconds=timeout_s
        )
        info.last_activity = self._now()

    def unprotect(
        self,
        user_id: str,
        lease_key: str,
        *,
        runtime_id: str | None = None,
    ) -> None:
        """Remove one named long-lived operation lease from a runtime container."""
        container_key = self._container_key(user_id, runtime_id)
        if not isinstance(lease_key, str):
            raise TypeError("lease_key must be a string")
        normalized_lease_key = lease_key.strip()
        if not normalized_lease_key:
            raise ValueError("lease_key must not be empty")

        info = self._containers.get(container_key)
        if info is None:
            logger.warning("Cannot unprotect missing container: %s", container_key[:41])
            return
        info.protected_operation_deadlines.pop(normalized_lease_key, None)
        info.last_activity = self._now()

    async def destroy_container(self, user_id: str, *, runtime_id: str | None = None) -> None:
        """Stop and remove a user's runtime container."""
        container_key = self._container_key(user_id, runtime_id)
        info = self._containers.pop(container_key, None)
        self._locks.pop(container_key, None)
        if not info:
            return
        await self._remove_container(info.container_id)
        logger.info("Destroyed container runtime %s", container_key[:41])

    async def reap_idle(self) -> int:
        """Destroy containers that have been idle past the timeout."""
        timeout = self._settings.container_idle_timeout_s
        cutoff = self._now()
        idle_keys = [
            container_key
            for container_key, info in self._containers.items()
            if self._container_is_reapable(info, cutoff, timeout)
        ]
        for container_key in idle_keys:
            await self._destroy_container_by_key(container_key)
        return len(idle_keys)

    async def run_reaper(self) -> None:
        """Background task that periodically reaps idle containers."""
        while True:
            await asyncio.sleep(60)
            try:
                count = await self.reap_idle()
                if count:
                    logger.info("Reaped %d idle container(s)", count)
            except Exception:
                logger.exception("Error in container reaper")

    async def destroy_all(self) -> None:
        """Destroy all managed containers (shutdown hook)."""
        container_keys = list(self._containers.keys())
        for container_key in container_keys:
            await self._destroy_container_by_key(container_key)
        logger.info("All sidecar containers destroyed")

    async def _destroy_container_by_key(self, container_key: str) -> None:
        """Stop and remove one runtime container by its internal key."""
        info = self._containers.pop(container_key, None)
        self._locks.pop(container_key, None)
        if info is None:
            return
        await self._remove_container(info.container_id)
        logger.info("Destroyed container runtime %s", container_key[:41])

    # -- Internal helpers ---------------------------------------------------

    async def _is_running(self, container_id: str) -> bool:
        """Check if a container is still running."""
        rc, stdout, _ = await self._run_podman(
            "inspect",
            "--format",
            "{{.State.Status}}",
            container_id,
            check=False,
        )
        return rc == 0 and stdout == "running"

    async def _remove_container(self, container_id: str) -> None:
        """Force-remove a container."""
        rc, _, _ = await self._run_podman(
            "rm",
            "-f",
            container_id,
            check=False,
        )
        if rc == 0:
            logger.info("Removed container %s", container_id[:12])

    async def _create_container(
        self,
        user_id: str,
        mounts: tuple[ContainerMount, ...],
        *,
        runtime_id: str | None = None,
        environment: tuple[tuple[str, str], ...] = (),
    ) -> ContainerInfo:
        """Start a new sidecar container for a user or workspace runtime."""
        s = self._settings
        container_key = self._container_key(user_id, runtime_id)
        socket_dir = self._socket_dir(user_id, runtime_id)
        socket_path = os.path.join(socket_dir, "sidecar.sock")
        cidfile_path = os.path.join(socket_dir, "container.cid")
        self._prepare_socket_dir(socket_dir, socket_path)
        self._remove_stale_file(cidfile_path, "container cidfile")

        cpus = str(s.container_cpu_quota / 100000)
        mount_args: list[str] = []
        for mount in mounts:
            mode = "ro" if mount.read_only else "rw"
            mount_args.extend(["-v", f"{mount.source_path}:{mount.target_path}:{mode}"])
        runtime_uid = os.getuid()
        runtime_gid = os.getgid()
        env_args = ["--env", "SIDECAR_SOCKET_PATH=/run/sidecar/sidecar.sock"]
        for key, value in environment or (("HOME", "/tmp"),):
            env_args.extend(["--env", f"{key}={value}"])

        _, container_id, _ = await self._run_podman_waiting_for_exit(
            "run",
            "-d",
            "--replace",
            "--cidfile",
            cidfile_path,
            "--name",
            self._container_name(user_id, runtime_id),
            "--userns",
            "keep-id",
            "--user",
            f"{runtime_uid}:{runtime_gid}",
            *env_args,
            "-v",
            f"{socket_dir}:/run/sidecar:rw",
            *mount_args,
            "--network",
            _SIDECAR_NET,
            "--memory",
            s.container_memory_limit,
            "--memory-swap",
            s.container_memory_limit,
            "--cpus",
            cpus,
            "--pids-limit",
            str(s.container_pids_limit),
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--label",
            f"yinshi.user_id={user_id}",
            "--label",
            f"yinshi.runtime_id={runtime_id or ''}",
            s.container_image,
            timeout=_PODMAN_RUN_TIMEOUT_S,
        )
        container_id = self._resolve_created_container_id(container_id, cidfile_path)

        await self._wait_for_socket(socket_path)

        info = ContainerInfo(
            container_id=container_id,
            user_id=user_id,
            socket_path=socket_path,
            mounts=mounts,
            runtime_id=runtime_id,
            environment=environment or (("HOME", "/tmp"),),
        )
        self._containers[container_key] = info
        logger.info(
            "Started container %s for runtime %s",
            container_id[:12],
            container_key[:41],
        )
        return info

    async def _wait_for_socket(self, socket_path: str) -> None:
        """Poll until the sidecar accepts a connection and sends its init message."""
        deadline = time.monotonic() + self._socket_poll_timeout_s
        while time.monotonic() < deadline:
            reader: asyncio.StreamReader | None = None
            writer: asyncio.StreamWriter | None = None
            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                init_line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=self._socket_poll_interval_s,
                )
                if init_line:
                    init_message = json.loads(init_line.decode())
                    if init_message.get("type") == "init_status" and init_message.get("success"):
                        return
                await asyncio.sleep(self._socket_poll_interval_s)
            except (
                asyncio.TimeoutError,
                ConnectionRefusedError,
                FileNotFoundError,
                OSError,
                json.JSONDecodeError,
            ):
                await asyncio.sleep(self._socket_poll_interval_s)
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass

        raise ContainerNotReadyError(
            f"Sidecar socket not ready after {self._socket_poll_timeout_s}s"
        )

    def _resolve_created_container_id(self, stdout: str, cidfile_path: str) -> str:
        """Return the created container id from Podman stdout or its cidfile."""
        if not isinstance(stdout, str):
            raise TypeError("stdout must be a string")
        if not isinstance(cidfile_path, str):
            raise TypeError("cidfile_path must be a string")

        normalized_stdout = stdout.strip()
        if normalized_stdout:
            return normalized_stdout

        try:
            with open(cidfile_path, encoding="utf-8") as cidfile:
                container_id = cidfile.read().strip()
        except OSError as exc:
            raise ContainerStartError("podman run did not report a container id") from exc

        if not container_id:
            raise ContainerStartError("podman run did not report a container id")
        return container_id

    def _prepare_socket_dir(self, socket_dir: str, socket_path: str) -> None:
        """Create one user socket directory and remove any stale socket file."""
        if not isinstance(socket_dir, str):
            raise TypeError("socket_dir must be a string")
        normalized_socket_dir = socket_dir.strip()
        if not normalized_socket_dir:
            raise ValueError("socket_dir must not be empty")
        if not isinstance(socket_path, str):
            raise TypeError("socket_path must be a string")
        normalized_socket_path = socket_path.strip()
        if not normalized_socket_path:
            raise ValueError("socket_path must not be empty")

        try:
            os.makedirs(normalized_socket_dir, mode=0o700, exist_ok=True)
            os.chmod(normalized_socket_dir, 0o700)
            if os.path.lexists(normalized_socket_path):
                if os.path.isdir(normalized_socket_path):
                    raise ContainerStartError("container socket path must not be a directory")
                os.unlink(normalized_socket_path)
        except OSError as exc:
            raise ContainerStartError(
                f"Failed to prepare socket directory for user container: {normalized_socket_dir}"
            ) from exc

    def _remove_stale_file(self, path: str, description: str) -> None:
        """Remove one stale filesystem entry before container creation."""
        if not isinstance(path, str):
            raise TypeError("path must be a string")
        if not isinstance(description, str):
            raise TypeError("description must be a string")
        normalized_path = path.strip()
        if not normalized_path:
            raise ValueError("path must not be empty")
        normalized_description = description.strip()
        if not normalized_description:
            raise ValueError("description must not be empty")

        try:
            if os.path.lexists(normalized_path):
                if os.path.isdir(normalized_path):
                    raise ContainerStartError(
                        f"{normalized_description} path must not be a directory"
                    )
                os.unlink(normalized_path)
        except OSError as exc:
            raise ContainerStartError(f"Failed to remove stale {normalized_description}") from exc

    def _container_is_reapable(
        self,
        info: ContainerInfo,
        cutoff: datetime,
        timeout_s: int,
    ) -> bool:
        """Return whether one container can be safely reaped."""
        if not isinstance(timeout_s, int):
            raise TypeError("timeout_s must be an integer")
        if timeout_s < 0:
            raise ValueError("timeout_s must not be negative")

        self._prune_expired_protection(info, cutoff)
        if info.active_request_count > 0:
            return False
        if info.protected_operation_deadlines:
            return False
        return (cutoff - info.last_activity).total_seconds() > timeout_s

    def _prune_expired_protection(self, info: ContainerInfo, cutoff: datetime) -> None:
        """Drop any expired protection lease from one container."""
        expired_lease_keys = [
            lease_key
            for lease_key, deadline in info.protected_operation_deadlines.items()
            if deadline <= cutoff
        ]
        for lease_key in expired_lease_keys:
            del info.protected_operation_deadlines[lease_key]
