"""Tests for control-plane runner maintenance fencing."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from yinshi.services.runner_relay import RunnerRelayBroker, RunnerTransferGrant


class FakeWebSocket:
    """Capture broker sends and closes."""

    def __init__(self) -> None:
        self.text_frames: list[str] = []
        self.closes: list[tuple[int, str | None]] = []

    async def send_text(self, data: str) -> None:
        self.text_frames.append(data)

    async def send_bytes(self, data: bytes) -> None:
        return None

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closes.append((code, reason))


@pytest.mark.asyncio
async def test_quiescence_disconnects_current_transfer_clients() -> None:
    """Quiescence should revoke current transfer sockets before guest maintenance."""
    broker = RunnerRelayBroker()
    runner = FakeWebSocket()
    client = FakeWebSocket()
    grant = RunnerTransferGrant(
        transfer_id=str(uuid.uuid4()),
        runner_id="runner-1",
        expires_at=1_900_000_300,
        max_session_bytes=65_536,
    )
    await broker.register_runner(grant.runner_id, runner)
    await broker.attach_client(grant, client)
    job_id = str(uuid.uuid4())

    waiter = asyncio.create_task(
        broker.quiesce_runner(grant.runner_id, job_id=job_id, timeout_seconds=1.0)
    )
    await asyncio.sleep(0)

    assert client.closes == [(4006, "Runner entered maintenance")]
    await broker.runner_quiesced(grant.runner_id, job_id)
    await waiter
