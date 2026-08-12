"""Verify opaque relay routing, framing, limits, and connection replacement.

In-memory WebSocket fakes capture only emitted frames. Tests assert ciphertext is
routed by random transfer UUID without granting relay access to plaintext.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from yinshi.services.runner_relay import (
    RunnerRelayAuthorizationError,
    RunnerRelayBroker,
    RunnerTransferGrant,
)


class FakeWebSocket:
    """Capture broker sends and closes without a network server."""

    def __init__(self) -> None:
        self.text_frames: list[str] = []
        self.binary_frames: list[bytes] = []
        self.closes: list[tuple[int, str | None]] = []

    async def send_text(self, data: str) -> None:
        self.text_frames.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.binary_frames.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closes.append((code, reason))


def _grant(*, byte_limit: int = 65_536) -> RunnerTransferGrant:
    return RunnerTransferGrant(
        transfer_id=str(uuid.uuid4()),
        runner_id="runner-1",
        expires_at=1_900_000_300,
        max_session_bytes=byte_limit,
    )


@pytest.mark.asyncio
async def test_relay_reports_current_runner_connection() -> None:
    """Wake coordination must observe only a current relay socket."""
    broker = RunnerRelayBroker()
    runner = FakeWebSocket()

    assert broker.is_runner_connected("runner-1") is False
    await broker.register_runner("runner-1", runner)
    assert broker.is_runner_connected("runner-1") is True
    await broker.unregister_runner("runner-1", runner)
    assert broker.is_runner_connected("runner-1") is False


@pytest.mark.asyncio
async def test_relay_requests_exact_runner_quiescence() -> None:
    """Control plane should wait for one matching runner maintenance acknowledgement."""
    broker = RunnerRelayBroker()
    runner = FakeWebSocket()
    await broker.register_runner("runner-1", runner)
    job_id = str(uuid.uuid4())

    waiter = asyncio.create_task(
        broker.quiesce_runner("runner-1", job_id=job_id, timeout_seconds=1.0)
    )
    await asyncio.sleep(0)
    assert runner.text_frames == [f'{{"job_id":"{job_id}","type":"quiesce"}}']
    await broker.runner_quiesced("runner-1", job_id)

    await waiter


@pytest.mark.asyncio
async def test_relay_reuses_exact_runner_maintenance_fence() -> None:
    """A retry for the same job should reacquire its existing transfer fence."""
    broker = RunnerRelayBroker()
    runner = FakeWebSocket()
    await broker.register_runner("runner-1", runner)
    job_id = str(uuid.uuid4())
    first_waiter = asyncio.create_task(
        broker.quiesce_runner("runner-1", job_id=job_id, timeout_seconds=1.0)
    )
    await asyncio.sleep(0)
    await broker.runner_quiesced("runner-1", job_id)
    await first_waiter

    await broker.quiesce_runner("runner-1", job_id=job_id, timeout_seconds=1.0)

    assert runner.text_frames[-1] == f'{{"job_id":"{job_id}","type":"quiesce"}}'


@pytest.mark.asyncio
async def test_relay_releases_exact_runner_maintenance_fence() -> None:
    """Coordinator recovery should remove only its matching in-memory fence."""
    broker = RunnerRelayBroker()
    runner = FakeWebSocket()
    await broker.register_runner("runner-1", runner)
    job_id = str(uuid.uuid4())
    waiter = asyncio.create_task(
        broker.quiesce_runner("runner-1", job_id=job_id, timeout_seconds=1.0)
    )
    await asyncio.sleep(0)
    await broker.runner_quiesced("runner-1", job_id)
    await waiter

    await broker.release_maintenance("runner-1", job_id=job_id)
    client = FakeWebSocket()
    await broker.attach_client(_grant(), client)


@pytest.mark.asyncio
async def test_relay_routes_only_bounded_ciphertext() -> None:
    """Broker adds only routing UUID and never transforms ciphertext bytes."""
    broker = RunnerRelayBroker()
    runner = FakeWebSocket()
    client = FakeWebSocket()
    grant = _grant()
    await broker.register_runner(grant.runner_id, runner)
    await broker.attach_client(grant, client)

    client_ciphertext = b"client-ciphertext-and-tag"
    await broker.client_frame(grant.transfer_id, client_ciphertext)
    assert runner.text_frames == [f'{{"transfer_id":"{grant.transfer_id}","type":"open"}}']
    assert runner.binary_frames == [uuid.UUID(grant.transfer_id).bytes + client_ciphertext]

    sender_task = asyncio.create_task(broker.send_client_frames(grant.transfer_id))
    runner_ciphertext = b"runner-ciphertext-and-tag"
    await broker.runner_frame(
        grant.runner_id,
        uuid.UUID(grant.transfer_id).bytes + runner_ciphertext,
    )
    await asyncio.wait_for(_wait_for_binary_frame(client), timeout=1.0)
    assert client.binary_frames == [runner_ciphertext]

    await broker.detach_client(grant.transfer_id, client)
    await asyncio.wait_for(sender_task, timeout=1.0)
    assert runner.text_frames[-1] == (f'{{"transfer_id":"{grant.transfer_id}","type":"close"}}')


@pytest.mark.asyncio
async def test_relay_closes_a_transfer_when_client_backpressure_fills() -> None:
    """One slow client cannot create an unbounded runner-to-client queue."""
    broker = RunnerRelayBroker()
    runner = FakeWebSocket()
    client = FakeWebSocket()
    grant = _grant()
    await broker.register_runner(grant.runner_id, runner)
    await broker.attach_client(grant, client)
    prefix = uuid.UUID(grant.transfer_id).bytes

    for index in range(16):
        await broker.runner_frame(grant.runner_id, prefix + bytes([index]))
    await broker.runner_frame(grant.runner_id, prefix + b"x")
    assert client.closes == [(4005, "Runner relay client exceeded backpressure")]
    assert runner.text_frames[-1] == (f'{{"transfer_id":"{grant.transfer_id}","type":"close"}}')


@pytest.mark.asyncio
async def test_relay_enforces_shared_byte_budget_and_runner_replacement() -> None:
    """Oversized sessions fail and stale runner sockets lose authority."""
    broker = RunnerRelayBroker()
    first_runner = FakeWebSocket()
    replacement_runner = FakeWebSocket()
    client = FakeWebSocket()
    grant = _grant(byte_limit=32)
    await broker.register_runner(grant.runner_id, first_runner)
    await broker.attach_client(grant, client)

    await broker.client_frame(grant.transfer_id, b"a" * 16)
    await broker.runner_frame(
        grant.runner_id,
        uuid.UUID(grant.transfer_id).bytes + b"b" * 17,
    )
    assert client.closes == [(4005, "Runner relay session exceeded byte limit")]

    await broker.register_runner(grant.runner_id, replacement_runner)
    assert first_runner.closes == [(4001, "Runner connection replaced")]
    await broker.unregister_runner(grant.runner_id, first_runner)
    with pytest.raises(RunnerRelayAuthorizationError, match="no attached client"):
        await broker.runner_frame(
            grant.runner_id,
            uuid.UUID(grant.transfer_id).bytes + b"c",
        )


def test_runner_websocket_sends_welcome_before_recovered_maintenance(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnected runner must receive its identity before maintenance control."""
    from yinshi.api import runner_relay as runner_relay_api

    job_id = str(uuid.uuid4())

    async def register_runner(_runner_id: str, websocket: object) -> None:
        await websocket.send_text(f'{{"job_id":"{job_id}","type":"quiesce"}}')

    monkeypatch.setattr(
        runner_relay_api,
        "authenticate_runner_token",
        lambda _token: {"runner_id": "runner-1"},
    )
    monkeypatch.setattr(
        runner_relay_api.runner_relay_broker,
        "register_runner",
        register_runner,
    )

    with auth_client.websocket_connect(
        "/runner/relay",
        headers={"Authorization": "Bearer runner-token"},
    ) as runner_socket:
        assert runner_socket.receive_json() == {
            "runner_id": "runner-1",
            "type": "welcome",
        }
        assert runner_socket.receive_json() == {"job_id": job_id, "type": "quiesce"}


def test_runner_websocket_routes_quiesced_acknowledgement(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner WebSocket should route only exact maintenance acknowledgements."""
    from yinshi.api import runner_relay as runner_relay_api

    async def authenticate(_token: str) -> dict[str, str]:
        return {"runner_id": "runner-1"}

    acknowledgements: list[tuple[str, str]] = []

    async def runner_quiesced(runner_id: str, job_id: str) -> None:
        acknowledgements.append((runner_id, job_id))

    monkeypatch.setattr(
        runner_relay_api,
        "authenticate_runner_token",
        lambda _token: {"runner_id": "runner-1"},
    )
    monkeypatch.setattr(
        runner_relay_api.runner_relay_broker,
        "runner_quiesced",
        runner_quiesced,
    )
    job_id = str(uuid.uuid4())

    with auth_client.websocket_connect(
        "/runner/relay",
        headers={"Authorization": "Bearer runner-token"},
    ) as runner_socket:
        runner_socket.receive_json()
        runner_socket.send_json({"job_id": job_id, "type": "quiesced"})

    assert acknowledgements == [("runner-1", job_id)]


def test_websocket_relay_authenticates_runner_and_exact_capability(
    auth_client: TestClient,
) -> None:
    """Hosted sockets relay binary frames only after both sides authenticate."""
    create_response = auth_client.post(
        "/api/settings/runner",
        json={"name": "Relay runner", "cloud_provider": "aws", "region": "us-west-2"},
    )
    assert create_response.status_code == 201
    registration = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_response.json()["registration_token"],
            "runner_version": "0.2.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
            "storage_profile": "aws_ebs_s3_files",
            "noise_public_key": "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
        },
    )
    assert registration.status_code == 201
    runner_token = registration.json()["runner_token"]
    confirmation = auth_client.post(
        "/api/settings/runner/noise-key/confirm",
        json={"noise_public_key": "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I"},
    )
    assert confirmation.status_code == 200
    capability_response = auth_client.post(
        "/api/settings/runner/capabilities",
        json={
            "initiator_public_key": "a8OCKiqn9OaYHWU4aSs83z5t-e6m7SaetB2TwidXt1o",
            "scopes": ["worker.health"],
            "max_session_bytes": 65_536,
        },
    )
    assert capability_response.status_code == 201
    capability = capability_response.json()
    transfer_id = capability["transfer_id"]

    with auth_client.websocket_connect(
        "/runner/relay",
        headers={"Authorization": f"Bearer {runner_token}"},
    ) as runner_socket:
        assert runner_socket.receive_json() == {
            "runner_id": registration.json()["runner_id"],
            "type": "welcome",
        }
        with auth_client.websocket_connect(f"/api/runner/relay/{transfer_id}") as client_socket:
            client_socket.send_text(capability["capability"])
            assert runner_socket.receive_text() == (
                f'{{"transfer_id":"{transfer_id}","type":"open"}}'
            )
            assert client_socket.receive_json() == {"type": "ready"}

            client_socket.send_bytes(b"opaque-client-frame")
            assert runner_socket.receive_bytes() == (
                uuid.UUID(transfer_id).bytes + b"opaque-client-frame"
            )
            runner_socket.send_bytes(uuid.UUID(transfer_id).bytes + b"opaque-runner-frame")
            assert client_socket.receive_bytes() == b"opaque-runner-frame"

            revoke_response = auth_client.delete("/api/settings/runner")
            assert revoke_response.status_code == 204
            with pytest.raises(WebSocketDisconnect) as runner_disconnect:
                runner_socket.receive_bytes()
            assert runner_disconnect.value.code == 4003


async def _wait_for_binary_frame(websocket: FakeWebSocket) -> None:
    """Yield until the broker's sender task drains one queued frame."""
    while not websocket.binary_frames:
        await asyncio.sleep(0)
