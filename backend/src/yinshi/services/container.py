"""Per-user Docker container management for sidecar isolation."""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime

import docker
import docker.errors

from yinshi.exceptions import ContainerNotReadyError, ContainerStartError

logger = logging.getLogger(__name__)

_SIDECAR_NET = "yinshi-sidecar-net"


@dataclass
class ContainerInfo:
    """Tracks a running per-user sidecar container."""

    container_id: str
    user_id: str
    socket_path: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)


class ContainerManager:
    """Manages per-user Docker containers for sidecar isolation.

    Each user gets a dedicated container with only their data directory
    mounted. Containers are reaped after an idle timeout.
    """

    def __init__(self, settings, docker_client=None) -> None:
        self._settings = settings
        self._docker = docker_client or docker.from_env()
        self._containers: dict[str, ContainerInfo] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._socket_poll_timeout_s: float = 10.0
        self._socket_poll_interval_s: float = 0.1
        self._ensure_network()

    def _ensure_network(self) -> None:
        """Create the restricted Docker network if it doesn't exist."""
        try:
            self._docker.networks.get(_SIDECAR_NET)
        except docker.errors.NotFound:
            self._docker.networks.create(_SIDECAR_NET, driver="bridge")
            logger.info("Created Docker network %s", _SIDECAR_NET)

    async def _get_lock(self, user_id: str) -> asyncio.Lock:
        """Get or create a per-user lock."""
        async with self._global_lock:
            if user_id not in self._locks:
                self._locks[user_id] = asyncio.Lock()
            return self._locks[user_id]

    async def ensure_container(
        self, user_id: str, data_dir: str
    ) -> ContainerInfo:
        """Get or create a sidecar container for a user."""
        lock = await self._get_lock(user_id)
        async with lock:
            existing = self._containers.get(user_id)
            if existing:
                if self._is_running(existing.container_id):
                    existing.last_activity = datetime.utcnow()
                    return existing
                self._remove_container(existing.container_id)
                del self._containers[user_id]

            return await self._create_container(user_id, data_dir)

    def _is_running(self, container_id: str) -> bool:
        """Check if a container is still running."""
        try:
            c = self._docker.containers.get(container_id)
            return c.status == "running"
        except docker.errors.NotFound:
            return False

    def _remove_container(self, container_id: str) -> None:
        """Force-remove a container."""
        try:
            c = self._docker.containers.get(container_id)
            c.remove(force=True)
            logger.info("Removed container %s", container_id[:12])
        except docker.errors.NotFound:
            pass

    async def _create_container(
        self, user_id: str, data_dir: str
    ) -> ContainerInfo:
        """Start a new sidecar container for a user."""
        s = self._settings
        socket_dir = os.path.join(s.container_socket_base, user_id)
        os.makedirs(socket_dir, exist_ok=True)
        socket_path = os.path.join(socket_dir, "sidecar.sock")

        try:
            container = await asyncio.to_thread(
                self._docker.containers.run,
                image=s.container_image,
                name=f"yinshi-sidecar-{user_id[:12]}",
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
                f"Failed to start container for user {user_id[:8]}: {exc}"
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
        elapsed = 0.0
        while elapsed < self._socket_poll_timeout_s:
            if os.path.exists(socket_path):
                return
            await asyncio.sleep(self._socket_poll_interval_s)
            elapsed += self._socket_poll_interval_s

        raise ContainerNotReadyError(
            f"Sidecar socket not ready after {self._socket_poll_timeout_s}s"
        )

    def touch(self, user_id: str) -> None:
        """Update last activity timestamp for a user's container."""
        info = self._containers.get(user_id)
        if info:
            info.last_activity = datetime.utcnow()

    async def destroy_container(self, user_id: str) -> None:
        """Stop and remove a user's container."""
        info = self._containers.pop(user_id, None)
        if not info:
            return
        self._remove_container(info.container_id)
        logger.info(
            "Destroyed container for user %s", user_id[:8]
        )

    async def reap_idle(self) -> int:
        """Destroy containers that have been idle past the timeout."""
        timeout = self._settings.container_idle_timeout_s
        cutoff = datetime.utcnow()
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
