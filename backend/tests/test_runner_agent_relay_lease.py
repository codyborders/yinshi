"""Verify managed relay task references."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from yinshi.services.runner_agent_relay import (
    RunnerAgentRelayRuntime,
    RunnerRelaySessionError,
)

_RUNNER_PRIVATE_KEY = bytes.fromhex(
    "4a3acbfdb163dec651dfa3194dece676d437029c62a408b4c5ea9114246e4893"
)


class RecordingTaskLease:
    """Record local task operations."""

    def __init__(self) -> None:
        self.operations: list[str] = []

    async def acquire(self) -> None:
        self.operations.append("acquire")

    async def release(self) -> None:
        self.operations.append("release")


@pytest.mark.asyncio
async def test_runner_relay_holds_task_during_transfer(tmp_path: Path) -> None:
    """Managed transfer holds one task reference from open through close."""
    task_lease = RecordingTaskLease()
    signing_key = b"s" * 32
    runtime = RunnerAgentRelayRuntime(
        runner_static_private_key=_RUNNER_PRIVATE_KEY,
        capability_signing_public_key=signing_key,
        replay_database_path=tmp_path / "runner-replay.sqlite3",
        task_lease=task_lease,
    )
    transfer_id = str(uuid.uuid4())
    await runtime.handle_control(json.dumps({"runner_id": "runner-1", "type": "welcome"}))
    await runtime.handle_control(json.dumps({"transfer_id": transfer_id, "type": "open"}))
    assert task_lease.operations == ["acquire"]
    await runtime.handle_control(json.dumps({"transfer_id": transfer_id, "type": "close"}))
    assert task_lease.operations == ["acquire", "release"]


@pytest.mark.asyncio
async def test_runner_relay_releases_task_when_encrypted_session_fails(
    tmp_path: Path,
) -> None:
    """A rejected encrypted frame removes its transfer and releases its task reference."""
    task_lease = RecordingTaskLease()
    runtime = RunnerAgentRelayRuntime(
        runner_static_private_key=_RUNNER_PRIVATE_KEY,
        capability_signing_public_key=b"s" * 32,
        replay_database_path=tmp_path / "runner-replay.sqlite3",
        task_lease=task_lease,
    )
    transfer_id = str(uuid.uuid4())
    await runtime.handle_control(json.dumps({"runner_id": "runner-1", "type": "welcome"}))
    await runtime.handle_control(json.dumps({"transfer_id": transfer_id, "type": "open"}))

    with pytest.raises(RunnerRelaySessionError):
        await runtime.handle_binary(
            uuid.UUID(transfer_id).bytes + b"invalid-encrypted-frame",
            current_time=1_900_000_000,
        )

    assert runtime.active_transfer_ids == ()
    assert task_lease.operations == ["acquire", "release"]


@pytest.mark.asyncio
async def test_runner_relay_aclose_releases_remaining_tasks_once(tmp_path: Path) -> None:
    """Closing a runtime clears transfers and releases each remaining task reference once."""
    task_lease = RecordingTaskLease()
    runtime = RunnerAgentRelayRuntime(
        runner_static_private_key=_RUNNER_PRIVATE_KEY,
        capability_signing_public_key=b"s" * 32,
        replay_database_path=tmp_path / "runner-replay.sqlite3",
        task_lease=task_lease,
    )
    transfer_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    await runtime.handle_control(json.dumps({"runner_id": "runner-1", "type": "welcome"}))
    for transfer_id in transfer_ids:
        await runtime.handle_control(json.dumps({"transfer_id": transfer_id, "type": "open"}))

    await runtime.aclose()
    await runtime.aclose()

    assert runtime.active_transfer_ids == ()
    assert task_lease.operations == ["acquire", "acquire", "release", "release"]


@pytest.mark.asyncio
async def test_runner_relay_releases_task_when_session_construction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed session constructor releases the task reference acquired for it."""
    task_lease = RecordingTaskLease()
    runtime = RunnerAgentRelayRuntime(
        runner_static_private_key=_RUNNER_PRIVATE_KEY,
        capability_signing_public_key=b"s" * 32,
        replay_database_path=tmp_path / "runner-replay.sqlite3",
        task_lease=task_lease,
    )
    transfer_id = str(uuid.uuid4())
    await runtime.handle_control(json.dumps({"runner_id": "runner-1", "type": "welcome"}))

    def reject_session(**kwargs: object) -> None:
        raise RuntimeError("session construction failed")

    monkeypatch.setattr(
        "yinshi.services.runner_agent_relay.EncryptedRunnerRpcSession",
        reject_session,
    )

    with pytest.raises(RuntimeError, match="session construction failed"):
        await runtime.handle_control(json.dumps({"transfer_id": transfer_id, "type": "open"}))

    assert runtime.active_transfer_ids == ()
    assert task_lease.operations == ["acquire", "release"]
