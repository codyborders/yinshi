"""Tests for per-user container isolation."""

import asyncio
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yinshi.exceptions import ContainerNotReadyError, ContainerStartError


# ---------------------------------------------------------------------------
# Path remapping tests (pure function, no mocks needed)
# ---------------------------------------------------------------------------

class TestRemapPath:
    """Tests for _remap_path helper in stream.py."""

    def test_remap_basic(self):
        from yinshi.api.stream import _remap_path

        result = _remap_path(
            "/var/lib/yinshi/users/ab/abc123/repos/myrepo/.worktrees/branch",
            "/var/lib/yinshi/users/ab/abc123",
        )
        assert result == "/data/repos/myrepo/.worktrees/branch"

    def test_remap_data_dir_root(self):
        from yinshi.api.stream import _remap_path

        result = _remap_path(
            "/var/lib/yinshi/users/ab/abc123",
            "/var/lib/yinshi/users/ab/abc123",
        )
        assert result == "/data"

    def test_remap_custom_mount(self):
        from yinshi.api.stream import _remap_path

        result = _remap_path(
            "/var/lib/yinshi/users/ab/abc123/repos/r",
            "/var/lib/yinshi/users/ab/abc123",
            mount="/workspace",
        )
        assert result == "/workspace/repos/r"

    def test_remap_rejects_outside_path(self):
        from yinshi.api.stream import _remap_path

        with pytest.raises(ValueError, match="outside user data directory"):
            _remap_path("/etc/passwd", "/var/lib/yinshi/users/ab/abc123")


# ---------------------------------------------------------------------------
# ContainerManager tests (mock Docker SDK)
# ---------------------------------------------------------------------------

def _make_settings(**overrides):
    """Build a mock Settings object with container defaults."""
    defaults = {
        "container_enabled": True,
        "container_image": "yinshi-sidecar:latest",
        "container_idle_timeout_s": 900,
        "container_memory_limit": "512m",
        "container_cpu_quota": 50000,
        "container_pids_limit": 256,
        "container_socket_base": "/tmp/test-yinshi-sockets",
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


def _mock_docker_client():
    """Return a mock docker.DockerClient."""
    client = MagicMock()
    container = MagicMock()
    container.id = "abc123deadbeef"
    container.status = "running"
    container.attrs = {"State": {"Status": "running"}}
    client.containers.run.return_value = container
    client.containers.get.return_value = container

    network = MagicMock()
    client.networks.get.return_value = network

    return client, container


class TestContainerManager:
    """Tests for ContainerManager lifecycle methods."""

    @pytest.mark.asyncio
    async def test_ensure_container_creates_new(self, tmp_path):
        from yinshi.services.container import ContainerManager

        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)
        docker_client, container = _mock_docker_client()

        mgr = ContainerManager(settings=settings, docker_client=docker_client)

        # Simulate socket file appearing after container start
        user_id = "abcdef1234567890"
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        socket_dir = os.path.join(socket_base, user_id)

        def _create_socket(*args, **kwargs):
            os.makedirs(socket_dir, exist_ok=True)
            with open(os.path.join(socket_dir, "sidecar.sock"), "w") as f:
                f.write("")
            return container

        docker_client.containers.run.side_effect = _create_socket

        info = await mgr.ensure_container(user_id, data_dir)

        assert info.user_id == user_id
        assert info.container_id == "abc123deadbeef"
        assert "sidecar.sock" in info.socket_path

        # Verify Docker was called with security options
        call_kwargs = docker_client.containers.run.call_args
        assert call_kwargs is not None

    @pytest.mark.asyncio
    async def test_ensure_container_reuses_existing(self, tmp_path):
        from yinshi.services.container import ContainerInfo, ContainerManager

        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)
        docker_client, container = _mock_docker_client()

        mgr = ContainerManager(settings=settings, docker_client=docker_client)

        user_id = "abcdef1234567890"
        data_dir = str(tmp_path / "data")

        # Pre-populate with existing container
        existing = ContainerInfo(
            container_id="existing123",
            user_id=user_id,
            socket_path="/tmp/fake.sock",
            created_at=datetime.utcnow(),
            last_activity=datetime.utcnow(),
        )
        mgr._containers[user_id] = existing

        info = await mgr.ensure_container(user_id, data_dir)

        assert info.container_id == "existing123"
        docker_client.containers.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_container_replaces_stopped(self, tmp_path):
        from yinshi.services.container import ContainerInfo, ContainerManager

        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)
        docker_client, container = _mock_docker_client()

        mgr = ContainerManager(settings=settings, docker_client=docker_client)

        user_id = "abcdef1234567890"
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        # Existing container is stopped
        stopped_container = MagicMock()
        stopped_container.status = "exited"
        docker_client.containers.get.return_value = stopped_container

        existing = ContainerInfo(
            container_id="stopped123",
            user_id=user_id,
            socket_path="/tmp/fake.sock",
            created_at=datetime.utcnow(),
            last_activity=datetime.utcnow(),
        )
        mgr._containers[user_id] = existing

        socket_dir = os.path.join(socket_base, user_id)

        def _create_socket(*args, **kwargs):
            os.makedirs(socket_dir, exist_ok=True)
            with open(os.path.join(socket_dir, "sidecar.sock"), "w") as f:
                f.write("")
            return container

        docker_client.containers.run.side_effect = _create_socket

        info = await mgr.ensure_container(user_id, data_dir)

        assert info.container_id == "abc123deadbeef"
        stopped_container.remove.assert_called_once_with(force=True)

    @pytest.mark.asyncio
    async def test_touch_updates_last_activity(self, tmp_path):
        from yinshi.services.container import ContainerInfo, ContainerManager

        settings = _make_settings(container_socket_base=str(tmp_path))
        docker_client, _ = _mock_docker_client()
        mgr = ContainerManager(settings=settings, docker_client=docker_client)

        user_id = "abcdef1234567890"
        old_time = datetime.utcnow() - timedelta(minutes=10)
        mgr._containers[user_id] = ContainerInfo(
            container_id="c1",
            user_id=user_id,
            socket_path="/tmp/fake.sock",
            created_at=old_time,
            last_activity=old_time,
        )

        mgr.touch(user_id)

        assert mgr._containers[user_id].last_activity > old_time

    @pytest.mark.asyncio
    async def test_reap_idle_destroys_old_containers(self, tmp_path):
        from yinshi.services.container import ContainerInfo, ContainerManager

        settings = _make_settings(
            container_socket_base=str(tmp_path),
            container_idle_timeout_s=60,
        )
        docker_client, _ = _mock_docker_client()
        mgr = ContainerManager(settings=settings, docker_client=docker_client)

        # One idle, one active
        old_time = datetime.utcnow() - timedelta(minutes=5)
        mgr._containers["idle-user"] = ContainerInfo(
            container_id="idle123",
            user_id="idle-user",
            socket_path="/tmp/fake.sock",
            created_at=old_time,
            last_activity=old_time,
        )
        mgr._containers["active-user"] = ContainerInfo(
            container_id="active123",
            user_id="active-user",
            socket_path="/tmp/fake2.sock",
            created_at=datetime.utcnow(),
            last_activity=datetime.utcnow(),
        )

        count = await mgr.reap_idle()

        assert count == 1
        assert "idle-user" not in mgr._containers
        assert "active-user" in mgr._containers

    @pytest.mark.asyncio
    async def test_destroy_all(self, tmp_path):
        from yinshi.services.container import ContainerInfo, ContainerManager

        settings = _make_settings(container_socket_base=str(tmp_path))
        docker_client, _ = _mock_docker_client()
        mgr = ContainerManager(settings=settings, docker_client=docker_client)

        mgr._containers["u1"] = ContainerInfo(
            container_id="c1", user_id="u1", socket_path="/tmp/s1",
            created_at=datetime.utcnow(), last_activity=datetime.utcnow(),
        )
        mgr._containers["u2"] = ContainerInfo(
            container_id="c2", user_id="u2", socket_path="/tmp/s2",
            created_at=datetime.utcnow(), last_activity=datetime.utcnow(),
        )

        await mgr.destroy_all()

        assert len(mgr._containers) == 0

    @pytest.mark.asyncio
    async def test_socket_timeout_raises(self, tmp_path):
        from yinshi.services.container import ContainerManager

        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)
        docker_client, container = _mock_docker_client()

        mgr = ContainerManager(settings=settings, docker_client=docker_client)
        # Socket file never appears -- should timeout
        mgr._socket_poll_timeout_s = 0.3
        mgr._socket_poll_interval_s = 0.1

        user_id = "abcdef1234567890"
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        with pytest.raises(ContainerNotReadyError):
            await mgr.ensure_container(user_id, data_dir)
