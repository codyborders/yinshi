"""Per-user Docker container management for sidecar isolation."""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import docker
import docker.errors

from yinshi.exceptions import ContainerNotReadyError, ContainerStartError

logger = logging.getLogger(__name__)

_SIDECAR_NET = "yinshi-sidecar-net"
_USER_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class ContainerInfo:
    """Tracks a running per-user sidecar container."""

    container_id: str
    user_id: str
    socket_path: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ContainerManager:
    """Manages per-user Docker containers for sidecar isolation.

    Each user gets a dedicated container with only their data directory
    mounted. Containers are reaped after an idle timeout.
    """

    def __init__(
        self,
        settings: "Settings",  # noqa: F821 -- forward ref avoids circular import
        docker_client: "docker.DockerClient | None" = None,
    ) -> None:
        self._settings = settings
        self._docker = docker_client or docker.from_env()
        self._containers: dict[str, ContainerInfo] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._socket_poll_timeout_s: float = 10.0
        self._socket_poll_interval_s: float = 0.1
        self._initialized = False

    async def initialize(self) -> None:
        """Create the Docker network and clean up orphaned containers.

        Idempotent -- safe to call more than once.  Called automatically
        on the first ``ensure_container`` invocation.
        """
        if self._initialized:
            return
        await asyncio.to_thread(self._ensure_network)
        await asyncio.to_thread(self._cleanup_orphaned_containers)
        self._initialized = True

    # -- Docker network --------------------------------------------------

    def _ensure_network(self) -> None:
        """Create the restricted Docker network if it doesn't exist."""
        try:
            self._docker.networks.get(_SIDECAR_NET)
        except docker.errors.NotFound:
            self._docker.networks.create(
                _SIDECAR_NET, driver="bridge", internal=True,
            )
            logger.info("Created Docker network %s", _SIDECAR_NET)

    # -- Orphan cleanup ---------------------------------------------------

    def _cleanup_orphaned_containers(self) -> None:
        """Remove containers left over from a previous process crash."""
        try:
            stale = self._docker.containers.list(
                filters={"label": "yinshi.user_id"}, all=True,
            )
            for c in stale:
                c.remove(force=True)
                logger.info("Removed orphaned container %s", c.id[:12])
        except docker.errors.APIError:
            logger.warning("Failed to clean up orphaned containers", exc_info=True)

    # -- Per-user locks ---------------------------------------------------

    async def _get_lock(self, user_id: str) -> asyncio.Lock:
        """Get or create a per-user lock."""
        async with self._global_lock:
            if user_id not in self._locks:
                self._locks[user_id] = asyncio.Lock()
            return self._locks[user_id]

    # -- Public API -------------------------------------------------------

    async def ensure_container(
        self, user_id: str, data_dir: str,
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
            info.last_activity = datetime.now(timezone.utc)

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
        cutoff = datetime.now(timezone.utc)
        idle_users = [
            uid
            for uid, info in self._containers.items()
            if (cutoff - info.last_activity).total_seconds() > timeout
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

    # -- Internal helpers -------------------------------------------------

    async def _is_running(self, container_id: str) -> bool:
        """Check if a container is still running."""
        try:
            c = await asyncio.to_thread(
                self._docker.containers.get, container_id,
            )
            return c.status == "running"
        except docker.errors.NotFound:
            return False

    async def _remove_container(self, container_id: str) -> None:
        """Force-remove a container."""
        try:
            c = await asyncio.to_thread(
                self._docker.containers.get, container_id,
            )
            await asyncio.to_thread(c.remove, force=True)
            logger.info("Removed container %s", container_id[:12])
        except docker.errors.NotFound:
            pass

    async def _create_container(
        self, user_id: str, data_dir: str,
    ) -> ContainerInfo:
        """Start a new sidecar container for a user."""
        s = self._settings
        socket_dir = os.path.join(s.container_socket_base, user_id)
        os.makedirs(socket_dir, mode=0o700, exist_ok=True)
        socket_path = os.path.join(socket_dir, "sidecar.sock")

        try:
            container = await asyncio.to_thread(
                self._docker.containers.run,
                image=s.container_image,
                name=f"yinshi-sidecar-{user_id}",
                detach=True,
                environment={
                    "SIDECAR_SOCKET_PATH": "/run/sidecar/sidecar.sock",
                },
                volumes={
                    socket_dir: {"bind": "/run/sidecar", "mode": "rw"},
                    os.path.realpath(data_dir): {
                        "bind": "/data",
                        "mode": "rw",
                    },
                },
                network=_SIDECAR_NET,
                mem_limit=s.container_memory_limit,
                memswap_limit=s.container_memory_limit,
                cpu_period=100000,
                cpu_quota=s.container_cpu_quota,
                pids_limit=s.container_pids_limit,
                security_opt=["no-new-privileges"],
                cap_drop=["ALL"],
                labels={"yinshi.user_id": user_id},
            )
        except docker.errors.APIError as exc:
            raise ContainerStartError(
                f"Failed to start container for user {user_id[:8]}"
            ) from exc

        await self._wait_for_socket(socket_path)

        info = ContainerInfo(
            container_id=container.id,
            user_id=user_id,
            socket_path=socket_path,
        )
        self._containers[user_id] = info
        logger.info(
            "Started container %s for user %s",
            container.id[:12],
            user_id[:8],
        )
        return info

    async def _wait_for_socket(self, socket_path: str) -> None:
        """Poll until the sidecar socket file appears."""
        deadline = time.monotonic() + self._socket_poll_timeout_s
        while time.monotonic() < deadline:
            exists = await asyncio.to_thread(os.path.exists, socket_path)
            if exists:
                return
            await asyncio.sleep(self._socket_poll_interval_s)

        raise ContainerNotReadyError(
            f"Sidecar socket not ready after {self._socket_poll_timeout_s}s"
        )
