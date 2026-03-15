"""Tests for per-user container isolation."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from yinshi.exceptions import ContainerNotReadyError, ContainerStartError


# ---------------------------------------------------------------------------
# Path utility tests
# ---------------------------------------------------------------------------

class TestIsPathInside:
    """Tests for the shared is_path_inside utility."""

    def test_path_inside(self):
        from yinshi.utils.paths import is_path_inside

        assert is_path_inside("/var/lib/users/abc/repos/r", "/var/lib/users/abc") is True

    def test_path_equal_to_base(self):
        from yinshi.utils.paths import is_path_inside

        assert is_path_inside("/var/lib/users/abc", "/var/lib/users/abc") is True

    def test_path_outside(self):
        from yinshi.utils.paths import is_path_inside

        assert is_path_inside("/etc/passwd", "/var/lib/users/abc") is False

    def test_path_prefix_trick(self):
        from yinshi.utils.paths import is_path_inside

        # "/var/lib/users/abcdef" should NOT be inside "/var/lib/users/abc"
        assert is_path_inside("/var/lib/users/abcdef", "/var/lib/users/abc") is False


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
        "container_max_count": 0,
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
    client.containers.list.return_value = []

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
        user_id = "abcdef12345678901234567890abcdef"
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

        user_id = "abcdef12345678901234567890abcdef"
        data_dir = str(tmp_path / "data")

        # Pre-populate with existing container
        existing = ContainerInfo(
            container_id="existing123",
            user_id=user_id,
            socket_path="/tmp/fake.sock",
            created_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
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

        user_id = "abcdef12345678901234567890abcdef"
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
            created_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
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

        user_id = "abcdef12345678901234567890abcdef"
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
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
        idle_uid = "0" * 32
        active_uid = "1" * 32
        old_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        mgr._containers[idle_uid] = ContainerInfo(
            container_id="idle123",
            user_id=idle_uid,
            socket_path="/tmp/fake.sock",
            created_at=old_time,
            last_activity=old_time,
        )
        mgr._containers[active_uid] = ContainerInfo(
            container_id="active123",
            user_id=active_uid,
            socket_path="/tmp/fake2.sock",
            created_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
        )

        count = await mgr.reap_idle()

        assert count == 1
        assert idle_uid not in mgr._containers
        assert active_uid in mgr._containers

    @pytest.mark.asyncio
    async def test_destroy_all(self, tmp_path):
        from yinshi.services.container import ContainerInfo, ContainerManager

        settings = _make_settings(container_socket_base=str(tmp_path))
        docker_client, _ = _mock_docker_client()
        mgr = ContainerManager(settings=settings, docker_client=docker_client)

        mgr._containers["a" * 32] = ContainerInfo(
            container_id="c1", user_id="a" * 32, socket_path="/tmp/s1",
            created_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
        )
        mgr._containers["b" * 32] = ContainerInfo(
            container_id="c2", user_id="b" * 32, socket_path="/tmp/s2",
            created_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
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

        user_id = "abcdef12345678901234567890abcdef"
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        with pytest.raises(ContainerNotReadyError):
            await mgr.ensure_container(user_id, data_dir)

    @pytest.mark.asyncio
    async def test_invalid_user_id_rejected(self, tmp_path):
        """S1: user_id must be a 32-char hex string."""
        from yinshi.services.container import ContainerManager

        settings = _make_settings(container_socket_base=str(tmp_path))
        docker_client, _ = _mock_docker_client()
        mgr = ContainerManager(settings=settings, docker_client=docker_client)

        with pytest.raises(ValueError, match="Invalid user_id"):
            await mgr.ensure_container("../../etc", str(tmp_path))

        with pytest.raises(ValueError, match="Invalid user_id"):
            await mgr.ensure_container("short", str(tmp_path))

        with pytest.raises(ValueError, match="Invalid user_id"):
            await mgr.ensure_container("ABCDEF12345678901234567890ABCDEF", str(tmp_path))

    @pytest.mark.asyncio
    async def test_destroy_container_cleans_up_lock(self, tmp_path):
        """P3: Locks should be cleaned up when containers are destroyed."""
        from yinshi.services.container import ContainerInfo, ContainerManager

        settings = _make_settings(container_socket_base=str(tmp_path))
        docker_client, _ = _mock_docker_client()
        mgr = ContainerManager(settings=settings, docker_client=docker_client)

        user_id = "abcdef12345678901234567890abcdef"
        mgr._containers[user_id] = ContainerInfo(
            container_id="c1", user_id=user_id, socket_path="/tmp/s1",
            created_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
        )
        # Simulate that a lock exists for this user
        import asyncio
        mgr._locks[user_id] = asyncio.Lock()

        await mgr.destroy_container(user_id)

        assert user_id not in mgr._containers
        assert user_id not in mgr._locks

    @pytest.mark.asyncio
    async def test_max_container_count_enforced(self, tmp_path):
        """P5: Reject new containers when max count is reached."""
        from yinshi.services.container import ContainerInfo, ContainerManager

        settings = _make_settings(
            container_socket_base=str(tmp_path),
            container_max_count=2,
            container_idle_timeout_s=99999,
        )
        docker_client, _ = _mock_docker_client()
        mgr = ContainerManager(settings=settings, docker_client=docker_client)

        # Fill up to max
        uid1 = "a" * 32
        uid2 = "b" * 32
        uid3 = "c" * 32
        mgr._containers[uid1] = ContainerInfo(
            container_id="c1", user_id=uid1, socket_path="/tmp/s1",
            created_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
        )
        mgr._containers[uid2] = ContainerInfo(
            container_id="c2", user_id=uid2, socket_path="/tmp/s2",
            created_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
        )

        with pytest.raises(ContainerStartError, match="Maximum container limit"):
            await mgr.ensure_container(uid3, str(tmp_path / "data"))

    @pytest.mark.asyncio
    async def test_orphaned_containers_cleaned_on_init(self, tmp_path):
        """S7: Orphaned containers should be removed on initialization."""
        from yinshi.services.container import ContainerManager

        settings = _make_settings(container_socket_base=str(tmp_path))
        docker_client, _ = _mock_docker_client()

        orphan = MagicMock()
        orphan.id = "orphan123"
        docker_client.containers.list.return_value = [orphan]

        mgr = ContainerManager(settings=settings, docker_client=docker_client)
        await mgr.initialize()

        orphan.remove.assert_called_once_with(force=True)

    @pytest.mark.asyncio
    async def test_socket_dir_permissions(self, tmp_path):
        """S2: Socket directory should be created with 0o700 permissions."""
        from yinshi.services.container import ContainerManager

        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)
        docker_client, container = _mock_docker_client()

        mgr = ContainerManager(settings=settings, docker_client=docker_client)
        mgr._socket_poll_timeout_s = 0.2
        mgr._socket_poll_interval_s = 0.05

        user_id = "abcdef12345678901234567890abcdef"
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        socket_dir = os.path.join(socket_base, user_id)

        def _create_socket(*args, **kwargs):
            # Socket dir is created by _create_container before this call
            with open(os.path.join(socket_dir, "sidecar.sock"), "w") as f:
                f.write("")
            return container

        docker_client.containers.run.side_effect = _create_socket

        await mgr.ensure_container(user_id, data_dir)

        # Verify socket dir has restricted permissions
        stat = os.stat(socket_dir)
        assert oct(stat.st_mode & 0o777) == oct(0o700)
