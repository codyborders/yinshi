"""Verify opaque relay routing, framing, limits, and connection replacement.

In-memory WebSocket fakes capture only emitted frames. Tests assert ciphertext is
routed by random transfer UUID without granting relay access to plaintext.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import pytest
from fastapi import WebSocket
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


class StrictCloseWebSocket(FakeWebSocket):
    """Reject a repeated close like Starlette after close transmission."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        if self.closed:
            raise RuntimeError('Cannot call "send" once a close message has been sent.')
        self.closed = True
        await super().close(code=code, reason=reason)


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
async def test_relay_unregister_survives_an_already_closed_client() -> None:
    """Runner cleanup remains complete when a client already sent its close."""
    broker = RunnerRelayBroker()
    runner = StrictCloseWebSocket()
    client = StrictCloseWebSocket()
    grant = _grant()
    await broker.register_runner(grant.runner_id, runner)
    await broker.attach_client(grant, client)
    await client.close(code=1000, reason="Browser disconnected")

    await broker.unregister_runner(grant.runner_id, runner)

    assert broker.is_runner_connected(grant.runner_id) is False
    with pytest.raises(RunnerRelayAuthorizationError, match="not attached"):
        await broker.client_frame(grant.transfer_id, b"detached")


@pytest.mark.asyncio
async def test_relay_replacement_close_failure_does_not_publish_candidate() -> None:
    """A failed stale-socket close cannot expose an incomplete replacement."""

    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class FailingCloseWebSocket(FakeWebSocket):
        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            close_started.set()
            await release_close.wait()
            raise RuntimeError("replacement close failed")

    broker = RunnerRelayBroker()
    old_runner = FailingCloseWebSocket()
    candidate = FakeWebSocket()
    await broker.register_runner("runner-1", old_runner)

    registration = asyncio.create_task(broker.register_runner("runner-1", candidate))
    await close_started.wait()
    assert broker.is_runner_connected("runner-1") is False
    with pytest.raises(RunnerRelayAuthorizationError, match="not connected"):
        await broker.attach_client(_grant(), FakeWebSocket())
    release_close.set()
    with pytest.raises(RuntimeError, match="replacement close failed"):
        await registration

    assert broker.is_runner_connected("runner-1") is False
    with pytest.raises(RunnerRelayAuthorizationError, match="not connected"):
        await broker.attach_client(_grant(), FakeWebSocket())


@pytest.mark.asyncio
async def test_relay_failed_registration_preserves_later_replacement() -> None:
    """A failed candidate rollback cannot remove a later healthy connection."""
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class DelayedFailingCloseWebSocket(FakeWebSocket):
        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            close_started.set()
            await release_close.wait()
            raise RuntimeError("old close failed")

    broker = RunnerRelayBroker()
    old_runner = DelayedFailingCloseWebSocket()
    first_candidate = FakeWebSocket()
    final_runner = FakeWebSocket()
    await broker.register_runner("runner-1", old_runner)

    failed_registration = asyncio.create_task(broker.register_runner("runner-1", first_candidate))
    await close_started.wait()
    await broker.register_runner("runner-1", final_runner)
    release_close.set()
    with pytest.raises(RuntimeError, match="old close failed"):
        await failed_registration

    assert broker.is_runner_connected("runner-1") is True
    grant = _grant()
    await broker.attach_client(grant, FakeWebSocket())
    assert final_runner.text_frames == [f'{{"transfer_id":"{grant.transfer_id}","type":"open"}}']


@pytest.mark.asyncio
async def test_relay_replacement_survives_already_closed_sockets() -> None:
    """Connection replacement cleans old state after both sockets closed."""
    broker = RunnerRelayBroker()
    old_runner = StrictCloseWebSocket()
    replacement_runner = StrictCloseWebSocket()
    client = StrictCloseWebSocket()
    grant = _grant()
    await broker.register_runner(grant.runner_id, old_runner)
    await broker.attach_client(grant, client)
    await old_runner.close(code=1000, reason="Transport ended")
    await client.close(code=1000, reason="Browser ended")

    await broker.register_runner(grant.runner_id, replacement_runner)

    assert broker.is_runner_connected(grant.runner_id) is True
    with pytest.raises(RunnerRelayAuthorizationError, match="not attached"):
        await broker.client_frame(grant.transfer_id, b"detached")


@pytest.mark.asyncio
async def test_relay_maintenance_survives_an_already_closed_client() -> None:
    """Maintenance fencing completes after a browser close raced cleanup."""
    broker = RunnerRelayBroker()
    runner = StrictCloseWebSocket()
    client = StrictCloseWebSocket()
    grant = _grant()
    job_id = str(uuid.uuid4())
    await broker.register_runner(grant.runner_id, runner)
    await broker.attach_client(grant, client)
    await client.close(code=1000, reason="Browser ended")

    waiter = asyncio.create_task(
        broker.quiesce_runner(grant.runner_id, job_id=job_id, timeout_seconds=1.0)
    )
    await asyncio.sleep(0)
    await broker.runner_quiesced(grant.runner_id, job_id)
    await waiter

    assert runner.text_frames[-1] == f'{{"job_id":"{job_id}","type":"quiesce"}}'


@pytest.mark.asyncio
async def test_relay_rejected_transfer_survives_an_already_closed_client() -> None:
    """Runner rejection retires one transfer after the browser closed first."""
    broker = RunnerRelayBroker()
    runner = StrictCloseWebSocket()
    client = StrictCloseWebSocket()
    grant = _grant()
    await broker.register_runner(grant.runner_id, runner)
    await broker.attach_client(grant, client)
    await client.close(code=1000, reason="Browser ended")

    await broker.runner_closed_transfer(grant.runner_id, grant.transfer_id)

    assert broker.is_runner_connected(grant.runner_id) is True


@pytest.mark.asyncio
async def test_relay_revocation_survives_already_closed_sockets() -> None:
    """Runner revocation remains complete after transport close races."""
    broker = RunnerRelayBroker()
    runner = StrictCloseWebSocket()
    client = StrictCloseWebSocket()
    grant = _grant()
    await broker.register_runner(grant.runner_id, runner)
    await broker.attach_client(grant, client)
    await runner.close(code=1000, reason="Transport ended")
    await client.close(code=1000, reason="Browser ended")

    await broker.disconnect_runner(grant.runner_id)

    assert broker.is_runner_connected(grant.runner_id) is False


@pytest.mark.asyncio
async def test_relay_does_not_suppress_unrelated_close_failure() -> None:
    """Only Starlette's completed-close failure is safe to suppress."""

    class BrokenCloseWebSocket(FakeWebSocket):
        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            raise RuntimeError("close transport failed")

    broker = RunnerRelayBroker()
    runner = FakeWebSocket()
    client = BrokenCloseWebSocket()
    grant = _grant()
    await broker.register_runner(grant.runner_id, runner)
    await broker.attach_client(grant, client)

    with pytest.raises(RuntimeError, match="close transport failed"):
        await broker.unregister_runner(grant.runner_id, runner)

    assert broker.is_runner_connected(grant.runner_id) is False


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
async def test_relay_ignores_late_frames_for_a_detached_transfer() -> None:
    """A timed-out browser transfer must not disconnect its shared runner."""
    broker = RunnerRelayBroker()
    runner = FakeWebSocket()
    client = FakeWebSocket()
    grant = _grant()
    await broker.register_runner(grant.runner_id, runner)
    await broker.attach_client(grant, client)
    await broker.detach_client(grant.transfer_id, client)

    await broker.runner_frame(
        grant.runner_id,
        uuid.UUID(grant.transfer_id).bytes + b"late-response",
    )
    await broker.runner_closed_transfer(grant.runner_id, grant.transfer_id)

    assert broker.is_runner_connected(grant.runner_id) is True
    with pytest.raises(RunnerRelayAuthorizationError, match="no attached client"):
        await broker.runner_frame(
            grant.runner_id,
            uuid.uuid4().bytes + b"unknown-response",
        )


@pytest.mark.asyncio
async def test_relay_accepts_delayed_runner_close_after_retirement_eviction() -> None:
    """A delayed valid close must not terminate the shared runner relay."""
    broker = RunnerRelayBroker()
    runner = FakeWebSocket()
    await broker.register_runner("runner-1", runner)
    oldest_grant = _grant()
    oldest_client = FakeWebSocket()
    await broker.attach_client(oldest_grant, oldest_client)
    await broker.detach_client(oldest_grant.transfer_id, oldest_client)

    for _ in range(128):
        grant = _grant()
        client = FakeWebSocket()
        await broker.attach_client(grant, client)
        await broker.detach_client(grant.transfer_id, client)

    await broker.runner_closed_transfer("runner-1", oldest_grant.transfer_id)

    current_grant = _grant()
    current_client = FakeWebSocket()
    await broker.attach_client(current_grant, current_client)
    await broker.client_frame(current_grant.transfer_id, b"still-usable")

    assert broker.is_runner_connected("runner-1") is True
    assert runner.binary_frames[-1] == (
        uuid.UUID(current_grant.transfer_id).bytes + b"still-usable"
    )


@pytest.mark.asyncio
async def test_relay_rejects_close_for_another_runners_active_transfer() -> None:
    """One runner cannot close or mutate another runner's active transfer."""
    broker = RunnerRelayBroker()
    first_runner = FakeWebSocket()
    second_runner = FakeWebSocket()
    client = FakeWebSocket()
    transfer_id = str(uuid.uuid4())
    grant = RunnerTransferGrant(
        transfer_id=transfer_id,
        runner_id="runner-2",
        expires_at=1_900_000_300,
        max_session_bytes=65_536,
    )
    await broker.register_runner("runner-1", first_runner)
    await broker.register_runner("runner-2", second_runner)
    await broker.attach_client(grant, client)

    with pytest.raises(RunnerRelayAuthorizationError, match="not attached"):
        await broker.runner_closed_transfer("runner-1", transfer_id)
    await broker.client_frame(transfer_id, b"still-owned-by-runner-2")

    assert broker.is_runner_connected("runner-1") is True
    assert broker.is_runner_connected("runner-2") is True
    assert second_runner.binary_frames == [
        uuid.UUID(transfer_id).bytes + b"still-owned-by-runner-2"
    ]


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
async def test_relay_byte_limit_survives_an_already_closed_client() -> None:
    """Byte-limit cleanup retires a transfer after browser close races."""
    broker = RunnerRelayBroker()
    runner = StrictCloseWebSocket()
    client = StrictCloseWebSocket()
    grant = _grant(byte_limit=16)
    await broker.register_runner(grant.runner_id, runner)
    await broker.attach_client(grant, client)
    await client.close(code=1000, reason="Browser ended")

    await broker.runner_frame(
        grant.runner_id,
        uuid.UUID(grant.transfer_id).bytes + b"x" * 17,
    )

    assert broker.is_runner_connected(grant.runner_id) is True
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


def test_runner_websocket_logs_only_sanitized_disconnect_code(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Runner disconnect logs expose only one normalized close code."""
    from yinshi.api import runner_relay as runner_relay_api

    monkeypatch.setattr(
        runner_relay_api,
        "authenticate_runner_token",
        lambda _token: {"runner_id": "secret-runner-id"},
    )

    with caplog.at_level(logging.INFO, logger="yinshi.api.runner_relay"):
        with auth_client.websocket_connect(
            "/runner/relay",
            headers={"Authorization": "Bearer secret-runner-token"},
        ) as runner_socket:
            runner_socket.receive_json()

    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["Runner relay disconnected code=1000"]
    assert "secret" not in " ".join(messages)


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


def test_runner_websocket_survives_an_idempotent_unknown_close(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid delayed close must leave the shared runner socket usable."""
    from yinshi.api import runner_relay as runner_relay_api

    broker = RunnerRelayBroker()
    transfer_id = str(uuid.uuid4())
    grant = RunnerTransferGrant(
        transfer_id=transfer_id,
        runner_id="runner-1",
        expires_at=1_900_000_300,
        max_session_bytes=65_536,
    )
    monkeypatch.setattr(
        runner_relay_api,
        "authenticate_runner_token",
        lambda _token: {"runner_id": "runner-1"},
    )
    monkeypatch.setattr(runner_relay_api, "runner_relay_broker", broker)
    monkeypatch.setattr(
        runner_relay_api,
        "claim_runner_transfer_grant",
        lambda _transfer_id, _capability: grant,
    )

    with auth_client.websocket_connect(
        "/runner/relay",
        headers={"Authorization": "Bearer runner-token"},
    ) as runner_socket:
        assert runner_socket.receive_json() == {
            "runner_id": "runner-1",
            "type": "welcome",
        }
        runner_socket.send_json({"transfer_id": str(uuid.uuid4()), "type": "close"})
        with auth_client.websocket_connect(f"/api/runner/relay/{transfer_id}") as client_socket:
            client_socket.send_text("capability")
            assert runner_socket.receive_text() == (
                f'{{"transfer_id":"{transfer_id}","type":"open"}}'
            )
            assert client_socket.receive_json() == {"type": "ready"}


def test_runner_websocket_rejects_malformed_close_control(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotent close handling must still reject extra control fields."""
    from yinshi.api import runner_relay as runner_relay_api

    monkeypatch.setattr(
        runner_relay_api,
        "authenticate_runner_token",
        lambda _token: {"runner_id": "runner-1"},
    )

    with auth_client.websocket_connect(
        "/runner/relay",
        headers={"Authorization": "Bearer runner-token"},
    ) as runner_socket:
        assert runner_socket.receive_json() == {
            "runner_id": "runner-1",
            "type": "welcome",
        }
        runner_socket.send_json(
            {
                "extra": True,
                "transfer_id": str(uuid.uuid4()),
                "type": "close",
            }
        )
        with pytest.raises(WebSocketDisconnect) as disconnect:
            runner_socket.receive_bytes()

    assert disconnect.value.code == 4400


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


def test_client_websocket_does_not_close_twice_after_concurrent_teardown(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent broker teardown must not trigger a second close send."""
    from yinshi.api import runner_relay as runner_relay_api

    transfer_id = str(uuid.uuid4())
    grant = RunnerTransferGrant(
        transfer_id=transfer_id,
        runner_id="runner-1",
        expires_at=1_900_000_300,
        max_session_bytes=65_536,
    )
    client_websocket: WebSocket | None = None

    async def attach_client(_grant: RunnerTransferGrant, websocket: WebSocket) -> None:
        nonlocal client_websocket
        client_websocket = websocket

    async def client_frame(_transfer_id: str, _ciphertext: bytes) -> None:
        assert client_websocket is not None
        await client_websocket.close(code=4004, reason="Runner rejected transfer")
        raise RunnerRelayAuthorizationError("Runner relay client is not attached")

    async def send_client_frames(_transfer_id: str) -> None:
        return None

    async def detach_client(_transfer_id: str, _websocket: WebSocket) -> None:
        return None

    monkeypatch.setattr(
        runner_relay_api,
        "claim_runner_transfer_grant",
        lambda _transfer_id, _capability: grant,
    )
    monkeypatch.setattr(
        runner_relay_api.runner_relay_broker,
        "attach_client",
        attach_client,
    )
    monkeypatch.setattr(
        runner_relay_api.runner_relay_broker,
        "client_frame",
        client_frame,
    )
    monkeypatch.setattr(
        runner_relay_api.runner_relay_broker,
        "send_client_frames",
        send_client_frames,
    )
    monkeypatch.setattr(
        runner_relay_api.runner_relay_broker,
        "detach_client",
        detach_client,
    )

    with auth_client.websocket_connect(f"/api/runner/relay/{transfer_id}") as client_socket:
        client_socket.send_text("capability")
        assert client_socket.receive_json() == {"type": "ready"}
        client_socket.send_bytes(b"opaque-client-frame")
        with pytest.raises(WebSocketDisconnect) as disconnect:
            client_socket.receive_bytes()

    assert disconnect.value.code == 4004


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
