"""Tests for per-user container isolation (Podman subprocess backend)."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yinshi.api.stream import _remap_path
from yinshi.exceptions import ContainerNotReadyError, ContainerStartError
from yinshi.services.container import ContainerInfo, ContainerManager
from yinshi.utils.paths import is_path_inside

# ---------------------------------------------------------------------------
# Path utility tests
# ---------------------------------------------------------------------------


class TestIsPathInside:
    """Tests for the shared is_path_inside utility."""

    def test_path_inside(self):
        assert is_path_inside("/var/lib/users/abc/repos/r", "/var/lib/users/abc") is True

    def test_path_equal_to_base(self):
        assert is_path_inside("/var/lib/users/abc", "/var/lib/users/abc") is True

    def test_path_outside(self):
        assert is_path_inside("/etc/passwd", "/var/lib/users/abc") is False

    def test_path_prefix_trick(self):
        # "/var/lib/users/abcdef" should NOT be inside "/var/lib/users/abc"
        assert is_path_inside("/var/lib/users/abcdef", "/var/lib/users/abc") is False


# ---------------------------------------------------------------------------
# Path remapping tests (pure function, no mocks needed)
# ---------------------------------------------------------------------------


class TestRemapPath:
    """Tests for _remap_path helper in stream.py."""

    def test_remap_basic(self):
        result = _remap_path(
            "/var/lib/yinshi/users/ab/abc123/repos/myrepo/.worktrees/branch",
            "/var/lib/yinshi/users/ab/abc123",
        )
        assert result == "/data/repos/myrepo/.worktrees/branch"

    def test_remap_data_dir_root(self):
        result = _remap_path(
            "/var/lib/yinshi/users/ab/abc123",
            "/var/lib/yinshi/users/ab/abc123",
        )
        assert result == "/data"

    def test_remap_custom_mount(self):
        result = _remap_path(
            "/var/lib/yinshi/users/ab/abc123/repos/r",
            "/var/lib/yinshi/users/ab/abc123",
            mount="/workspace",
        )
        assert result == "/workspace/repos/r"

    def test_remap_rejects_outside_path(self):
        with pytest.raises(ValueError, match="outside user data directory"):
            _remap_path("/etc/passwd", "/var/lib/yinshi/users/ab/abc123")


# ---------------------------------------------------------------------------
# Podman subprocess mock helpers
# ---------------------------------------------------------------------------


def _make_mock_process(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> AsyncMock:
    """Build a mock asyncio.subprocess.Process."""
    proc = AsyncMock()
    proc.wait = AsyncMock(return_value=returncode)
    proc.stdout = AsyncMock()
    proc.stdout.read = AsyncMock(return_value=stdout.encode())
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=stderr.encode())
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


def _podman_router(routes: dict[str, AsyncMock]) -> AsyncMock:
    """Return an async side_effect that dispatches by podman subcommand.

    *routes* maps subcommand name (e.g. "run", "rm", "inspect") to a
    mock process.  Unmatched subcommands return success with empty output.
    """

    async def _dispatch(*args, **kwargs):
        # args[0] = "podman", args[1] = subcommand
        subcmd = args[1] if len(args) > 1 else ""
        if subcmd in routes:
            return routes[subcmd]
        return _make_mock_process()

    mock = AsyncMock(side_effect=_dispatch)
    return mock


def _ready_socket_listener(expected_socket_path: str) -> AsyncMock:
    """Return a mocked Unix socket listener that emits the sidecar init banner."""
    reader = AsyncMock()
    reader.readline = AsyncMock(
        return_value=b'{"id":"init","type":"init_status","success":true}\n'
    )
    writer = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock(return_value=None)

    async def _open_socket(path: str, *args, **kwargs):
        del args, kwargs
        assert path == expected_socket_path
        return reader, writer

    return AsyncMock(side_effect=_open_socket)


def _make_settings(**overrides):
    """Build a mock Settings object with container defaults."""
    defaults = {
        "container_enabled": True,
        "container_image": "yinshi-sidecar:latest",
        "container_idle_timeout_s": 300,
        "container_memory_limit": "256m",
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


# ---------------------------------------------------------------------------
# ContainerManager tests (mock Podman subprocess)
# ---------------------------------------------------------------------------


class TestContainerManager:
    """Tests for ContainerManager lifecycle methods."""

    @pytest.mark.asyncio
    async def test_ensure_container_creates_new(self, tmp_path):
        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)

        user_id = "abcdef12345678901234567890abcdef"
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)
        socket_dir = os.path.join(socket_base, user_id)

        container_id = "abc123deadbeef456789"

        def _run_side_effect(*args, **kwargs):
            subcmd = args[1] if len(args) > 1 else ""
            if subcmd == "run":
                # Simulate socket file appearing after container start
                os.makedirs(socket_dir, exist_ok=True)
                with open(os.path.join(socket_dir, "sidecar.sock"), "w") as f:
                    f.write("")
                return _make_mock_process(stdout=container_id)
            if subcmd == "network":
                return _make_mock_process(returncode=0)
            if subcmd == "ps":
                return _make_mock_process(stdout="[]")
            return _make_mock_process()

        socket_path = os.path.join(socket_dir, "sidecar.sock")
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=_run_side_effect)),
            patch("asyncio.open_unix_connection", _ready_socket_listener(socket_path)),
        ):
            mgr = ContainerManager(settings=settings)
            info = await mgr.ensure_container(user_id, data_dir)

        assert info.user_id == user_id
        assert info.container_id == container_id
        assert "sidecar.sock" in info.socket_path

    @pytest.mark.asyncio
    async def test_ensure_container_reuses_existing(self, tmp_path):
        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)

        user_id = "abcdef12345678901234567890abcdef"
        data_dir = str(tmp_path / "data")

        # "inspect" returns "running" for status check
        routes = {
            "network": _make_mock_process(returncode=0),
            "ps": _make_mock_process(stdout="[]"),
            "inspect": _make_mock_process(stdout="running"),
        }

        with patch("asyncio.create_subprocess_exec", _podman_router(routes)) as mock_exec:
            mgr = ContainerManager(settings=settings)

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
        # Verify "run" was never called (container was reused)
        for call in mock_exec.call_args_list:
            assert call.args[1] != "run" or call.args[0] != "podman"

    @pytest.mark.asyncio
    async def test_ensure_container_replaces_stopped(self, tmp_path):
        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)

        user_id = "abcdef12345678901234567890abcdef"
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)
        socket_dir = os.path.join(socket_base, user_id)

        new_container_id = "newcontainer789"
        call_count = {"inspect": 0}

        def _side_effect(*args, **kwargs):
            subcmd = args[1] if len(args) > 1 else ""
            if subcmd == "inspect":
                call_count["inspect"] += 1
                # First inspect: stopped; subsequent: running
                if call_count["inspect"] == 1:
                    return _make_mock_process(stdout="exited")
                return _make_mock_process(stdout="running")
            if subcmd == "rm":
                return _make_mock_process(returncode=0)
            if subcmd == "run":
                os.makedirs(socket_dir, exist_ok=True)
                with open(os.path.join(socket_dir, "sidecar.sock"), "w") as f:
                    f.write("")
                return _make_mock_process(stdout=new_container_id)
            if subcmd == "network":
                return _make_mock_process(returncode=0)
            if subcmd == "ps":
                return _make_mock_process(stdout="[]")
            return _make_mock_process()

        socket_path = os.path.join(socket_dir, "sidecar.sock")
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=_side_effect)) as mock_exec,
            patch("asyncio.open_unix_connection", _ready_socket_listener(socket_path)),
        ):
            mgr = ContainerManager(settings=settings)

            existing = ContainerInfo(
                container_id="stopped123",
                user_id=user_id,
                socket_path="/tmp/fake.sock",
                created_at=datetime.now(timezone.utc),
                last_activity=datetime.now(timezone.utc),
            )
            mgr._containers[user_id] = existing

            info = await mgr.ensure_container(user_id, data_dir)

        assert info.container_id == new_container_id
        # Verify rm was called to remove the stopped container
        rm_calls = [c for c in mock_exec.call_args_list if len(c.args) > 1 and c.args[1] == "rm"]
        assert len(rm_calls) >= 1

    @pytest.mark.asyncio
    async def test_touch_updates_last_activity(self, tmp_path):
        settings = _make_settings(container_socket_base=str(tmp_path))
        mgr = ContainerManager(settings=settings)

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
    async def test_begin_activity_prevents_reaping_busy_container(self, tmp_path):
        """Busy containers must stay alive even when their idle timestamp is old."""
        settings = _make_settings(
            container_socket_base=str(tmp_path),
            container_idle_timeout_s=60,
        )
        mgr = ContainerManager(settings=settings)

        user_id = "abcdef12345678901234567890abcdef"
        old_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        mgr._containers[user_id] = ContainerInfo(
            container_id="busy1",
            user_id=user_id,
            socket_path="/tmp/busy.sock",
            created_at=old_time,
            last_activity=old_time,
        )

        mgr.begin_activity(user_id)
        count = await mgr.reap_idle()

        assert count == 0
        assert user_id in mgr._containers
        assert mgr._containers[user_id].active_request_count == 1

    @pytest.mark.asyncio
    async def test_protect_prevents_reaping_until_lease_expires(self, tmp_path):
        """Protected containers should survive until their keepalive lease expires."""
        settings = _make_settings(
            container_socket_base=str(tmp_path),
            container_idle_timeout_s=60,
        )
        mgr = ContainerManager(settings=settings)

        user_id = "fedcba98765432100123456789abcdef"
        old_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        mgr._containers[user_id] = ContainerInfo(
            container_id="protected1",
            user_id=user_id,
            socket_path="/tmp/protected.sock",
            created_at=old_time,
            last_activity=old_time,
        )

        mgr.protect(user_id, "oauth:flow-1", 300)
        count = await mgr.reap_idle()
        assert count == 0
        assert user_id in mgr._containers

        mgr._containers[user_id].protected_operation_deadlines["oauth:flow-1"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        mgr._containers[user_id].last_activity = old_time
        with patch(
            "asyncio.create_subprocess_exec",
            _podman_router({"rm": _make_mock_process(returncode=0)}),
        ):
            count = await mgr.reap_idle()

        assert count == 1
        assert user_id not in mgr._containers

    @pytest.mark.asyncio
    async def test_reap_idle_destroys_old_containers(self, tmp_path):
        settings = _make_settings(
            container_socket_base=str(tmp_path),
            container_idle_timeout_s=60,
        )

        routes = {
            "rm": _make_mock_process(returncode=0),
        }

        with patch("asyncio.create_subprocess_exec", _podman_router(routes)):
            mgr = ContainerManager(settings=settings)

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
    async def test_wait_for_socket_requires_live_listener(self, tmp_path):
        """Socket readiness should require a live sidecar listener, not any filesystem entry."""
        settings = _make_settings(container_socket_base=str(tmp_path))
        mgr = ContainerManager(settings=settings)
        mgr._socket_poll_timeout_s = 0.2
        mgr._socket_poll_interval_s = 0.01

        socket_path = str(tmp_path / "sidecar.sock")
        Path(socket_path).write_text("stale", encoding="utf-8")

        reader = AsyncMock()
        reader.readline = AsyncMock(
            return_value=b'{"id":"init","type":"init_status","success":true}\n'
        )
        writer = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock(return_value=None)

        attempts = {"count": 0}

        async def _open_socket(path: str, *args, **kwargs):
            del args, kwargs
            assert path == socket_path
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise ConnectionRefusedError("listener not ready")
            return reader, writer

        with patch("asyncio.open_unix_connection", AsyncMock(side_effect=_open_socket)):
            await mgr._wait_for_socket(socket_path)

        assert attempts["count"] == 3

    @pytest.mark.asyncio
    async def test_destroy_all(self, tmp_path):
        settings = _make_settings(container_socket_base=str(tmp_path))

        routes = {
            "rm": _make_mock_process(returncode=0),
        }

        with patch("asyncio.create_subprocess_exec", _podman_router(routes)):
            mgr = ContainerManager(settings=settings)

            mgr._containers["a" * 32] = ContainerInfo(
                container_id="c1",
                user_id="a" * 32,
                socket_path="/tmp/s1",
                created_at=datetime.now(timezone.utc),
                last_activity=datetime.now(timezone.utc),
            )
            mgr._containers["b" * 32] = ContainerInfo(
                container_id="c2",
                user_id="b" * 32,
                socket_path="/tmp/s2",
                created_at=datetime.now(timezone.utc),
                last_activity=datetime.now(timezone.utc),
            )

            await mgr.destroy_all()

        assert len(mgr._containers) == 0

    @pytest.mark.asyncio
    async def test_socket_timeout_raises(self, tmp_path):
        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)

        user_id = "abcdef12345678901234567890abcdef"
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        container_id = "timeout_container_123"

        def _side_effect(*args, **kwargs):
            subcmd = args[1] if len(args) > 1 else ""
            if subcmd == "run":
                # Container starts but socket never appears
                return _make_mock_process(stdout=container_id)
            if subcmd == "network":
                return _make_mock_process(returncode=0)
            if subcmd == "ps":
                return _make_mock_process(stdout="[]")
            return _make_mock_process()

        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=_side_effect)):
            mgr = ContainerManager(settings=settings)
            mgr._socket_poll_timeout_s = 0.3
            mgr._socket_poll_interval_s = 0.1

            with pytest.raises(ContainerNotReadyError):
                await mgr.ensure_container(user_id, data_dir)

    @pytest.mark.asyncio
    async def test_invalid_user_id_rejected(self, tmp_path):
        """S1: user_id must be a 32-char hex string."""
        settings = _make_settings(container_socket_base=str(tmp_path))
        mgr = ContainerManager(settings=settings)

        with pytest.raises(ValueError, match="Invalid user_id"):
            await mgr.ensure_container("../../etc", str(tmp_path))

        with pytest.raises(ValueError, match="Invalid user_id"):
            await mgr.ensure_container("short", str(tmp_path))

        with pytest.raises(ValueError, match="Invalid user_id"):
            await mgr.ensure_container("ABCDEF12345678901234567890ABCDEF", str(tmp_path))

    @pytest.mark.asyncio
    async def test_destroy_container_cleans_up_lock(self, tmp_path):
        """P3: Locks should be cleaned up when containers are destroyed."""
        settings = _make_settings(container_socket_base=str(tmp_path))

        routes = {
            "rm": _make_mock_process(returncode=0),
        }

        with patch("asyncio.create_subprocess_exec", _podman_router(routes)):
            mgr = ContainerManager(settings=settings)

            user_id = "abcdef12345678901234567890abcdef"
            mgr._containers[user_id] = ContainerInfo(
                container_id="c1",
                user_id=user_id,
                socket_path="/tmp/s1",
                created_at=datetime.now(timezone.utc),
                last_activity=datetime.now(timezone.utc),
            )
            mgr._locks[user_id] = asyncio.Lock()

            await mgr.destroy_container(user_id)

        assert user_id not in mgr._containers
        assert user_id not in mgr._locks

    @pytest.mark.asyncio
    async def test_max_container_count_enforced(self, tmp_path):
        """P5: Reject new containers when max count is reached."""
        settings = _make_settings(
            container_socket_base=str(tmp_path),
            container_max_count=2,
            container_idle_timeout_s=99999,
        )
        mgr = ContainerManager(settings=settings)
        mgr._initialized = True  # Skip init to avoid subprocess call

        # Fill up to max
        uid1 = "a" * 32
        uid2 = "b" * 32
        uid3 = "c" * 32
        mgr._containers[uid1] = ContainerInfo(
            container_id="c1",
            user_id=uid1,
            socket_path="/tmp/s1",
            created_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
        )
        mgr._containers[uid2] = ContainerInfo(
            container_id="c2",
            user_id=uid2,
            socket_path="/tmp/s2",
            created_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
        )

        with pytest.raises(ContainerStartError, match="Maximum container limit"):
            await mgr.ensure_container(uid3, str(tmp_path / "data"))

    @pytest.mark.asyncio
    async def test_orphaned_containers_cleaned_on_init(self, tmp_path):
        """S7: Orphaned containers should be removed on initialization."""
        settings = _make_settings(container_socket_base=str(tmp_path))

        orphan_data = [{"Id": "orphan123abc", "Names": ["yinshi-sidecar-test"]}]

        def _side_effect(*args, **kwargs):
            subcmd = args[1] if len(args) > 1 else ""
            if subcmd == "network":
                return _make_mock_process(returncode=0)
            if subcmd == "ps":
                return _make_mock_process(stdout=json.dumps(orphan_data))
            if subcmd == "rm":
                return _make_mock_process(returncode=0)
            return _make_mock_process()

        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(side_effect=_side_effect)
        ) as mock_exec:
            mgr = ContainerManager(settings=settings)
            await mgr.initialize()

        # Verify rm was called with the orphan ID
        rm_calls = [c for c in mock_exec.call_args_list if len(c.args) > 1 and c.args[1] == "rm"]
        assert len(rm_calls) == 1
        assert "orphan123abc" in rm_calls[0].args

    @pytest.mark.asyncio
    async def test_socket_dir_permissions(self, tmp_path):
        """S2: Socket directory should be created with 0o700 permissions."""
        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)

        user_id = "abcdef12345678901234567890abcdef"
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)
        socket_dir = os.path.join(socket_base, user_id)

        container_id = "perms_container_123"

        def _side_effect(*args, **kwargs):
            subcmd = args[1] if len(args) > 1 else ""
            if subcmd == "run":
                # Socket dir is created by _create_container before podman run
                with open(os.path.join(socket_dir, "sidecar.sock"), "w") as f:
                    f.write("")
                return _make_mock_process(stdout=container_id)
            if subcmd == "network":
                return _make_mock_process(returncode=0)
            if subcmd == "ps":
                return _make_mock_process(stdout="[]")
            return _make_mock_process()

        socket_path = os.path.join(socket_dir, "sidecar.sock")
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=_side_effect)),
            patch("asyncio.open_unix_connection", _ready_socket_listener(socket_path)),
        ):
            mgr = ContainerManager(settings=settings)
            mgr._socket_poll_timeout_s = 0.2
            mgr._socket_poll_interval_s = 0.05

            await mgr.ensure_container(user_id, data_dir)

        # Verify socket dir has restricted permissions
        stat = os.stat(socket_dir)
        assert oct(stat.st_mode & 0o777) == oct(0o700)

    @pytest.mark.asyncio
    async def test_run_podman_security_flags(self, tmp_path):
        """Verify podman run is called with correct security flags."""
        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)

        user_id = "abcdef12345678901234567890abcdef"
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)
        socket_dir = os.path.join(socket_base, user_id)

        def _side_effect(*args, **kwargs):
            subcmd = args[1] if len(args) > 1 else ""
            if subcmd == "run":
                os.makedirs(socket_dir, exist_ok=True)
                with open(os.path.join(socket_dir, "sidecar.sock"), "w") as f:
                    f.write("")
                return _make_mock_process(stdout="sec_container_123")
            if subcmd == "network":
                return _make_mock_process(returncode=0)
            if subcmd == "ps":
                return _make_mock_process(stdout="[]")
            return _make_mock_process()

        socket_path = os.path.join(socket_dir, "sidecar.sock")
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=_side_effect)) as mock_exec,
            patch("asyncio.open_unix_connection", _ready_socket_listener(socket_path)),
        ):
            mgr = ContainerManager(settings=settings)
            await mgr.ensure_container(user_id, data_dir)

        # Find the "run" call and verify security flags
        run_calls = [c for c in mock_exec.call_args_list if len(c.args) > 1 and c.args[1] == "run"]
        assert len(run_calls) == 1
        run_args = run_calls[0].args
        assert "--cap-drop" in run_args
        assert "ALL" in run_args
        assert "--security-opt" in run_args
        assert "no-new-privileges" in run_args
        assert "--memory" in run_args
        assert "--pids-limit" in run_args
        assert "--network" in run_args
        assert "--replace" in run_args
        assert "--user" in run_args
        assert "0:0" in run_args
        assert "HOME=/tmp" in run_args

    @pytest.mark.asyncio
    async def test_run_podman_timeout_ignores_process_lookup_error(self, tmp_path):
        """Timeout cleanup should still raise ContainerStartError when Podman already exited."""
        settings = _make_settings(container_socket_base=str(tmp_path))

        async def _wait_forever() -> int:
            await asyncio.sleep(60)
            return 0

        proc = AsyncMock()
        proc.wait = AsyncMock(side_effect=_wait_forever)
        proc.stdout = AsyncMock()
        proc.stdout.read = AsyncMock(return_value=b"")
        proc.stderr = AsyncMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.returncode = None
        proc.kill = MagicMock(side_effect=ProcessLookupError())

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            mgr = ContainerManager(settings=settings)
            with pytest.raises(ContainerStartError, match="Podman command timed out"):
                await mgr._run_podman("run", timeout=0.01)

    @pytest.mark.asyncio
    async def test_podman_not_found_raises(self, tmp_path):
        """If podman binary is missing, raise ContainerStartError."""
        settings = _make_settings(container_socket_base=str(tmp_path))

        async def _raise_fnf(*args, **kwargs):
            raise FileNotFoundError("podman not found")

        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=_raise_fnf)):
            mgr = ContainerManager(settings=settings)
            with pytest.raises(ContainerStartError, match="podman binary not found"):
                await mgr.initialize()

    @pytest.mark.asyncio
    async def test_missing_image_raises(self, tmp_path):
        """Startup preflight should fail when the configured image is unavailable."""
        settings = _make_settings(container_socket_base=str(tmp_path))

        def _side_effect(*args, **kwargs):
            subcmd = args[1] if len(args) > 1 else ""
            if subcmd == "--version":
                return _make_mock_process(stdout="podman version 5.0.0")
            if subcmd == "network":
                return _make_mock_process(returncode=0)
            if subcmd == "image":
                return _make_mock_process(returncode=1)
            if subcmd == "ps":
                return _make_mock_process(stdout="[]")
            return _make_mock_process()

        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=_side_effect)):
            mgr = ContainerManager(settings=settings)
            with pytest.raises(ContainerStartError, match="Configured sidecar image"):
                await mgr.initialize()

    @pytest.mark.asyncio
    async def test_initialize_restricts_socket_base_permissions(self, tmp_path):
        """Startup preflight should enforce 0o700 on the shared socket base."""
        socket_base = tmp_path / "sockets"
        socket_base.mkdir(mode=0o755)
        settings = _make_settings(container_socket_base=str(socket_base))

        def _side_effect(*args, **kwargs):
            subcmd = args[1] if len(args) > 1 else ""
            if subcmd == "--version":
                return _make_mock_process(stdout="podman version 5.0.0")
            if subcmd == "network":
                return _make_mock_process(returncode=0)
            if subcmd == "image":
                return _make_mock_process(returncode=0)
            if subcmd == "ps":
                return _make_mock_process(stdout="[]")
            return _make_mock_process()

        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=_side_effect)):
            mgr = ContainerManager(settings=settings)
            await mgr.initialize()

        stat = os.stat(socket_base)
        assert oct(stat.st_mode & 0o777) == oct(0o700)
