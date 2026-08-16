"""Verify runner-side relay multiplexing and strict control messages.

Tests cover connection-scoped runner identity plus transfer lifecycle without a
network server; encrypted RPC behavior is covered by test_runner_rpc.py.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from yinshi.services.runner_agent_relay import RunnerAgentRelayRuntime
from yinshi.services.runner_capabilities import runner_capability_signing_public_key

_RUNNER_PRIVATE_KEY = bytes.fromhex(
    "4a3acbfdb163dec651dfa3194dece676d437029c62a408b4c5ea9114246e4893"
)


class RecordingTaskLease:
    """Record managed task reference operations."""

    def __init__(self) -> None:
        self.operations: list[str] = []

    async def acquire(self) -> None:
        self.operations.append("acquire")

    async def release(self) -> None:
        self.operations.append("release")


def _runtime(tmp_path: Path) -> RunnerAgentRelayRuntime:
    signing_key = base64.urlsafe_b64decode(runner_capability_signing_public_key() + "=")
    return RunnerAgentRelayRuntime(
        runner_static_private_key=_RUNNER_PRIVATE_KEY,
        capability_signing_public_key=signing_key,
        replay_database_path=tmp_path / "runner-replay.sqlite3",
    )


async def test_runner_relay_runtime_requires_welcome_before_transfer(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Relay cannot open sessions before authenticated control identifies runner."""
    runtime = _runtime(tmp_path)
    transfer_id = str(uuid.uuid4())
    open_message = json.dumps({"transfer_id": transfer_id, "type": "open"})

    with pytest.raises(ValueError, match="welcome"):
        await runtime.handle_control(open_message)

    await runtime.handle_control(json.dumps({"runner_id": "runner-1", "type": "welcome"}))
    await runtime.handle_control(open_message)
    assert runtime.active_transfer_ids == (transfer_id,)

    await runtime.handle_control(json.dumps({"transfer_id": transfer_id, "type": "close"}))
    assert not runtime.active_transfer_ids

    await runtime.handle_control(open_message)
    await runtime.aclose()
    await runtime.aclose()
    assert not runtime.active_transfer_ids


@pytest.mark.asyncio
async def test_runner_relay_runtime_rejects_unknown_or_malformed_frames(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Unknown UUID prefixes and extra control fields fail closed."""
    runtime = _runtime(tmp_path)
    await runtime.handle_control(json.dumps({"runner_id": "runner-1", "type": "welcome"}))

    with pytest.raises(ValueError, match="shape"):
        await runtime.handle_control(
            json.dumps({"extra": True, "runner_id": "runner-1", "type": "welcome"})
        )
    with pytest.raises(ValueError, match="not open"):
        await runtime.handle_binary(
            uuid.uuid4().bytes + b"ciphertext",
            current_time=1_900_000_000,
        )


@pytest.mark.asyncio
async def test_runner_relay_runtime_accepts_duplicate_close_after_retirement(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """A queued browser close remains harmless after its transfer retires."""
    runtime = _runtime(tmp_path)
    transfer_id = str(uuid.uuid4())
    close_message = json.dumps({"transfer_id": transfer_id, "type": "close"})
    await runtime.handle_control(json.dumps({"runner_id": "runner-1", "type": "welcome"}))
    await runtime.handle_control(json.dumps({"transfer_id": transfer_id, "type": "open"}))

    await runtime.handle_control(close_message)
    await runtime.handle_control(close_message)

    with pytest.raises(ValueError, match="not open"):
        await runtime.handle_control(
            json.dumps({"transfer_id": str(uuid.uuid4()), "type": "close"})
        )
