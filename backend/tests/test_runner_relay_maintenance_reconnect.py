"""Maintenance continuity tests for runner relay reconnection."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from yinshi.services.managed_backups import ManagedBackupOperation
from yinshi.services.runner_relay import (
    RunnerRelayAuthorizationError,
    RunnerRelayBroker,
    RunnerTransferGrant,
)


class FakeWebSocket:
    """Capture broker traffic without a network socket."""

    def __init__(self) -> None:
        self.text_frames: list[str] = []

    async def send_text(self, data: str) -> None:
        self.text_frames.append(data)

    async def send_bytes(self, _data: bytes) -> None:
        return None

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        return None


def _running_operation(runner_id: str, job_id: str) -> ManagedBackupOperation:
    return ManagedBackupOperation(
        user_id="user-1",
        job_id=job_id,
        archive_id="archive-1",
        operation="create",
        status="running",
        runtime_generation=1,
        started_at="2026-08-12T12:00:00Z",
        updated_at="2026-08-12T12:00:00Z",
        last_error=None,
        source_runner_id=runner_id,
    )


def test_relay_restart_recovers_database_backed_maintenance_fence(
    auth_client: TestClient,
) -> None:
    """A new broker must recover its job from the durable backup catalog."""
    from datetime import datetime, timezone

    from yinshi.db import get_control_db
    from yinshi.services.managed_backups import (
        get_running_managed_backup_operation_for_runner,
        start_managed_backup_creation,
    )
    from yinshi.services.managed_runners import claim_managed_runtime_provisioning

    tenant = getattr(auth_client, "yinshi_tenant")
    claim_managed_runtime_provisioning(
        tenant.user_id,
        name_prefix="yinshi",
        name_key="secret-name-key",
        artifact_version="runner-v1",
        region="ord",
        control_url="https://control.example",
        now=datetime(2026, 8, 12, 11, 0, tzinfo=timezone.utc),
    )
    with get_control_db() as database:
        runtime = database.execute(
            "SELECT runner_id FROM managed_runtimes WHERE user_id = ?",
            (tenant.user_id,),
        ).fetchone()
        assert runtime is not None
        runner_id = runtime["runner_id"]
        database.execute(
            """UPDATE managed_runtimes
               SET lifecycle_status = 'ready', generation = 3
               WHERE user_id = ?""",
            (tenant.user_id,),
        )
        database.commit()
    job_id = str(uuid.uuid4())
    start_managed_backup_creation(
        tenant.user_id,
        runtime_generation=3,
        archive_id=str(uuid.uuid4()),
        job_id=job_id,
        object_key="managed/v1/restart.enc",
        wrapped_key=b"wrapped-key",
        key_id="backup-v1",
        owner_digest="a" * 64,
        now=datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
    )

    async def reconnect() -> None:
        broker = RunnerRelayBroker(
            get_running_operation=get_running_managed_backup_operation_for_runner
        )
        runner = FakeWebSocket()
        await broker.register_runner(runner_id, runner)
        await broker.runner_quiesced(runner_id, job_id)
        grant = RunnerTransferGrant(
            transfer_id=str(uuid.uuid4()),
            runner_id=runner_id,
            expires_at=1_900_000_300,
            max_session_bytes=65_536,
        )

        with pytest.raises(RunnerRelayAuthorizationError, match="maintenance"):
            await broker.attach_client(grant, FakeWebSocket())
        assert runner.text_frames == [f'{{"job_id":"{job_id}","type":"quiesce"}}']

    asyncio.run(reconnect())


@pytest.mark.asyncio
async def test_registration_lookup_failure_leaves_no_runner_connection() -> None:
    """A durable lookup failure must not publish the new runner connection."""

    def fail_lookup(_runner_id: str) -> ManagedBackupOperation | None:
        raise RuntimeError("catalog unavailable")

    broker = RunnerRelayBroker(get_running_operation=fail_lookup)

    with pytest.raises(RuntimeError, match="catalog unavailable"):
        await broker.register_runner("runner-1", FakeWebSocket())

    assert not broker.is_runner_connected("runner-1")


@pytest.mark.asyncio
async def test_reconnected_acknowledged_maintenance_accepts_duplicate_ack() -> None:
    """A runner may acknowledge the repeated reconnect quiesce request again."""
    job_id = str(uuid.uuid4())
    broker = RunnerRelayBroker()
    first_runner = FakeWebSocket()
    await broker.register_runner("runner-1", first_runner)

    waiter = asyncio.create_task(
        broker.quiesce_runner("runner-1", job_id=job_id, timeout_seconds=1.0)
    )
    await asyncio.sleep(0)
    await broker.runner_quiesced("runner-1", job_id)
    await waiter

    replacement_runner = FakeWebSocket()
    await broker.register_runner("runner-1", replacement_runner)
    await broker.runner_quiesced("runner-1", job_id)

    assert replacement_runner.text_frames == [f'{{"job_id":"{job_id}","type":"quiesce"}}']


@pytest.mark.asyncio
async def test_recovered_maintenance_waits_for_runner_acknowledgement() -> None:
    """Recovered maintenance must wait until the runner confirms quiescence."""
    job_id = str(uuid.uuid4())
    broker = RunnerRelayBroker(
        get_running_operation=lambda runner_id: _running_operation(runner_id, job_id)
    )
    await broker.register_runner("runner-1", FakeWebSocket())

    waiter = asyncio.create_task(
        broker.quiesce_runner("runner-1", job_id=job_id, timeout_seconds=1.0)
    )
    await asyncio.sleep(0)

    assert not waiter.done()
    await broker.runner_quiesced("runner-1", job_id)
    await waiter


@pytest.mark.asyncio
async def test_new_broker_recovers_durable_maintenance_fence() -> None:
    """Process restart must recover transfer denial from durable operation state."""
    job_id = str(uuid.uuid4())
    broker = RunnerRelayBroker(
        get_running_operation=lambda runner_id: _running_operation(runner_id, job_id)
    )
    runner = FakeWebSocket()
    await broker.register_runner("runner-1", runner)
    await broker.runner_quiesced("runner-1", job_id)
    grant = RunnerTransferGrant(
        transfer_id=str(uuid.uuid4()),
        runner_id="runner-1",
        expires_at=1_900_000_300,
        max_session_bytes=65_536,
    )

    with pytest.raises(RunnerRelayAuthorizationError, match="maintenance"):
        await broker.attach_client(grant, FakeWebSocket())
    assert runner.text_frames == [f'{{"job_id":"{job_id}","type":"quiesce"}}']


@pytest.mark.asyncio
async def test_relay_preserves_maintenance_across_runner_reconnect() -> None:
    """Runner reconnection must not reopen transfers during active maintenance."""
    broker = RunnerRelayBroker()
    first_runner = FakeWebSocket()
    replacement_runner = FakeWebSocket()
    await broker.register_runner("runner-1", first_runner)
    job_id = str(uuid.uuid4())
    waiter = asyncio.create_task(
        broker.quiesce_runner("runner-1", job_id=job_id, timeout_seconds=1.0)
    )
    await asyncio.sleep(0)
    await broker.runner_quiesced("runner-1", job_id)
    await waiter

    await broker.unregister_runner("runner-1", first_runner)
    await broker.register_runner("runner-1", replacement_runner)
    assert replacement_runner.text_frames == [f'{{"job_id":"{job_id}","type":"quiesce"}}']
    grant = RunnerTransferGrant(
        transfer_id=str(uuid.uuid4()),
        runner_id="runner-1",
        expires_at=1_900_000_300,
        max_session_bytes=65_536,
    )

    with pytest.raises(RunnerRelayAuthorizationError, match="maintenance"):
        await broker.attach_client(grant, FakeWebSocket())
    await broker.release_maintenance("runner-1", job_id=job_id)
    await broker.attach_client(grant, FakeWebSocket())
