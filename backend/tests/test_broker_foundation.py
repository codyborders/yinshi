"""Security and durability contracts for the disabled broker foundation."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from yinshi.services import execution_broker
from yinshi.services.broker_journal import (
    BrokerJournal,
    JournalCommitAbsent,
    JournalDecision,
    JournalSyncError,
)
from yinshi.services.broker_protocol import (
    BrokerProtocolError,
    BrokerRequest,
    create_signed_request,
    parse_signed_request,
    verify_broker_response,
)
from yinshi.services.execution_broker import (
    BrokerService,
    LauncherClient,
    LauncherResult,
    LauncherTimeoutError,
    LauncherTransportError,
    LauncherVerificationError,
)

APPLICATION_ID = "yinshi-desktop"
BROKER_INCARNATION = "b" * 32
DATABASE_INCARNATION = "d" * 32
OPERATION_ID = "a" * 32
NONCE = "c" * 32
APPLICATION_UID = 501


class LifecycleLauncher:
    async def prepare(self, operation_id: str) -> LauncherResult:
        del operation_id
        return LauncherResult(status="prepared", error=None)

    async def reclaim(self, operation_id: str) -> LauncherResult:
        del operation_id
        return LauncherResult(status="reclaimed", error=None)


class RecordingRuntime:
    def setup(self, operation_id: str) -> object:
        return operation_id


class RecordingLauncher(LifecycleLauncher):
    """Record launch operation IDs and return a fixed result."""

    def __init__(self, result: LauncherResult | None = None) -> None:
        self.calls: list[str] = []
        self.result = result or LauncherResult(status="launched", error=None)

    async def launch(self, operation_id: str) -> LauncherResult:
        self.calls.append(operation_id)
        return self.result


def _request(
    private_key: Ed25519PrivateKey,
    *,
    broker_incarnation: str = BROKER_INCARNATION,
    database_incarnation: str = DATABASE_INCARNATION,
    sequence: int = 1,
    operation_id: str = OPERATION_ID,
    request_type: str = "executor.launch",
    nonce: str = NONCE,
    payload: dict[str, object] | None = None,
) -> bytes:
    return create_signed_request(
        private_key=private_key,
        protocol_version="yinshi-broker-v1",
        broker_incarnation=broker_incarnation,
        database_incarnation=database_incarnation,
        connection_sequence=sequence,
        operation_id=operation_id,
        request_type=request_type,
        nonce=nonce,
        payload={"generation": 1} if payload is None else payload,
    )


def _service(
    tmp_path: Path,
    private_key: Ed25519PrivateKey,
    launcher: LauncherClient,
    *,
    retry_wait_seconds: float = 0.25,
    launch_timeout_seconds: float = 30.0,
    service_type: type[BrokerService] = BrokerService,
) -> BrokerService:
    return service_type(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=private_key.public_key(),
        broker_private_key=Ed25519PrivateKey.generate(),
        journal=BrokerJournal(
            tmp_path / "broker-journal.sqlite3",
            application_id=APPLICATION_ID,
        ),
        launcher=launcher,
        runtime=RecordingRuntime(),
        retry_wait_seconds=retry_wait_seconds,
        launch_timeout_seconds=launch_timeout_seconds,
    )


@pytest.mark.asyncio
async def test_broker_journals_intent_and_outcome_before_reply(tmp_path: Path) -> None:
    """A launch crosses no effect boundary before accepted intent is durable."""
    application_key = Ed25519PrivateKey.generate()
    launcher = RecordingLauncher()
    service = _service(tmp_path, application_key, launcher)

    request = _request(application_key)
    response = await service.handle(request, peer_uid=APPLICATION_UID)

    verified = verify_broker_response(
        response,
        public_key=service.broker_public_key,
        expected_request=parse_signed_request(
            request,
            public_key=application_key.public_key(),
        ),
    )
    assert verified.status == "ok"
    assert verified.result == {
        "launcher_status": "launched",
        "lifecycle_stage": "launch",
    }
    assert launcher.calls == [OPERATION_ID]
    with sqlite3.connect(tmp_path / "broker-journal.sqlite3") as db:
        events = db.execute(
            "SELECT event_type FROM broker_journal_events ORDER BY event_id"
        ).fetchall()
    assert events == [
        ("accepted",),
        ("lifecycle_claimed",),
        ("stage_started",),
        ("stage_outcome",),
        ("stage_started",),
        ("stage_outcome",),
        ("stage_started",),
        ("stage_outcome",),
    ]


@pytest.mark.parametrize(
    "request_changes",
    [
        {"broker_incarnation": "e" * 32},
        {"database_incarnation": "e" * 32},
        {"sequence": 2},
        {"operation_id": "e" * 32},
        {"request_type": "executor.other"},
        {"nonce": "e" * 32},
        {"payload": {"generation": 2}},
    ],
)
@pytest.mark.asyncio
async def test_signed_response_is_bound_to_every_expected_request_field(
    tmp_path: Path,
    request_changes: dict[str, object],
) -> None:
    """A broker response cannot satisfy any different request identity."""
    application_key = Ed25519PrivateKey.generate()
    service = _service(tmp_path, application_key, RecordingLauncher())
    first_request = _request(application_key)
    response = await service.handle(first_request, peer_uid=APPLICATION_UID)
    other_request = _request(application_key, **request_changes)

    with pytest.raises(BrokerProtocolError, match="request identity"):
        verify_broker_response(
            response,
            public_key=service.broker_public_key,
            expected_request=parse_signed_request(
                other_request,
                public_key=application_key.public_key(),
            ),
        )


@pytest.mark.asyncio
async def test_identical_retry_returns_original_signed_response(tmp_path: Path) -> None:
    """A byte-identical accepted retry never contacts the launcher twice."""
    application_key = Ed25519PrivateKey.generate()
    launcher = RecordingLauncher()
    service = _service(tmp_path, application_key, launcher)
    request = _request(application_key)

    first = await service.handle(request, peer_uid=APPLICATION_UID)
    second = await service.handle(request, peer_uid=APPLICATION_UID)

    assert second == first
    assert launcher.calls == [OPERATION_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ({"generation": 2}, "operation payload mismatch"),
        ({"generation": 1, "command": "id"}, "forbidden launch control"),
    ],
)
async def test_changed_or_effect_controlling_payload_fails_without_launch(
    tmp_path: Path,
    replacement: dict[str, object],
    message: str,
) -> None:
    """An operation identity cannot change payload or control root-owned launch data."""
    application_key = Ed25519PrivateKey.generate()
    launcher = RecordingLauncher()
    service = _service(tmp_path, application_key, launcher)
    await service.handle(_request(application_key), peer_uid=APPLICATION_UID)

    with pytest.raises(BrokerProtocolError, match=message):
        await service.handle(
            _request(application_key, sequence=2, nonce="e" * 32, payload=replacement),
            peer_uid=APPLICATION_UID,
        )
    assert launcher.calls == [OPERATION_ID]


@pytest.mark.asyncio
async def test_replay_guards_fail_before_launcher(tmp_path: Path) -> None:
    """Duplicate nonces and stale connection sequences fail closed."""
    application_key = Ed25519PrivateKey.generate()
    launcher = RecordingLauncher()
    service = _service(tmp_path, application_key, launcher)
    await service.handle(_request(application_key), peer_uid=APPLICATION_UID)

    with pytest.raises(BrokerProtocolError, match="duplicate nonce"):
        await service.handle(
            _request(
                application_key,
                sequence=2,
                operation_id="e" * 32,
                nonce=NONCE,
            ),
            peer_uid=APPLICATION_UID,
        )
    with pytest.raises(BrokerProtocolError, match="stale connection sequence"):
        await service.handle(
            _request(
                application_key,
                sequence=1,
                operation_id="f" * 32,
                nonce="f" * 32,
            ),
            peer_uid=APPLICATION_UID,
        )
    assert launcher.calls == [OPERATION_ID]


@pytest.mark.asyncio
async def test_peer_schema_digest_and_signature_fail_before_launcher(tmp_path: Path) -> None:
    """Kernel peer identity remains a separate mandatory authentication layer."""
    application_key = Ed25519PrivateKey.generate()
    launcher = RecordingLauncher()
    service = _service(tmp_path, application_key, launcher)
    request = _request(application_key)

    with pytest.raises(BrokerProtocolError, match="peer UID"):
        await service.handle(request, peer_uid=APPLICATION_UID + 1)

    bad_signature = bytearray(request)
    bad_signature[-2] = ord("A") if bad_signature[-2] != ord("A") else ord("B")
    with pytest.raises(BrokerProtocolError):
        await service.handle(bytes(bad_signature), peer_uid=APPLICATION_UID)

    unknown = request[:-1] + b',"unknown":true}'
    with pytest.raises(BrokerProtocolError):
        await service.handle(unknown, peer_uid=APPLICATION_UID)
    assert launcher.calls == []


@pytest.mark.asyncio
async def test_launcher_failure_is_terminal_and_bounded(tmp_path: Path) -> None:
    """A launcher failure is signed, persisted, bounded, and never retried."""
    application_key = Ed25519PrivateKey.generate()
    launcher = RecordingLauncher(LauncherResult(status="rejected", error="unit_failed"))
    service = _service(tmp_path, application_key, launcher)
    request = _request(application_key)

    first = await service.handle(request, peer_uid=APPLICATION_UID)
    second = await service.handle(request, peer_uid=APPLICATION_UID)

    verified = verify_broker_response(
        first,
        public_key=service.broker_public_key,
        expected_request=parse_signed_request(
            request,
            public_key=application_key.public_key(),
        ),
    )
    assert verified.status == "error"
    assert verified.error == "unit_failed"
    assert len(first) < 4096
    assert second == first
    assert launcher.calls == [OPERATION_ID]


class FailingJournal(BrokerJournal):
    """Model a journal synchronization failure before launcher contact."""

    def accept(self, request: object, request_frame: bytes) -> object:
        raise JournalSyncError("broker journal synchronization failed")


@pytest.mark.asyncio
async def test_journal_sync_failure_makes_no_launcher_call(tmp_path: Path) -> None:
    """Uncertain persistence never converts into launch authority."""
    application_key = Ed25519PrivateKey.generate()
    launcher = RecordingLauncher()
    service = BrokerService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=application_key.public_key(),
        broker_private_key=Ed25519PrivateKey.generate(),
        journal=FailingJournal(
            tmp_path / "broker-journal.sqlite3",
            application_id=APPLICATION_ID,
        ),
        launcher=launcher,
        runtime=RecordingRuntime(),
    )

    with pytest.raises(JournalSyncError):
        await service.handle(_request(application_key), peer_uid=APPLICATION_UID)
    assert launcher.calls == []


class CancellableLauncher(LifecycleLauncher):
    """Expose a completion barrier for cancellation durability testing."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def launch(self, operation_id: str) -> LauncherResult:
        self.started.set()
        await self.finish.wait()
        return LauncherResult(status="launched", error=None)


class CancellationResistantLauncher(CancellableLauncher):
    """Consume task cancellation before waiting for external completion."""

    async def launch(self, operation_id: str) -> LauncherResult:
        self.started.set()
        current_task = asyncio.current_task()
        assert current_task is not None
        current_task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            await self.finish.wait()
        return LauncherResult(status="launched", error=None)


class SelfCancellingLauncher(LifecycleLauncher):
    """Terminate the launcher task through cancellation."""

    async def launch(self, operation_id: str) -> LauncherResult:
        raise asyncio.CancelledError


class FinalizerCancellingBrokerService(BrokerService):
    """Inject shutdown cancellation into owner finalization."""

    async def _finalize_lifecycle(self, request: BrokerRequest) -> bytes:
        current_task = asyncio.current_task()
        assert current_task is not None
        current_task.cancel()
        return await super()._finalize_lifecycle(request)


@pytest.mark.asyncio
async def test_cancellation_waits_for_durable_launcher_outcome(tmp_path: Path) -> None:
    """Cancellation cannot erase an accepted intent or completed launcher result."""
    application_key = Ed25519PrivateKey.generate()
    launcher = CancellableLauncher()
    service = BrokerService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=application_key.public_key(),
        broker_private_key=Ed25519PrivateKey.generate(),
        journal=BrokerJournal(
            tmp_path / "broker-journal.sqlite3",
            application_id=APPLICATION_ID,
        ),
        launcher=launcher,
        runtime=RecordingRuntime(),
    )

    task = asyncio.create_task(service.handle(_request(application_key), peer_uid=APPLICATION_UID))
    await launcher.started.wait()
    task.cancel()
    launcher.finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    with sqlite3.connect(tmp_path / "broker-journal.sqlite3") as db:
        events = db.execute(
            "SELECT event_type FROM broker_journal_events ORDER BY event_id"
        ).fetchall()
    assert events == [
        ("accepted",),
        ("lifecycle_claimed",),
        ("stage_started",),
        ("stage_outcome",),
        ("stage_started",),
        ("stage_outcome",),
        ("stage_started",),
        ("stage_outcome",),
    ]


@pytest.mark.asyncio
async def test_repeated_cancellation_records_one_unresolved_outcome(tmp_path: Path) -> None:
    """Repeated cancellation cannot interrupt durable owner finalization."""
    application_key = Ed25519PrivateKey.generate()
    launcher = CancellationResistantLauncher()
    service = _service(
        tmp_path,
        application_key,
        launcher,
        launch_timeout_seconds=0.05,
    )
    request = _request(application_key)
    task = asyncio.create_task(service.handle(request, peer_uid=APPLICATION_UID))
    await launcher.started.wait()

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    parsed_request = parse_signed_request(
        request,
        public_key=application_key.public_key(),
    )
    with sqlite3.connect(tmp_path / "broker-journal.sqlite3") as db:
        events = db.execute(
            "SELECT event_type FROM broker_journal_events ORDER BY event_id"
        ).fetchall()
    assert events == [
        ("accepted",),
        ("lifecycle_claimed",),
        ("stage_started",),
        ("stage_outcome",),
        ("stage_started",),
        ("stage_outcome",),
        ("stage_started",),
        ("stage_unresolved",),
    ]

    retry_launcher = RecordingLauncher()
    retry_service = _service(
        tmp_path,
        application_key,
        retry_launcher,
        launch_timeout_seconds=0.05,
    )
    response = await retry_service.handle(request, peer_uid=APPLICATION_UID)

    assert retry_launcher.calls == []
    verified = verify_broker_response(
        response,
        public_key=service.broker_public_key,
        expected_request=parsed_request,
    )
    assert verified.error == "launcher_outcome_unresolved"

    launcher.finish.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    with sqlite3.connect(tmp_path / "broker-journal.sqlite3") as db:
        final_events = db.execute(
            "SELECT event_type FROM broker_journal_events ORDER BY event_id"
        ).fetchall()
    assert final_events == events


@pytest.mark.asyncio
async def test_self_cancelling_launcher_records_unresolved_without_spin(
    tmp_path: Path,
) -> None:
    """Launcher-originated cancellation becomes one unresolved outcome."""
    application_key = Ed25519PrivateKey.generate()
    service = _service(tmp_path, application_key, SelfCancellingLauncher())
    request = _request(application_key)

    response = await asyncio.wait_for(
        service.handle(request, peer_uid=APPLICATION_UID),
        timeout=0.25,
    )

    verified = verify_broker_response(
        response,
        public_key=service.broker_public_key,
        expected_request=parse_signed_request(
            request,
            public_key=application_key.public_key(),
        ),
    )
    assert verified.error == "launcher_outcome_unresolved"
    with sqlite3.connect(tmp_path / "broker-journal.sqlite3") as db:
        events = db.execute(
            "SELECT event_type FROM broker_journal_events ORDER BY event_id"
        ).fetchall()
    assert events == [
        ("accepted",),
        ("lifecycle_claimed",),
        ("stage_started",),
        ("stage_outcome",),
        ("stage_started",),
        ("stage_outcome",),
        ("stage_started",),
        ("stage_unresolved",),
    ]


@pytest.mark.asyncio
async def test_finalizer_cancellation_records_unresolved_without_spin(
    tmp_path: Path,
) -> None:
    """Shutdown cancellation cannot strand a claimed operation."""
    application_key = Ed25519PrivateKey.generate()
    launcher = CancellableLauncher()
    service = _service(
        tmp_path,
        application_key,
        launcher,
        service_type=FinalizerCancellingBrokerService,
    )
    request = _request(application_key)

    response = await asyncio.wait_for(
        service.handle(request, peer_uid=APPLICATION_UID),
        timeout=0.25,
    )

    verified = verify_broker_response(
        response,
        public_key=service.broker_public_key,
        expected_request=parse_signed_request(
            request,
            public_key=application_key.public_key(),
        ),
    )
    assert verified.error == "launcher_outcome_unresolved"

    launcher.finish.set()
    await asyncio.sleep(0)
    retry_launcher = RecordingLauncher()
    retry_service = _service(tmp_path, application_key, retry_launcher)
    await retry_service.handle(request, peer_uid=APPLICATION_UID)
    assert retry_launcher.calls == []
    with sqlite3.connect(tmp_path / "broker-journal.sqlite3") as db:
        events = db.execute(
            "SELECT event_type FROM broker_journal_events ORDER BY event_id"
        ).fetchall()
    assert events == [
        ("accepted",),
        ("lifecycle_claimed",),
        ("stage_started",),
        ("stage_unresolved",),
    ]


@pytest.mark.asyncio
async def test_prestart_finalizer_cancellation_records_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation before the finalizer starts cannot spin or launch."""
    application_key = Ed25519PrivateKey.generate()
    launcher = RecordingLauncher()
    service = _service(tmp_path, application_key, launcher)
    request = _request(application_key)
    real_create_task = asyncio.create_task

    def create_cancelled_task(
        coroutine: Coroutine[Any, Any, bytes],
    ) -> asyncio.Task[bytes]:
        task = real_create_task(coroutine)
        task.cancel()
        return task

    monkeypatch.setattr(execution_broker.asyncio, "create_task", create_cancelled_task)
    response = await asyncio.wait_for(
        service.handle(request, peer_uid=APPLICATION_UID),
        timeout=0.25,
    )
    monkeypatch.undo()

    verified = verify_broker_response(
        response,
        public_key=service.broker_public_key,
        expected_request=parse_signed_request(
            request,
            public_key=application_key.public_key(),
        ),
    )
    assert verified.error == "launcher_outcome_unresolved"
    assert launcher.calls == []

    retry_launcher = RecordingLauncher()
    retry_service = _service(tmp_path, application_key, retry_launcher)
    await retry_service.handle(request, peer_uid=APPLICATION_UID)
    assert retry_launcher.calls == []
    with sqlite3.connect(tmp_path / "broker-journal.sqlite3") as db:
        events = db.execute(
            "SELECT event_type FROM broker_journal_events ORDER BY event_id"
        ).fetchall()
    assert events == [
        ("accepted",),
        ("lifecycle_claimed",),
        ("stage_started",),
        ("stage_unresolved",),
    ]


class GatedLauncher(LifecycleLauncher):
    """Hold one launch while concurrent broker requests race."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def launch(self, operation_id: str) -> LauncherResult:
        self.calls.append(operation_id)
        self.started.set()
        await self.finish.wait()
        return LauncherResult(status="launched", error=None)


@pytest.mark.asyncio
async def test_concurrent_services_assign_one_exclusive_launcher_owner(tmp_path: Path) -> None:
    """Concurrent broker instances contact the root launcher exactly once."""
    application_key = Ed25519PrivateKey.generate()
    launcher = GatedLauncher()
    request = _request(application_key)
    first = _service(tmp_path, application_key, launcher)
    second = _service(tmp_path, application_key, launcher)

    owner = asyncio.create_task(first.handle(request, peer_uid=APPLICATION_UID))
    await launcher.started.wait()
    retry = asyncio.create_task(second.handle(request, peer_uid=APPLICATION_UID))
    await asyncio.sleep(0)
    launcher.finish.set()
    owner_response, retry_response = await asyncio.gather(owner, retry)

    assert launcher.calls == [OPERATION_ID]
    assert retry_response == owner_response


class TransportFailingLauncher(LifecycleLauncher):
    """Model a lost authenticated launcher response."""

    async def launch(self, operation_id: str) -> LauncherResult:
        del operation_id
        raise LauncherTransportError("response lost")


@pytest.mark.asyncio
async def test_ambiguous_launcher_transport_failure_remains_unresolved(tmp_path: Path) -> None:
    """A lost launcher response never becomes a confirmed rejection or stop."""
    application_key = Ed25519PrivateKey.generate()
    service = _service(tmp_path, application_key, TransportFailingLauncher())

    request = _request(application_key)
    response = await service.handle(request, peer_uid=APPLICATION_UID)
    verified = verify_broker_response(
        response,
        public_key=service.broker_public_key,
        expected_request=parse_signed_request(
            request,
            public_key=application_key.public_key(),
        ),
    )

    assert verified.error == "launcher_outcome_unresolved"
    assert verified.result == {
        "launcher_status": "unresolved",
        "lifecycle_stage": "launch",
    }
    with sqlite3.connect(tmp_path / "broker-journal.sqlite3") as db:
        outcomes = db.execute(
            "SELECT COUNT(*) FROM broker_journal_events "
            "WHERE event_type = 'stage_outcome' AND stage = 'launch'"
        ).fetchone()[0]
    assert outcomes == 0


def test_journal_rejects_future_or_shadowed_schema(tmp_path: Path) -> None:
    """Unsupported authority schemas fail before a broker can accept work."""
    future = tmp_path / "future.sqlite3"
    BrokerJournal(future, application_id=APPLICATION_ID)
    with sqlite3.connect(future) as db:
        db.execute("DROP TRIGGER broker_journal_meta_reject_update")
        db.execute("UPDATE broker_journal_meta SET schema_version = schema_version + 1")
        db.execute(
            "CREATE TRIGGER broker_journal_meta_reject_update BEFORE UPDATE "
            "ON broker_journal_meta BEGIN SELECT RAISE(ABORT, "
            "'broker journal metadata is immutable'); END"
        )
        db.commit()
    with pytest.raises(JournalSyncError, match="schema"):
        BrokerJournal(future, application_id=APPLICATION_ID)

    shadowed = tmp_path / "shadowed.sqlite3"
    BrokerJournal(shadowed, application_id=APPLICATION_ID)
    with sqlite3.connect(shadowed) as db:
        db.execute("DROP TRIGGER broker_journal_reject_delete")
        db.execute(
            "CREATE TRIGGER broker_journal_reject_delete BEFORE DELETE "
            "ON broker_journal_events BEGIN SELECT 1; END"
        )
        db.commit()
    with pytest.raises(JournalSyncError, match="schema"):
        BrokerJournal(shadowed, application_id=APPLICATION_ID)


class FaultJournal(BrokerJournal):
    """Inject one controlled commit failure into a selected transition."""

    def __init__(self, path: Path, *, application_id: str) -> None:
        super().__init__(path, application_id=application_id)
        self.pending_mode: str | None = None
        self.pending_method: str | None = None
        self.pending_stage: str | None = None
        self.active_mode: str | None = None

    def arm(self, method: str, *, mode: str, stage: str | None = None) -> None:
        self.pending_mode = mode
        self.pending_method = method
        self.pending_stage = stage

    def _maybe_arm(self, method: str, stage: str | None) -> None:
        if self.pending_method != method:
            return
        if self.pending_stage is not None and self.pending_stage != stage:
            return
        self.active_mode = self.pending_mode
        self.pending_mode = None
        self.pending_method = None
        self.pending_stage = None

    def _commit_transaction(self, database: sqlite3.Connection) -> None:
        if self.active_mode is None:
            super()._commit_transaction(database)
            return
        mode = self.active_mode
        if mode != "verification":
            self.active_mode = None
        if mode == "after_commit":
            super()._commit_transaction(database)
        raise sqlite3.OperationalError("simulated uncertain commit")

    def _verify_intended_event(
        self,
        identity: tuple[str, str, str],
        event_type: str,
        exact: dict[str, object],
    ) -> bool:
        if self.active_mode == "verification":
            self.active_mode = None
            raise JournalSyncError("broker journal verification is unavailable")
        return super()._verify_intended_event(identity, event_type, exact)

    def accept(self, request: BrokerRequest, request_frame: bytes) -> JournalDecision:
        self._maybe_arm("accept", None)
        return super().accept(request, request_frame)

    def claim_lifecycle(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        broker_incarnation: str,
    ) -> JournalDecision:
        self._maybe_arm("claim", None)
        return super().claim_lifecycle(
            request,
            owner_token=owner_token,
            broker_incarnation=broker_incarnation,
        )

    def begin_stage(self, request: BrokerRequest, *, owner_token: str, stage: str) -> None:
        self._maybe_arm("begin_stage", stage)
        super().begin_stage(request, owner_token=owner_token, stage=stage)

    def record_stage_outcome(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
        stage_status: str,
        stage_error: str | None = None,
        response_frame: bytes | None = None,
    ) -> None:
        self._maybe_arm("outcome", stage)
        super().record_stage_outcome(
            request,
            owner_token=owner_token,
            stage=stage,
            stage_status=stage_status,
            stage_error=stage_error,
            response_frame=response_frame,
        )

    def record_stage_unresolved(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
        reason: str,
        response_frame: bytes,
    ) -> None:
        self._maybe_arm("unresolved", stage)
        super().record_stage_unresolved(
            request,
            owner_token=owner_token,
            stage=stage,
            reason=reason,
            response_frame=response_frame,
        )


class CountingLauncher(LifecycleLauncher):
    """Count prepare and launch effects."""

    def __init__(self) -> None:
        self.prepares: list[str] = []
        self.calls: list[str] = []

    async def prepare(self, operation_id: str) -> LauncherResult:
        self.prepares.append(operation_id)
        return LauncherResult(status="prepared", error=None)

    async def launch(self, operation_id: str) -> LauncherResult:
        self.calls.append(operation_id)
        return LauncherResult(status="launched", error=None)


class TimeoutLauncher(LifecycleLauncher):
    async def launch(self, operation_id: str) -> LauncherResult:
        del operation_id
        raise LauncherTimeoutError("launcher outcome timed out")


class UnconfirmedLauncher(LifecycleLauncher):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def launch(self, operation_id: str) -> LauncherResult:
        self.calls.append(operation_id)
        raise LauncherTransportError("response lost")


class InvalidResponseLauncher(LifecycleLauncher):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def launch(self, operation_id: str) -> LauncherResult:
        self.calls.append(operation_id)
        raise LauncherVerificationError("launcher response invalid")


def _fault_service(
    journal: BrokerJournal,
    private_key: Ed25519PrivateKey,
    launcher: LauncherClient,
    *,
    retry_wait_seconds: float = 0.05,
) -> BrokerService:
    return BrokerService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=private_key.public_key(),
        broker_private_key=Ed25519PrivateKey.generate(),
        journal=journal,
        launcher=launcher,
        runtime=RecordingRuntime(),
        retry_wait_seconds=retry_wait_seconds,
    )


def _journal_rows(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as database:
        rows = database.execute(
            "SELECT event_type, stage, unresolved_reason, stage_status "
            "FROM broker_journal_events ORDER BY event_id"
        ).fetchall()
    return [tuple(row) for row in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["accept", "claim", "begin_stage"])
@pytest.mark.parametrize("mode", ["before_commit", "after_commit", "verification"])
async def test_pre_effect_commit_faults_are_resolved_before_effects(
    tmp_path: Path,
    transition: str,
    mode: str,
) -> None:
    application_key = Ed25519PrivateKey.generate()
    launcher = CountingLauncher()
    journal = FaultJournal(tmp_path / "broker-journal.sqlite3", application_id=APPLICATION_ID)
    journal.arm(transition, mode=mode, stage="prepare" if transition == "begin_stage" else None)
    service = _fault_service(journal, application_key, launcher)
    request_frame = _request(application_key)

    if mode == "after_commit":
        response = await service.handle(request_frame, peer_uid=APPLICATION_UID)
        assert (
            verify_broker_response(
                response,
                public_key=service.broker_public_key,
                expected_request=parse_signed_request(
                    request_frame,
                    public_key=application_key.public_key(),
                ),
            ).status
            == "ok"
        )
        assert launcher.prepares == [OPERATION_ID]
        assert launcher.calls == [OPERATION_ID]
        return

    with pytest.raises(JournalSyncError) as excinfo:
        await service.handle(request_frame, peer_uid=APPLICATION_UID)
    assert launcher.prepares == []
    assert launcher.calls == []
    if mode == "before_commit":
        assert isinstance(excinfo.value, JournalCommitAbsent)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["before_commit", "after_commit", "verification"])
async def test_outcome_commit_faults_never_repeat_launch(
    tmp_path: Path,
    mode: str,
) -> None:
    application_key = Ed25519PrivateKey.generate()
    launcher = CountingLauncher()
    journal = FaultJournal(tmp_path / "broker-journal.sqlite3", application_id=APPLICATION_ID)
    journal.arm("outcome", mode=mode, stage="launch")
    service = _fault_service(journal, application_key, launcher)
    request_frame = _request(application_key)

    if mode == "verification":
        with pytest.raises(JournalSyncError):
            await service.handle(request_frame, peer_uid=APPLICATION_UID)
        with pytest.raises(LauncherTransportError):
            await service.handle(request_frame, peer_uid=APPLICATION_UID)
        assert launcher.calls == [OPERATION_ID]
        return

    response = await service.handle(request_frame, peer_uid=APPLICATION_UID)
    verified = verify_broker_response(
        response,
        public_key=service.broker_public_key,
        expected_request=parse_signed_request(
            request_frame,
            public_key=application_key.public_key(),
        ),
    )
    if mode == "before_commit":
        assert verified.error == "launcher_outcome_unresolved"
        assert _journal_rows(journal.path)[-1] == (
            "stage_unresolved",
            "launch",
            "sqlite_commit_unknown",
            None,
        )
    else:
        assert verified.status == "ok"
    assert await service.handle(request_frame, peer_uid=APPLICATION_UID) == response
    assert launcher.calls == [OPERATION_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepare", "runtime_setup"])
async def test_nonlaunch_outcome_commit_absence_stays_at_exact_stage(
    tmp_path: Path,
    stage: str,
) -> None:
    application_key = Ed25519PrivateKey.generate()
    launcher = CountingLauncher()
    journal = FaultJournal(tmp_path / "broker-journal.sqlite3", application_id=APPLICATION_ID)
    journal.arm("outcome", mode="before_commit", stage=stage)
    service = _fault_service(journal, application_key, launcher)
    request_frame = _request(application_key)

    response = await service.handle(request_frame, peer_uid=APPLICATION_UID)

    verified = verify_broker_response(
        response,
        public_key=service.broker_public_key,
        expected_request=parse_signed_request(
            request_frame,
            public_key=application_key.public_key(),
        ),
    )
    assert verified.error == "launcher_outcome_unresolved"
    assert verified.result == {
        "launcher_status": "unresolved",
        "lifecycle_stage": stage,
    }
    assert _journal_rows(journal.path)[-1] == (
        "stage_unresolved",
        stage,
        "sqlite_commit_unknown",
        None,
    )
    assert launcher.prepares == [OPERATION_ID]
    assert launcher.calls == []
    assert await service.handle(request_frame, peer_uid=APPLICATION_UID) == response
    assert launcher.prepares == [OPERATION_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["before_commit", "after_commit", "verification"])
async def test_unresolved_commit_faults_send_only_verified_responses(
    tmp_path: Path,
    mode: str,
) -> None:
    application_key = Ed25519PrivateKey.generate()
    launcher = UnconfirmedLauncher()
    journal = FaultJournal(tmp_path / "broker-journal.sqlite3", application_id=APPLICATION_ID)
    journal.arm("unresolved", mode=mode, stage="launch")
    service = _fault_service(journal, application_key, launcher)
    request_frame = _request(application_key)

    if mode == "after_commit":
        response = await service.handle(request_frame, peer_uid=APPLICATION_UID)
        assert (
            verify_broker_response(
                response,
                public_key=service.broker_public_key,
                expected_request=parse_signed_request(
                    request_frame,
                    public_key=application_key.public_key(),
                ),
            ).error
            == "launcher_outcome_unresolved"
        )
        assert await service.handle(request_frame, peer_uid=APPLICATION_UID) == response
    else:
        with pytest.raises(JournalSyncError):
            await service.handle(request_frame, peer_uid=APPLICATION_UID)
        with pytest.raises(LauncherTransportError):
            await service.handle(request_frame, peer_uid=APPLICATION_UID)
    assert launcher.calls == [OPERATION_ID]


@pytest.mark.asyncio
async def test_invalid_launcher_response_maps_to_verification_unknown(tmp_path: Path) -> None:
    application_key = Ed25519PrivateKey.generate()
    journal = BrokerJournal(tmp_path / "broker-journal.sqlite3", application_id=APPLICATION_ID)
    launcher = InvalidResponseLauncher()
    service = _fault_service(journal, application_key, launcher)
    request_frame = _request(application_key)

    response = await service.handle(request_frame, peer_uid=APPLICATION_UID)

    with sqlite3.connect(journal.path) as database:
        reason = database.execute(
            "SELECT unresolved_reason FROM broker_journal_events "
            "WHERE event_type = 'stage_unresolved'"
        ).fetchone()[0]
    assert reason == "verification_unknown"
    assert await service.handle(request_frame, peer_uid=APPLICATION_UID) == response
    assert launcher.calls == [OPERATION_ID]


@pytest.mark.asyncio
async def test_typed_launcher_timeout_maps_to_timeout_unknown(tmp_path: Path) -> None:
    application_key = Ed25519PrivateKey.generate()
    journal = BrokerJournal(tmp_path / "broker-journal.sqlite3", application_id=APPLICATION_ID)
    service = _fault_service(journal, application_key, TimeoutLauncher())

    response = await service.handle(_request(application_key), peer_uid=APPLICATION_UID)

    with sqlite3.connect(journal.path) as database:
        reason = database.execute(
            "SELECT unresolved_reason FROM broker_journal_events "
            "WHERE event_type = 'stage_unresolved'"
        ).fetchone()[0]
    assert response
    assert reason == "timeout_unknown"
