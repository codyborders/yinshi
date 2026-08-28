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
from yinshi.services.container import (
    _PODMAN_RUN_TIMEOUT_S,
    ContainerInfo,
    ContainerManager,
    ContainerMount,
)
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

    def test_absolute_descendant_is_inside_root(self):
        assert is_path_inside("/var/lib/users/abc", "/") is True


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


class TestPiSessionRuntimePaths:
    """Tests for durable Pi session file paths in workspace runtimes."""

    def test_resolve_context_returns_workspace_pi_session_file(self, tmp_path):
        from yinshi.services.sidecar_runtime import _workspace_pi_session_runtime_file
        from yinshi.tenant import TenantContext

        tenant = TenantContext(
            user_id="abcdef12345678901234567890abcdef",
            email="test@example.com",
            data_dir=str(tmp_path / "users" / "tenant"),
            db_path=str(tmp_path / "users" / "tenant" / "tenant.db"),
        )
        Path(tenant.data_dir).mkdir(parents=True)

        session_file = _workspace_pi_session_runtime_file(
            tenant,
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            container_enabled=True,
            narrow_mounts=True,
        )

        assert session_file.startswith("/home/yinshi/.yinshi/pi-sessions/")
        assert session_file.endswith(".jsonl")
        host_session_dir = (
            Path(tenant.data_dir)
            / "runtime"
            / "workspaces"
            / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            / "home"
            / ".yinshi"
            / "pi-sessions"
        )
        assert host_session_dir.is_dir()

    def test_delete_workspace_pi_sessions_removes_only_session_directory(self, tmp_path):
        from yinshi.services.sidecar_runtime import delete_workspace_pi_sessions
        from yinshi.tenant import TenantContext

        tenant = TenantContext(
            user_id="abcdef12345678901234567890abcdef",
            email="test@example.com",
            data_dir=str(tmp_path / "users" / "tenant"),
            db_path=str(tmp_path / "users" / "tenant" / "tenant.db"),
        )
        session_dir = (
            Path(tenant.data_dir)
            / "runtime"
            / "workspaces"
            / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            / "home"
            / ".yinshi"
            / "pi-sessions"
        )
        session_dir.mkdir(parents=True)
        (session_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
        keep_file = session_dir.parent / "keep.txt"
        keep_file.write_text("keep", encoding="utf-8")

        delete_workspace_pi_sessions(tenant, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

        assert not session_dir.exists()
        assert keep_file.read_text(encoding="utf-8") == "keep"

    def test_delete_workspace_pi_sessions_refuses_symlink_parent(self, tmp_path):
        from yinshi.services.sidecar_runtime import delete_workspace_pi_sessions
        from yinshi.tenant import TenantContext

        if not hasattr(os, "symlink"):
            pytest.skip("symlink support is unavailable")

        tenant = TenantContext(
            user_id="abcdef12345678901234567890abcdef",
            email="test@example.com",
            data_dir=str(tmp_path / "users" / "tenant"),
            db_path=str(tmp_path / "users" / "tenant" / "tenant.db"),
        )
        first_workspace_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        second_workspace_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        first_home = Path(tenant.data_dir) / "runtime" / "workspaces" / first_workspace_id / "home"
        second_session_file = (
            Path(tenant.data_dir)
            / "runtime"
            / "workspaces"
            / second_workspace_id
            / "home"
            / ".yinshi"
            / "pi-sessions"
            / "session.jsonl"
        )
        first_home.mkdir(parents=True)
        second_session_file.parent.mkdir(parents=True)
        second_session_file.write_text("{}\n", encoding="utf-8")
        (first_home / ".yinshi").symlink_to(
            f"../../{second_workspace_id}/home/.yinshi",
            target_is_directory=True,
        )

        delete_workspace_pi_sessions(tenant, first_workspace_id)

        assert (first_home / ".yinshi").is_symlink()
        assert second_session_file.read_text(encoding="utf-8") == "{}\n"


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
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    proc.stdout = AsyncMock()
    proc.stdout.read = AsyncMock(return_value=stdout.encode())
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=stderr.encode())
    proc.returncode = returncode
    proc.kill = MagicMock()
    return proc


def _write_detached_run_outputs(args, kwargs, container_id: str) -> None:
    """Write Podman's detached-run id to the mocked stdout and cidfile outputs."""
    stdout_target = kwargs.get("stdout")
    if hasattr(stdout_target, "write"):
        stdout_target.write(container_id.encode())
        stdout_target.flush()

    if "--cidfile" in args:
        cidfile_index = args.index("--cidfile") + 1
        cidfile_path = args[cidfile_index]
        Path(cidfile_path).write_text(container_id, encoding="utf-8")


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
    reader.readline = AsyncMock(return_value=b'{"id":"init","type":"init_status","success":true}\n')
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
        "container_mount_mode": "narrow",
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
                # Simulate socket file appearing after container start.
                _write_detached_run_outputs(args, kwargs, container_id)
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
                mounts=(
                    ContainerMount(source_path=os.path.realpath(data_dir), target_path="/data"),
                ),
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
                _write_detached_run_outputs(args, kwargs, new_container_id)
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
            patch(
                "asyncio.create_subprocess_exec", AsyncMock(side_effect=_side_effect)
            ) as mock_exec,
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
    async def test_activity_acquisition_waits_for_destroy_before_runtime_lookup(self, tmp_path):
        """Activity acquisition must not reserve a runtime already being removed."""
        settings = _make_settings(container_socket_base=str(tmp_path))
        mgr = ContainerManager(settings=settings)
        user_id = "abcdef12345678901234567890abcdef"
        info = ContainerInfo(
            container_id="destroying-container",
            user_id=user_id,
            socket_path="/tmp/destroying.sock",
        )
        mgr._containers[user_id] = info
        removal_started = asyncio.Event()
        finish_removal = asyncio.Event()
        active_counts_at_removal: list[int] = []

        async def _remove(container_id: str) -> bool:
            assert container_id == info.container_id
            active_counts_at_removal.append(info.active_request_count)
            removal_started.set()
            await finish_removal.wait()
            return True

        mgr._remove_container = AsyncMock(side_effect=_remove)
        destroy_task = asyncio.create_task(mgr.destroy_container(user_id))
        await removal_started.wait()
        acquire_task = asyncio.create_task(mgr.acquire_activity(user_id))
        await asyncio.sleep(0)

        assert not acquire_task.done()
        finish_removal.set()
        destroyed, reservation = await asyncio.gather(destroy_task, acquire_task)

        assert destroyed is True
        assert reservation is None
        assert active_counts_at_removal == [0]
        assert user_id not in mgr._containers

    @pytest.mark.asyncio
    async def test_activity_reservation_blocks_destroy_until_exact_release(self, tmp_path):
        """Destroy must reject an acquired runtime until its reservation is released."""
        settings = _make_settings(container_socket_base=str(tmp_path))
        mgr = ContainerManager(settings=settings)
        user_id = "abcdef12345678901234567890abcdef"
        info = ContainerInfo(
            container_id="active-container",
            user_id=user_id,
            socket_path="/tmp/active.sock",
        )
        mgr._containers[user_id] = info
        mgr._remove_container = AsyncMock(return_value=True)

        reservation = await mgr.acquire_activity(user_id)

        assert reservation is not None
        assert await mgr.destroy_container(user_id) is False
        mgr._remove_container.assert_not_awaited()

        await mgr.release_activity(reservation)

        assert info.active_request_count == 0
        assert await mgr.destroy_container(user_id) is True
        mgr._remove_container.assert_awaited_once_with(info.container_id)

    @pytest.mark.asyncio
    async def test_stale_activity_release_does_not_change_replacement_generation(self, tmp_path):
        """A stale reservation must release only the runtime generation it acquired."""
        settings = _make_settings(container_socket_base=str(tmp_path))
        mgr = ContainerManager(settings=settings)
        user_id = "abcdef12345678901234567890abcdef"
        original = ContainerInfo(
            container_id="original-container",
            user_id=user_id,
            socket_path="/tmp/original.sock",
        )
        replacement = ContainerInfo(
            container_id="replacement-container",
            user_id=user_id,
            socket_path="/tmp/replacement.sock",
            active_request_count=1,
        )
        mgr._containers[user_id] = original

        reservation = await mgr.acquire_activity(user_id)
        assert reservation is not None
        mgr._containers[user_id] = replacement

        await mgr.release_activity(reservation)

        assert original.active_request_count == 0
        assert replacement.active_request_count == 1

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

        mgr._containers[user_id].protected_operation_deadlines["oauth:flow-1"] = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)
        mgr._containers[user_id].last_activity = old_time
        with patch(
            "asyncio.create_subprocess_exec",
            _podman_router({"rm": _make_mock_process(returncode=0)}),
        ):
            count = await mgr.reap_idle()

        assert count == 1
        assert user_id not in mgr._containers

    @pytest.mark.asyncio
    async def test_reap_idle_rechecks_activity_after_waiting_for_runtime(self, tmp_path):
        """Activity started before lock acquisition must cancel pending reaping."""
        settings = _make_settings(
            container_socket_base=str(tmp_path),
            container_idle_timeout_s=60,
        )
        mgr = ContainerManager(settings=settings)
        mgr._initialized = True
        user_id = "0" * 32
        old_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        mounts = mgr._default_mounts(str(tmp_path))
        info = ContainerInfo(
            container_id="idle-before-wait",
            user_id=user_id,
            socket_path="/tmp/idle-before-wait.sock",
            mounts=mounts,
            created_at=old_time,
            last_activity=old_time,
        )
        mgr._containers[user_id] = info
        inspect_started = asyncio.Event()
        release_inspect = asyncio.Event()

        async def _is_running(container_id: str) -> bool:
            assert container_id == info.container_id
            inspect_started.set()
            await release_inspect.wait()
            return True

        mgr._is_running = AsyncMock(side_effect=_is_running)
        mgr._remove_container = AsyncMock(return_value=None)
        ensure_task = asyncio.create_task(mgr.ensure_container(user_id, str(tmp_path)))
        await inspect_started.wait()
        reap_task = asyncio.create_task(mgr.reap_idle())
        await asyncio.sleep(0)
        mgr.begin_activity(user_id)
        release_inspect.set()

        ensured_info, count = await asyncio.gather(ensure_task, reap_task)

        assert ensured_info is info
        assert count == 0
        assert mgr._containers[user_id] is info
        mgr._remove_container.assert_not_awaited()

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
    async def test_reap_idle_keeps_quota_entry_when_podman_removal_fails(self, tmp_path):
        """Failed Podman removal must keep the runtime tracked against quota."""
        settings = _make_settings(
            container_socket_base=str(tmp_path),
            container_idle_timeout_s=60,
            container_max_count=1,
        )
        mgr = ContainerManager(settings=settings)
        user_id = "0" * 32
        old_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        info = ContainerInfo(
            container_id="remove-failed",
            user_id=user_id,
            socket_path="/tmp/remove-failed.sock",
            created_at=old_time,
            last_activity=old_time,
        )
        mgr._containers[user_id] = info

        with patch(
            "asyncio.create_subprocess_exec",
            _podman_router({"rm": _make_mock_process(returncode=1)}),
        ):
            count = await mgr.reap_idle()

        assert count == 0
        assert mgr._containers[user_id] is info

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
                # Container starts but socket never appears.
                _write_detached_run_outputs(args, kwargs, container_id)
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
    async def test_destroy_container_reports_missing_runtime_safe_to_delete(self, tmp_path):
        """Destroy should report success when no runtime exists."""
        settings = _make_settings(container_socket_base=str(tmp_path))
        mgr = ContainerManager(settings=settings)

        safe_to_delete = await mgr.destroy_container("a" * 32)

        assert safe_to_delete is True

    @pytest.mark.asyncio
    async def test_destroy_container_reports_busy_runtime_not_removed(self, tmp_path):
        """Destroy should report a busy current runtime without untracking it."""
        settings = _make_settings(container_socket_base=str(tmp_path))
        mgr = ContainerManager(settings=settings)
        user_id = "a" * 32
        info = ContainerInfo(
            container_id="busy-container",
            user_id=user_id,
            socket_path="/tmp/busy.sock",
            active_request_count=1,
        )
        mgr._containers[user_id] = info
        mgr._remove_container = AsyncMock(return_value=True)

        removed = await mgr.destroy_container(user_id)

        assert removed is False
        assert mgr._containers[user_id] is info
        mgr._remove_container.assert_not_awaited()

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
    async def test_concurrent_creation_reserves_container_quota(self, tmp_path):
        """Different runtime keys must not start beyond the configured quota."""
        settings = _make_settings(
            container_socket_base=str(tmp_path),
            container_max_count=1,
            container_idle_timeout_s=99999,
        )
        mgr = ContainerManager(settings=settings)
        mgr._initialized = True
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        create_calls: list[str] = []

        async def _create(
            user_id: str,
            mounts: tuple[ContainerMount, ...],
            *,
            runtime_id: str | None = None,
            environment: tuple[tuple[str, str], ...] = (),
        ) -> ContainerInfo:
            del mounts, environment
            create_calls.append(user_id)
            if len(create_calls) == 1:
                first_started.set()
                await release_first.wait()
            return ContainerInfo(
                container_id=f"container-{user_id}",
                user_id=user_id,
                socket_path=str(tmp_path / user_id / "sidecar.sock"),
                runtime_id=runtime_id,
            )

        mgr._create_container = AsyncMock(side_effect=_create)
        first_user_id = "a" * 32
        second_user_id = "b" * 32
        first_task = asyncio.create_task(mgr.ensure_container(first_user_id, str(tmp_path)))
        await first_started.wait()

        with pytest.raises(ContainerStartError, match="Maximum container limit"):
            await mgr.ensure_container(second_user_id, str(tmp_path))

        release_first.set()
        first_info = await first_task

        assert first_info.user_id == first_user_id
        assert create_calls == [first_user_id]
        assert list(mgr._containers) == [first_user_id]

    @pytest.mark.asyncio
    async def test_destroy_waits_for_creation_then_removes_current_container(self, tmp_path):
        """Destroy requested during creation should remove the new current runtime."""
        settings = _make_settings(container_socket_base=str(tmp_path))
        mgr = ContainerManager(settings=settings)
        mgr._initialized = True
        creation_started = asyncio.Event()
        release_creation = asyncio.Event()
        user_id = "a" * 32
        created = ContainerInfo(
            container_id="created-container",
            user_id=user_id,
            socket_path=str(tmp_path / user_id / "created.sock"),
        )

        async def _create(
            selected_user_id: str,
            mounts: tuple[ContainerMount, ...],
            *,
            runtime_id: str | None = None,
            environment: tuple[tuple[str, str], ...] = (),
        ) -> ContainerInfo:
            del mounts, runtime_id, environment
            assert selected_user_id == user_id
            creation_started.set()
            await release_creation.wait()
            return created

        mgr._create_container = AsyncMock(side_effect=_create)
        mgr._remove_container = AsyncMock(return_value=True)
        ensure_task = asyncio.create_task(mgr.ensure_container(user_id, str(tmp_path)))
        await creation_started.wait()
        destroy_task = asyncio.create_task(mgr.destroy_container(user_id))
        await asyncio.sleep(0)

        assert not destroy_task.done()
        release_creation.set()
        ensured, safe_to_delete = await asyncio.gather(ensure_task, destroy_task)

        assert ensured is created
        assert safe_to_delete is True
        assert user_id not in mgr._containers
        mgr._remove_container.assert_awaited_once_with(created.container_id)

    @pytest.mark.asyncio
    async def test_max_container_count_allows_reusing_existing_container(self, tmp_path):
        """Container quota should block only new containers, not reuse of an existing one."""
        settings = _make_settings(
            container_socket_base=str(tmp_path),
            container_max_count=1,
            container_idle_timeout_s=99999,
        )
        mgr = ContainerManager(settings=settings)
        mgr._initialized = True
        user_id = "a" * 32
        mounts = mgr._default_mounts(str(tmp_path))
        existing_info = ContainerInfo(
            container_id="existing",
            user_id=user_id,
            socket_path="/tmp/socket",
            mounts=mounts,
            created_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
        )
        mgr._containers[user_id] = existing_info
        mgr._is_running = AsyncMock(return_value=True)

        info = await mgr.ensure_container(user_id, str(tmp_path))

        assert info is existing_info
        assert mgr._is_running.await_count == 1

    @pytest.mark.asyncio
    async def test_initialize_is_serialized(self, tmp_path):
        """Concurrent cold-starts should run Podman initialization once."""
        settings = _make_settings(container_socket_base=str(tmp_path))
        first_init_started = asyncio.Event()
        release_first_init = asyncio.Event()
        network_call_count = 0

        async def _network_once() -> None:
            nonlocal network_call_count
            network_call_count += 1
            first_init_started.set()
            await release_first_init.wait()

        mgr = ContainerManager(settings=settings)
        mgr._verify_podman_available = AsyncMock(return_value=None)
        mgr._ensure_network = AsyncMock(side_effect=_network_once)
        mgr._ensure_image = AsyncMock(return_value=None)
        mgr._cleanup_orphaned_containers = AsyncMock(return_value=None)

        first_task = asyncio.create_task(mgr.initialize())
        await first_init_started.wait()
        second_task = asyncio.create_task(mgr.initialize())
        await asyncio.sleep(0)
        release_first_init.set()

        await asyncio.gather(first_task, second_task)

        assert network_call_count == 1
        assert mgr._verify_podman_available.await_count == 1
        assert mgr._cleanup_orphaned_containers.await_count == 1
        assert mgr._initialized is True

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
                # Socket dir is created by _create_container before podman run.
                _write_detached_run_outputs(args, kwargs, container_id)
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
    async def test_ensure_container_uses_explicit_narrow_mounts(self, tmp_path):
        """Narrow mode should mount only requested tenant paths, not the data root."""
        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)

        user_id = "abcdef12345678901234567890abcdef"
        data_dir = tmp_path / "data"
        workspace_dir = data_dir / "repos" / "repo1" / ".worktrees" / "branch"
        workspace_dir.mkdir(parents=True)
        socket_dir = os.path.join(socket_base, user_id)

        def _side_effect(*args, **kwargs):
            subcmd = args[1] if len(args) > 1 else ""
            if subcmd == "run":
                container_id = "narrow_container_123"
                _write_detached_run_outputs(args, kwargs, container_id)
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
            patch(
                "asyncio.create_subprocess_exec", AsyncMock(side_effect=_side_effect)
            ) as mock_exec,
            patch("asyncio.open_unix_connection", _ready_socket_listener(socket_path)),
        ):
            mgr = ContainerManager(settings=settings)
            await mgr.ensure_container(
                user_id,
                str(data_dir),
                mounts=(
                    ContainerMount(
                        source_path=str(workspace_dir),
                        target_path="/workspace",
                        read_only=False,
                    ),
                ),
            )

        run_call = next(call for call in mock_exec.call_args_list if call.args[1] == "run")
        run_args = run_call.args
        assert f"{workspace_dir.resolve()}:/workspace:rw" in run_args
        assert f"{data_dir.resolve()}:/data:rw" not in run_args

    @pytest.mark.asyncio
    async def test_ensure_container_uses_workspace_runtime_identity(self, tmp_path):
        """Workspace runtimes should use separate sockets, names, labels, and env."""
        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)
        user_id = "abcdef12345678901234567890abcdef"
        runtime_id = "b" * 32
        data_dir = tmp_path / "data"
        workspace_dir = data_dir / "repos" / "repo1" / ".worktrees" / "branch"
        workspace_dir.mkdir(parents=True)
        socket_dir = Path(socket_base) / user_id / runtime_id
        container_id = "workspace_runtime_123"

        def _side_effect(*args, **kwargs):
            subcmd = args[1] if len(args) > 1 else ""
            if subcmd == "run":
                _write_detached_run_outputs(args, kwargs, container_id)
                socket_dir.mkdir(parents=True, exist_ok=True)
                (socket_dir / "sidecar.sock").write_text("", encoding="utf-8")
                return _make_mock_process(stdout=container_id)
            if subcmd == "network":
                return _make_mock_process(returncode=0)
            if subcmd == "ps":
                return _make_mock_process(stdout="[]")
            return _make_mock_process()

        socket_path = str(socket_dir / "sidecar.sock")
        with (
            patch(
                "asyncio.create_subprocess_exec", AsyncMock(side_effect=_side_effect)
            ) as mock_exec,
            patch("asyncio.open_unix_connection", _ready_socket_listener(socket_path)),
        ):
            mgr = ContainerManager(settings=settings)
            info = await mgr.ensure_container(
                user_id,
                str(data_dir),
                mounts=(
                    ContainerMount(
                        source_path=str(workspace_dir),
                        target_path="/workspace",
                        read_only=False,
                    ),
                ),
                runtime_id=runtime_id,
                environment={"HOME": "/home/yinshi", "PATH": "/home/yinshi/bin:/usr/bin"},
            )

        run_call = next(call for call in mock_exec.call_args_list if call.args[1] == "run")
        run_args = run_call.args
        assert info.runtime_id == runtime_id
        assert info.socket_path == socket_path
        assert f"yinshi-sidecar-{user_id[:12]}-{runtime_id}" in run_args
        assert f"yinshi.runtime_id={runtime_id}" in run_args
        assert "HOME=/home/yinshi" in run_args
        assert "PATH=/home/yinshi/bin:/usr/bin" in run_args

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
                container_id = "sec_container_123"
                _write_detached_run_outputs(args, kwargs, container_id)
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
            patch(
                "asyncio.create_subprocess_exec", AsyncMock(side_effect=_side_effect)
            ) as mock_exec,
            patch("asyncio.open_unix_connection", _ready_socket_listener(socket_path)),
            patch("os.getuid", return_value=1001),
            patch("os.getgid", return_value=1002),
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
        assert "--userns" in run_args
        assert "keep-id" in run_args
        assert "--user" in run_args
        assert "1001:1002" in run_args
        assert "0:0" not in run_args
        assert "HOME=/tmp" in run_args

    @pytest.mark.asyncio
    async def test_run_podman_preserves_large_output(self, tmp_path):
        """Podman JSON output should not be truncated by pipe reads."""
        settings = _make_settings(container_socket_base=str(tmp_path))
        large_stdout = "x" * (80 * 1024)
        proc = _make_mock_process(stdout=large_stdout)

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            mgr = ContainerManager(settings=settings)
            _, stdout, _ = await mgr._run_podman("ps", "--format", "json")

        assert stdout == large_stdout
        assert proc.communicate.await_count == 1

    @pytest.mark.asyncio
    async def test_create_container_uses_extended_podman_run_timeout(self, tmp_path):
        """Container creation should allow slow cold-starting rootless Podman runs."""
        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)
        user_id = "abcdef12345678901234567890abcdef"
        mgr = ContainerManager(settings=settings)
        cidfile_path = os.path.join(socket_base, user_id, "container.cid")
        mgr._run_podman_waiting_for_exit = AsyncMock(return_value=(0, "container_123", ""))
        mgr._wait_for_socket = AsyncMock(return_value=None)

        await mgr._create_container(user_id, mounts=())

        run_call = mgr._run_podman_waiting_for_exit.await_args
        assert run_call is not None
        assert run_call.args[0] == "run"
        assert "--cidfile" in run_call.args
        assert cidfile_path in run_call.args
        assert run_call.kwargs["timeout"] == _PODMAN_RUN_TIMEOUT_S

    @pytest.mark.asyncio
    async def test_create_container_cleans_started_container_when_socket_not_ready(self, tmp_path):
        """Readiness failure must remove the started container and its cidfile."""
        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)
        user_id = "abcdef12345678901234567890abcdef"
        container_id = "unready_container_123"
        cidfile_path = Path(socket_base) / user_id / "container.cid"
        mgr = ContainerManager(settings=settings)
        mgr._initialized = True

        async def _run(*args, **kwargs):
            del args, kwargs
            cidfile_path.write_text(container_id, encoding="utf-8")
            return 0, container_id, ""

        mgr._run_podman_waiting_for_exit = AsyncMock(side_effect=_run)
        mgr._wait_for_socket = AsyncMock(side_effect=ContainerNotReadyError("not ready"))
        mgr._remove_container = AsyncMock(return_value=True)

        with pytest.raises(ContainerNotReadyError, match="not ready"):
            await mgr.ensure_container(user_id, str(tmp_path))

        mgr._remove_container.assert_awaited_once_with(container_id)
        assert not cidfile_path.exists()
        assert not mgr._pending_container_keys
        assert user_id not in mgr._containers

    @pytest.mark.asyncio
    async def test_create_container_does_not_wait_on_detached_run_pipe_eof(self, tmp_path):
        """Detached Podman run should finish when the Podman CLI exits, not when pipes close."""
        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)
        user_id = "abcdef12345678901234567890abcdef"
        container_id = "detached_container_123"

        async def _communicate_forever() -> tuple[bytes, bytes]:
            await asyncio.sleep(60)
            return b"", b""

        run_process = None

        def _side_effect(*args, **kwargs):
            nonlocal run_process
            subcmd = args[1] if len(args) > 1 else ""
            if subcmd == "run":
                _write_detached_run_outputs(args, kwargs, container_id)
                run_process = _make_mock_process(stdout=container_id)
                run_process.communicate = AsyncMock(side_effect=_communicate_forever)
                return run_process
            return _make_mock_process()

        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=_side_effect)):
            mgr = ContainerManager(settings=settings)
            mgr._wait_for_socket = AsyncMock(return_value=None)

            info = await mgr._create_container(user_id, mounts=())

        assert info.container_id == container_id
        assert run_process is not None
        assert run_process.wait.await_count == 1
        assert run_process.communicate.await_count == 0

    @pytest.mark.asyncio
    async def test_cancelled_ensure_cleans_container_started_by_podman(self, tmp_path):
        """Cancelling creation must remove the exact container reported by its cidfile."""
        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)
        user_id = "abcdef12345678901234567890abcdef"
        container_id = "cancelled_container_123"
        run_started = asyncio.Event()
        run_wait_count = 0
        removed_ids: list[str] = []
        run_process = AsyncMock()
        run_process.returncode = None

        async def _wait_for_run() -> int:
            nonlocal run_wait_count
            run_wait_count += 1
            if run_wait_count == 1:
                run_started.set()
                await asyncio.sleep(60)
            return 0

        run_process.wait = AsyncMock(side_effect=_wait_for_run)

        async def _start_process(*args, **kwargs):
            del kwargs
            subcommand = args[1]
            if subcommand == "run":
                cidfile_path = Path(args[args.index("--cidfile") + 1])
                run_process.kill = MagicMock(
                    side_effect=lambda: cidfile_path.write_text(container_id, encoding="utf-8")
                )
                return run_process
            if subcommand == "rm":
                removed_ids.append(args[-1])
            return _make_mock_process()

        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=_start_process),
        ):
            mgr = ContainerManager(settings=settings)
            mgr._initialized = True
            task = asyncio.create_task(mgr.ensure_container(user_id, str(tmp_path)))
            await run_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run_process.kill.assert_called_once_with()
        assert removed_ids == [container_id]
        assert not (Path(socket_base) / user_id / "container.cid").exists()
        assert not mgr._pending_container_keys
        assert user_id not in mgr._containers

    @pytest.mark.asyncio
    async def test_cancelled_ensure_cleans_started_container_by_validated_name(self, tmp_path):
        """Cancellation without a container ID should clean the deterministic name."""
        socket_base = str(tmp_path / "sockets")
        settings = _make_settings(container_socket_base=socket_base)
        user_id = "abcdef12345678901234567890abcdef"
        run_started = asyncio.Event()
        run_wait_count = 0
        removed_names: list[str] = []
        run_process = AsyncMock()
        run_process.returncode = None
        run_process.kill = MagicMock()

        async def _wait_for_run() -> int:
            nonlocal run_wait_count
            run_wait_count += 1
            if run_wait_count == 1:
                run_started.set()
                await asyncio.sleep(60)
            return 0

        run_process.wait = AsyncMock(side_effect=_wait_for_run)

        async def _start_process(*args, **kwargs):
            del kwargs
            subcommand = args[1]
            if subcommand == "run":
                return run_process
            if subcommand == "rm":
                removed_names.append(args[-1])
            return _make_mock_process()

        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=_start_process),
        ):
            mgr = ContainerManager(settings=settings)
            mgr._initialized = True
            task = asyncio.create_task(mgr.ensure_container(user_id, str(tmp_path)))
            await run_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert removed_names == [f"yinshi-sidecar-{user_id}"]
        assert not mgr._pending_container_keys
        assert user_id not in mgr._containers

    @pytest.mark.asyncio
    async def test_run_podman_timeout_ignores_process_lookup_error(self, tmp_path):
        """Timeout cleanup should still raise ContainerStartError when Podman already exited."""
        settings = _make_settings(container_socket_base=str(tmp_path))

        async def _communicate_forever() -> tuple[bytes, bytes]:
            await asyncio.sleep(60)
            return b"", b""

        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=_communicate_forever)
        proc.wait = AsyncMock(return_value=0)
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
    async def test_initialize_recreates_internal_network(self, tmp_path):
        """Startup should repair old internal-only networks so containers can reach providers."""
        settings = _make_settings(container_socket_base=str(tmp_path))
        internal_network_inspect = json.dumps([{"name": "yinshi-sidecar-net", "internal": True}])

        def _side_effect(*args, **kwargs):
            subcmd = args[1] if len(args) > 1 else ""
            if subcmd == "--version":
                return _make_mock_process(stdout="podman version 5.0.0")
            if subcmd == "network":
                if args[2] == "inspect":
                    return _make_mock_process(returncode=0, stdout=internal_network_inspect)
                if args[2] == "rm":
                    return _make_mock_process(returncode=0)
                if args[2] == "create":
                    return _make_mock_process(returncode=0)
            if subcmd == "image":
                return _make_mock_process(returncode=0)
            if subcmd == "ps":
                return _make_mock_process(stdout="[]")
            return _make_mock_process()

        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=_side_effect),
        ) as mock_exec:
            mgr = ContainerManager(settings=settings)
            await mgr.initialize()

        network_calls = [
            call.args[2]
            for call in mock_exec.call_args_list
            if len(call.args) > 2 and call.args[1] == "network"
        ]
        assert network_calls == ["inspect", "rm", "create"]

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
