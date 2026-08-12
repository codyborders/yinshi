"""Privacy tests for container lifecycle logs."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yinshi.exceptions import ContainerStartError
from yinshi.services.container import (
    ContainerActivityReservation,
    ContainerInfo,
    ContainerManager,
)

_SENTINEL_USER_ID = "deadbeefdeadbeefdeadbeefdeadbeef"
_SENTINEL_RUNTIME_ID = "cafebabecafebabecafebabecafebabe"
_SENTINEL_CONTAINER_KEY = f"{_SENTINEL_USER_ID}:{_SENTINEL_RUNTIME_ID}"
_SENTINEL_CONTAINER_ID = "feedface" * 8
_SENTINEL_LEASE_KEY = "private-lease-sentinel"
_SENTINEL_EXCEPTION = "private-exception-sentinel"


def _manager(tmp_path: Path) -> ContainerManager:
    settings = MagicMock()
    settings.container_socket_base = str(tmp_path / "private-path-sentinel")
    settings.container_idle_timeout_s = 1
    settings.container_cpu_quota = 50000
    settings.container_memory_limit = "256m"
    settings.container_pids_limit = 256
    settings.container_image = "yinshi-sidecar:latest"
    return ContainerManager(settings)


def _info(tmp_path: Path) -> ContainerInfo:
    return ContainerInfo(
        container_id=_SENTINEL_CONTAINER_ID,
        user_id=_SENTINEL_USER_ID,
        runtime_id=_SENTINEL_RUNTIME_ID,
        socket_path=str(tmp_path / "private-path-sentinel" / "sidecar.sock"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        "orphan_removed",
        "orphan_cleanup_failed",
        "acquire_missing",
        "release_unmatched",
        "begin_missing",
        "end_missing",
        "end_unmatched",
        "protect_missing",
        "unprotect_missing",
        "destroy",
        "reap",
        "destroy_all",
        "remove",
        "start",
        "discard_file_failed",
        "network_created",
        "network_recreated",
        "reaper_count",
    ],
)
async def test_container_logs_exclude_runtime_sentinels(
    event: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Container logs must expose events, not tenant or runtime values."""
    manager = _manager(tmp_path)
    info = _info(tmp_path)
    caplog.set_level(logging.INFO, logger="yinshi.services.container")

    if event == "orphan_removed":
        manager._run_podman = AsyncMock(
            return_value=(0, json.dumps([{"Id": _SENTINEL_CONTAINER_ID}]), "")
        )
        await manager._cleanup_orphaned_containers()
    elif event == "orphan_cleanup_failed":
        manager._run_podman = AsyncMock(side_effect=ContainerStartError(_SENTINEL_EXCEPTION))
        await manager._cleanup_orphaned_containers()
    elif event == "acquire_missing":
        await manager.acquire_activity(_SENTINEL_USER_ID, runtime_id=_SENTINEL_RUNTIME_ID)
    elif event == "release_unmatched":
        await manager.release_activity(ContainerActivityReservation(_SENTINEL_CONTAINER_KEY, info))
    elif event == "begin_missing":
        manager.begin_activity(_SENTINEL_USER_ID, runtime_id=_SENTINEL_RUNTIME_ID)
    elif event == "end_missing":
        manager.end_activity(_SENTINEL_USER_ID, runtime_id=_SENTINEL_RUNTIME_ID)
    elif event == "end_unmatched":
        manager._containers[_SENTINEL_CONTAINER_KEY] = info
        manager.end_activity(_SENTINEL_USER_ID, runtime_id=_SENTINEL_RUNTIME_ID)
    elif event == "protect_missing":
        manager.protect(
            _SENTINEL_USER_ID,
            _SENTINEL_LEASE_KEY,
            30,
            runtime_id=_SENTINEL_RUNTIME_ID,
        )
    elif event == "unprotect_missing":
        manager.unprotect(
            _SENTINEL_USER_ID,
            _SENTINEL_LEASE_KEY,
            runtime_id=_SENTINEL_RUNTIME_ID,
        )
    elif event == "destroy":
        manager._containers[_SENTINEL_CONTAINER_KEY] = info
        manager._remove_container = AsyncMock(return_value=True)
        await manager.destroy_container(_SENTINEL_USER_ID, runtime_id=_SENTINEL_RUNTIME_ID)
    elif event == "reap":
        info.last_activity = datetime.now(timezone.utc) - timedelta(seconds=10)
        manager._containers[_SENTINEL_CONTAINER_KEY] = info
        manager._remove_container = AsyncMock(return_value=True)
        assert await manager.reap_idle() == 1
    elif event == "destroy_all":
        manager._containers[_SENTINEL_CONTAINER_KEY] = info
        manager._remove_container = AsyncMock(return_value=True)
        await manager.destroy_all()
    elif event == "remove":
        manager._run_podman = AsyncMock(return_value=(0, "", ""))
        assert await manager._remove_container(_SENTINEL_CONTAINER_ID) is True
    elif event == "start":
        manager._prepare_socket_dir = MagicMock()
        manager._remove_stale_file = MagicMock()
        manager._discard_runtime_file = MagicMock()
        manager._run_podman_waiting_for_exit = AsyncMock(
            return_value=(0, _SENTINEL_CONTAINER_ID, "")
        )
        manager._wait_for_socket = AsyncMock()
        await manager._create_container(
            _SENTINEL_USER_ID,
            (),
            runtime_id=_SENTINEL_RUNTIME_ID,
        )
    elif event == "discard_file_failed":
        private_path = str(tmp_path / "private-path-sentinel" / "container.cid")
        with (
            patch("yinshi.services.container.os.path.lexists", return_value=True),
            patch("yinshi.services.container.os.path.isdir", return_value=False),
            patch(
                "yinshi.services.container.os.unlink",
                side_effect=OSError(_SENTINEL_EXCEPTION),
            ),
        ):
            manager._discard_runtime_file(private_path, _SENTINEL_LEASE_KEY)
    elif event == "network_created":
        manager._run_podman = AsyncMock(return_value=(0, "", ""))
        await manager._create_network()
        assert "yinshi-sidecar-net" in caplog.text
    elif event == "network_recreated":
        manager._run_podman = AsyncMock(
            side_effect=[
                (0, '[{"internal": true}]', ""),
                (0, "", ""),
                (0, "", ""),
            ]
        )
        await manager._ensure_network()
        assert "yinshi-sidecar-net" in caplog.text
    elif event == "reaper_count":
        manager.reap_idle = AsyncMock(return_value=2)
        with (
            patch(
                "yinshi.services.container.asyncio.sleep",
                new=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await manager.run_reaper()
        assert "Reaped 2 idle container(s)" in caplog.text
    else:
        raise AssertionError(f"Unhandled event: {event}")

    assert caplog.records
    forbidden_values = (
        _SENTINEL_USER_ID,
        _SENTINEL_RUNTIME_ID,
        _SENTINEL_CONTAINER_KEY,
        _SENTINEL_CONTAINER_ID,
        _SENTINEL_CONTAINER_ID[:12],
        "private-path-sentinel",
        _SENTINEL_LEASE_KEY,
        _SENTINEL_EXCEPTION,
    )
    for record in caplog.records:
        rendered_record = record.getMessage()
        if record.exc_info:
            rendered_record += logging.Formatter().formatException(record.exc_info)
        assert all(value not in rendered_record for value in forbidden_values)
