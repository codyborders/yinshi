"""Per-user Podman container management for sidecar isolation."""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from yinshi.exceptions import ContainerNotReadyError, ContainerStartError

logger = logging.getLogger(__name__)

_SIDECAR_NET = "yinshi-sidecar-net"
_USER_ID_RE = re.compile(r"^[0-9a-f]{32}$")

if TYPE_CHECKING:
    from yinshi.config import Settings


@dataclass
class ContainerInfo:
    """Tracks a running per-user sidecar container."""

    container_id: str
    user_id: str
    socket_path: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    active_request_count: int = 0
    protected_operation_deadlines: dict[str, datetime] = field(default_factory=dict)


class ContainerManager:
    """Manages per-user Podman containers for sidecar isolation.

    Each user gets a dedicated container with only their data directory
    mounted. Containers are reaped after an idle timeout.
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
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._podman_bin,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            raw_out, raw_err = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except FileNotFoundError:
            raise ContainerStartError("podman binary not found") from None
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
            raise ContainerStartError(
                f"Podman command timed out: podman {' '.join(args)}"
            ) from None

        stdout = raw_out.decode().strip()
        stderr = raw_err.decode().strip()
        returncode = proc.returncode
        if returncode is None:
            raise ContainerStartError("Podman process exited without a return code")

        if check and returncode != 0:
            raise ContainerStartError(f"podman {args[0]} failed: {stderr}")

        return returncode, stdout, stderr

    # -- Initialization -----------------------------------------------------

    async def initialize(self) -> None:
        """Create the Podman network and clean up orphaned containers.

        Idempotent -- safe to call more than once.  Called automatically
        on the first ``ensure_container`` invocation.
        """
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
        """Create the restricted Podman network if it doesn't exist."""
        rc, _, _ = await self._run_podman(
            "network",
            "exists",
            _SIDECAR_NET,
            check=False,
        )
        if rc != 0:
            await self._run_podman(
                "network",
                "create",
                "--internal",
                _SIDECAR_NET,
            )
            logger.info("Created Podman network %s", _SIDECAR_NET)

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

    # -- Public API ---------------------------------------------------------

    async def ensure_container(
        self,
        user_id: str,
        data_dir: str,
    ) -> ContainerInfo:
        """Get or create a sidecar container for a user.

        Raises ``ValueError`` if *user_id* is not a valid 32-char hex string.
        Raises ``ContainerStartError`` if the max container limit is reached.
        """
        if not _USER_ID_RE.match(user_id):
            raise ValueError(f"Invalid user_id format: {user_id!r}")

        if not self._initialized:
            await self.initialize()

        # Enforce max container count (0 = unlimited)
        max_count = getattr(self._settings, "container_max_count", 0)
        if max_count and len(self._containers) >= max_count:
            await self.reap_idle()
            if len(self._containers) >= max_count:
                raise ContainerStartError("Maximum container limit reached")

        lock = await self._get_lock(user_id)
        async with lock:
            existing = self._containers.get(user_id)
            if existing:
                if await self._is_running(existing.container_id):
                    existing.last_activity = datetime.now(timezone.utc)
                    return existing
                await self._remove_container(existing.container_id)
                del self._containers[user_id]

            return await self._create_container(user_id, data_dir)

    def touch(self, user_id: str) -> None:
        """Update last activity timestamp for a user's container."""
        info = self._containers.get(user_id)
        if info:
            info.last_activity = self._now()

    def begin_activity(self, user_id: str) -> None:
        """Mark a container as busy for the lifetime of one request."""
        if not isinstance(user_id, str):
            raise TypeError("user_id must be a string")
        normalized_user_id = user_id.strip()
        if not normalized_user_id:
            raise ValueError("user_id must not be empty")

        info = self._containers.get(normalized_user_id)
        if info is None:
            logger.warning("Cannot mark activity for missing container: %s", normalized_user_id[:8])
            return
        info.active_request_count += 1
        info.last_activity = self._now()

    def end_activity(self, user_id: str) -> None:
        """Release one active request marker for a user's container."""
        if not isinstance(user_id, str):
            raise TypeError("user_id must be a string")
        normalized_user_id = user_id.strip()
        if not normalized_user_id:
            raise ValueError("user_id must not be empty")

        info = self._containers.get(normalized_user_id)
        if info is None:
            logger.warning("Cannot end activity for missing container: %s", normalized_user_id[:8])
            return
        if info.active_request_count == 0:
            logger.warning(
                "Cannot end activity for container %s without a matching begin",
                normalized_user_id[:8],
            )
            return
        info.active_request_count -= 1
        info.last_activity = self._now()

    def protect(self, user_id: str, lease_key: str, timeout_s: int) -> None:
        """Keep one container alive for a named long-lived operation."""
        if not isinstance(user_id, str):
            raise TypeError("user_id must be a string")
        normalized_user_id = user_id.strip()
        if not normalized_user_id:
            raise ValueError("user_id must not be empty")
        if not isinstance(lease_key, str):
            raise TypeError("lease_key must be a string")
        normalized_lease_key = lease_key.strip()
        if not normalized_lease_key:
            raise ValueError("lease_key must not be empty")
        if not isinstance(timeout_s, int):
            raise TypeError("timeout_s must be an integer")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")

        info = self._containers.get(normalized_user_id)
        if info is None:
            logger.warning("Cannot protect missing container: %s", normalized_user_id[:8])
            return
        info.protected_operation_deadlines[normalized_lease_key] = self._now() + timedelta(
            seconds=timeout_s
        )
        info.last_activity = self._now()

    def unprotect(self, user_id: str, lease_key: str) -> None:
        """Remove one named long-lived operation lease from a user's container."""
        if not isinstance(user_id, str):
            raise TypeError("user_id must be a string")
        normalized_user_id = user_id.strip()
        if not normalized_user_id:
            raise ValueError("user_id must not be empty")
        if not isinstance(lease_key, str):
            raise TypeError("lease_key must be a string")
        normalized_lease_key = lease_key.strip()
        if not normalized_lease_key:
            raise ValueError("lease_key must not be empty")

        info = self._containers.get(normalized_user_id)
        if info is None:
            logger.warning("Cannot unprotect missing container: %s", normalized_user_id[:8])
            return
        info.protected_operation_deadlines.pop(normalized_lease_key, None)
        info.last_activity = self._now()

    async def destroy_container(self, user_id: str) -> None:
        """Stop and remove a user's container."""
        info = self._containers.pop(user_id, None)
        self._locks.pop(user_id, None)
        if not info:
            return
        await self._remove_container(info.container_id)
        logger.info("Destroyed container for user %s", user_id[:8])

    async def reap_idle(self) -> int:
        """Destroy containers that have been idle past the timeout."""
        timeout = self._settings.container_idle_timeout_s
        cutoff = self._now()
        idle_users = [
            uid
            for uid, info in self._containers.items()
            if self._container_is_reapable(info, cutoff, timeout)
        ]
        for uid in idle_users:
            await self.destroy_container(uid)
        return len(idle_users)

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
        user_ids = list(self._containers.keys())
        for uid in user_ids:
            await self.destroy_container(uid)
        logger.info("All sidecar containers destroyed")

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
        data_dir: str,
    ) -> ContainerInfo:
        """Start a new sidecar container for a user."""
        s = self._settings
        socket_dir = os.path.join(s.container_socket_base, user_id)
        socket_path = os.path.join(socket_dir, "sidecar.sock")
        self._prepare_socket_dir(socket_dir, socket_path)

        real_data_dir = os.path.realpath(data_dir)
        cpus = str(s.container_cpu_quota / 100000)
        runtime_uid = str(os.getuid())
        runtime_gid = str(os.getgid())

        _, container_id, _ = await self._run_podman(
            "run",
            "-d",
            "--replace",
            "--name",
            f"yinshi-sidecar-{user_id}",
            "--userns",
            "keep-id",
            "--user",
            f"{runtime_uid}:{runtime_gid}",
            "--env",
            "SIDECAR_SOCKET_PATH=/run/sidecar/sidecar.sock",
            "--env",
            "HOME=/tmp",
            "-v",
            f"{socket_dir}:/run/sidecar:rw",
            "-v",
            f"{real_data_dir}:/data:rw",
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
            s.container_image,
        )

        await self._wait_for_socket(socket_path)

        info = ContainerInfo(
            container_id=container_id,
            user_id=user_id,
            socket_path=socket_path,
        )
        self._containers[user_id] = info
        logger.info(
            "Started container %s for user %s",
            container_id[:12],
            user_id[:8],
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
