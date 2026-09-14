"""Focused tests for concrete broker and root-launcher daemons."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sqlite3
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from yinshi import execution_daemons
from yinshi.execution_daemons import (
    APPLICATION_PUBLIC_KEY_PATH,
    BROKER_JOURNAL_PATH,
    BROKER_PRIVATE_KEY_PATH,
    LAUNCHER_FRAME_BYTES_MAX,
    LAUNCHER_SOCKET_PATH,
    DaemonError,
    LauncherSocketClient,
    ManagedRootWorker,
    acquire_systemd_socket,
    broker_connection_handler,
    canonical_launcher_request,
    launcher_connection_handler,
    load_broker_config,
    load_ed25519_private_key,
    resolve_execution_gate,
    resolve_prepare_gate,
    resolve_reclaim_gate,
    serve,
)
from yinshi.root_launcher import BROKER_UID, RootLauncher
from yinshi.services.broker_journal import BrokerJournal, LegacyJournalStateError
from yinshi.services.broker_protocol import (
    BROKER_FRAME_BYTES_MAX,
    create_signed_request,
    parse_signed_request,
    verify_broker_response,
)
from yinshi.services.broker_replica_journal_v2 import (
    BrokerReplicaJournalV2,
    LegacyReplicaJournalStateError,
    ReplicaJournalV2SyncError,
)
from yinshi.services.broker_replica_lifecycle import StageOutcomeUnknown, StageRejected
from yinshi.services.broker_replica_lifecycle_v2 import (
    BrokerReplicaLifecycleCoordinatorV2,
)
from yinshi.services.execution_broker import (
    BrokerControlService,
    BrokerService,
    LauncherResult,
    LauncherTimeoutError,
    LauncherTransportError,
    LauncherVerificationError,
)

BROKER_INCARNATION = "b" * 32
DATABASE_INCARNATION = "d" * 32
OPERATION_ID = "a" * 32
APPLICATION_UID = 501


class RecordingRuntime:
    def setup(self, operation_id: str) -> object:
        return operation_id


class RecordingLauncher:
    """Record fixed launch identities."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def prepare(self, operation_id: str) -> LauncherResult:
        del operation_id
        return LauncherResult(status="prepared", error=None)

    async def launch(self, operation_id: str) -> LauncherResult:
        self.calls.append(operation_id)
        return LauncherResult(status="launched", error=None)

    async def reclaim(self, operation_id: str) -> LauncherResult:
        del operation_id
        return LauncherResult(status="reclaimed", error=None)


def _request(key: Ed25519PrivateKey) -> bytes:
    return create_signed_request(
        private_key=key,
        protocol_version="yinshi-broker-v1",
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        connection_sequence=1,
        operation_id=OPERATION_ID,
        request_type="executor.launch",
        nonce="c" * 32,
        payload={"generation": 1},
    )


def _peer(uid: int) -> Callable[[socket.socket], int]:
    return lambda _socket: uid


@pytest.fixture
def socket_path() -> Generator[Path, None, None]:
    path = Path("/tmp") / f"yinshi-test-{uuid.uuid4().hex}.sock"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _listener(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(8)
    return listener


async def _raw_exchange(path: Path, framed: bytes) -> bytes:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        try:
            writer.write(framed)
            await writer.drain()
            writer.write_eof()
            return await asyncio.wait_for(reader.read(), timeout=1.0)
        except (BrokenPipeError, ConnectionResetError):
            return b""
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def _exchange(path: Path, body: bytes) -> bytes:
    return await _raw_exchange(path, len(body).to_bytes(4, "big") + body)


@pytest.mark.asyncio
async def test_broker_serves_one_peer_bound_signed_exchange(
    tmp_path: Path,
    socket_path: Path,
) -> None:
    application_key = Ed25519PrivateKey.generate()
    launcher = RecordingLauncher()
    service = BrokerService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=application_key.public_key(),
        broker_private_key=Ed25519PrivateKey.generate(),
        journal=BrokerJournal(tmp_path / "journal.sqlite3", application_id="yinshi-desktop"),
        launcher=launcher,
        runtime=RecordingRuntime(),
    )
    daemon = await serve(
        _listener(socket_path),
        handle=broker_connection_handler(service=service, peer_uid_of=_peer(APPLICATION_UID)),
    )
    request = _request(application_key)
    try:
        framed = await _exchange(socket_path, request)
    finally:
        daemon.close()
        await daemon.wait_closed()

    size = int.from_bytes(framed[:4], "big")
    response = framed[4:]
    assert size == len(response)
    verified = verify_broker_response(
        response,
        public_key=service.broker_public_key,
        expected_request=parse_signed_request(
            request,
            public_key=application_key.public_key(),
        ),
    )
    assert verified.status == "ok"
    assert launcher.calls == [OPERATION_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{}",
        b"x" * (BROKER_FRAME_BYTES_MAX + 1),
    ],
)
async def test_broker_closes_invalid_frames_without_launch(
    tmp_path: Path,
    socket_path: Path,
    body: bytes,
) -> None:
    application_key = Ed25519PrivateKey.generate()
    launcher = RecordingLauncher()
    service = BrokerService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=application_key.public_key(),
        broker_private_key=Ed25519PrivateKey.generate(),
        journal=BrokerJournal(tmp_path / "journal.sqlite3", application_id="yinshi-desktop"),
        launcher=launcher,
        runtime=RecordingRuntime(),
    )
    daemon = await serve(
        _listener(socket_path),
        handle=broker_connection_handler(service=service, peer_uid_of=_peer(APPLICATION_UID)),
    )
    try:
        answer = await _exchange(socket_path, body)
    finally:
        daemon.close()
        await daemon.wait_closed()
    assert answer == b""
    assert launcher.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("framed", [b"\x00\x00", b"\x00\x00\x00\x08short"])
async def test_broker_closes_truncated_frames(
    tmp_path: Path,
    socket_path: Path,
    framed: bytes,
) -> None:
    application_key = Ed25519PrivateKey.generate()
    launcher = RecordingLauncher()
    service = BrokerService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=application_key.public_key(),
        broker_private_key=Ed25519PrivateKey.generate(),
        journal=BrokerJournal(tmp_path / "journal.sqlite3", application_id="yinshi-desktop"),
        launcher=launcher,
        runtime=RecordingRuntime(),
    )
    daemon = await serve(
        _listener(socket_path),
        handle=broker_connection_handler(service=service, peer_uid_of=_peer(APPLICATION_UID)),
    )
    try:
        assert await _raw_exchange(socket_path, framed) == b""
    finally:
        daemon.close()
        await daemon.wait_closed()
    assert launcher.calls == []


@pytest.mark.asyncio
async def test_broker_authenticates_peer_before_reading_frame(
    tmp_path: Path,
    socket_path: Path,
) -> None:
    application_key = Ed25519PrivateKey.generate()
    launcher = RecordingLauncher()
    service = BrokerService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=application_key.public_key(),
        broker_private_key=Ed25519PrivateKey.generate(),
        journal=BrokerJournal(tmp_path / "journal.sqlite3", application_id="yinshi-desktop"),
        launcher=launcher,
        runtime=RecordingRuntime(),
    )
    daemon = await serve(
        _listener(socket_path),
        handle=broker_connection_handler(
            service=service,
            peer_uid_of=_peer(APPLICATION_UID + 1),
        ),
    )
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        assert await asyncio.wait_for(reader.read(), timeout=0.25) == b""
    finally:
        writer.close()
        await writer.wait_closed()
        daemon.close()
        await daemon.wait_closed()
    assert launcher.calls == []


@pytest.mark.asyncio
async def test_connection_logs_only_stable_code(
    socket_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail(
        _reader: asyncio.StreamReader,
        _writer: asyncio.StreamWriter,
    ) -> None:
        raise RuntimeError("sentinel-secret-path")

    caplog.set_level(logging.WARNING)
    daemon = await serve(_listener(socket_path), handle=fail)
    try:
        assert await _exchange(socket_path, b"request") == b""
    finally:
        daemon.close()
        await daemon.wait_closed()
    assert "internal_error" in caplog.text
    assert "sentinel-secret-path" not in caplog.text


@pytest.mark.asyncio
async def test_launcher_rejects_wrong_uid_before_frame_read(socket_path: Path) -> None:
    daemon = await serve(
        _listener(socket_path),
        handle=launcher_connection_handler(
            launcher=RootLauncher(),
            peer_uid_of=_peer(BROKER_UID + 1),
        ),
    )
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        assert await asyncio.wait_for(reader.read(), timeout=0.25) == b""
    finally:
        writer.close()
        await writer.wait_closed()
        daemon.close()
        await daemon.wait_closed()


@pytest.mark.asyncio
async def test_shutdown_has_final_deadline(
    socket_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def resist_once(
        _reader: asyncio.StreamReader,
        _writer: asyncio.StreamWriter,
    ) -> None:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    monkeypatch.setattr(execution_daemons, "SHUTDOWN_GRACE_SECONDS", 0.01)
    caplog.set_level(logging.ERROR)
    daemon = await serve(_listener(socket_path), handle=resist_once)
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    await started.wait()

    daemon.close()
    await asyncio.wait_for(daemon.wait_closed(), timeout=0.25)

    assert "shutdown_timeout" in caplog.text
    release.set()
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0)
    del reader


@pytest.mark.asyncio
async def test_disabled_root_launcher_returns_canonical_rejection(
    socket_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []
    daemon = await serve(
        _listener(socket_path),
        handle=launcher_connection_handler(
            launcher=RootLauncher(command_runner=commands.append),
            peer_uid_of=_peer(BROKER_UID),
        ),
    )
    try:
        framed = await _exchange(
            socket_path,
            canonical_launcher_request(OPERATION_ID),
        )
    finally:
        daemon.close()
        await daemon.wait_closed()
    assert b'"error":"launch_disabled"' in framed
    assert commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "error"),
    (("prepare", "prepare_disabled"), ("reclaim", "reclaim_disabled")),
)
async def test_disabled_replica_actions_return_canonical_rejection(
    socket_path: Path,
    action: str,
    error: str,
) -> None:
    request = canonical_launcher_request(OPERATION_ID).replace(
        b'"action":"launch"', f'"action":"{action}"'.encode()
    )
    daemon = await serve(
        _listener(socket_path),
        handle=launcher_connection_handler(
            launcher=RootLauncher(),
            peer_uid_of=_peer(BROKER_UID),
        ),
    )
    try:
        framed = await _exchange(socket_path, request)
    finally:
        daemon.close()
        await daemon.wait_closed()

    assert f'"error":"{error}"'.encode() in framed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_error"),
    (
        ("prepare", "prepare_disabled"),
        ("launch", "launch_disabled"),
        ("reclaim", "reclaim_disabled"),
    ),
)
async def test_launcher_client_supports_action_bound_lifecycle_requests(
    socket_path: Path,
    action: str,
    expected_error: str,
) -> None:
    daemon = await serve(
        _listener(socket_path),
        handle=launcher_connection_handler(
            launcher=RootLauncher(),
            peer_uid_of=_peer(BROKER_UID),
        ),
    )
    client = LauncherSocketClient(socket_path=socket_path, peer_uid_of=_peer(0))
    try:
        result = await getattr(client, action)(OPERATION_ID)
    finally:
        daemon.close()
        await daemon.wait_closed()

    assert result == LauncherResult(status="rejected", error=expected_error)


def test_launcher_response_rejects_success_for_a_different_action() -> None:
    body = execution_daemons._launcher_response(
        OPERATION_ID,
        action="prepare",
        status="prepared",
        error=None,
    )
    with pytest.raises(LauncherVerificationError, match="response invalid"):
        execution_daemons._parse_launcher_response(
            OPERATION_ID,
            body,
            action="launch",
        )


@pytest.mark.asyncio
async def test_launcher_client_rejects_non_root_peer(socket_path: Path) -> None:
    daemon = await serve(
        _listener(socket_path),
        handle=launcher_connection_handler(
            launcher=RootLauncher(),
            peer_uid_of=_peer(BROKER_UID),
        ),
    )
    client = LauncherSocketClient(socket_path=socket_path, peer_uid_of=_peer(1))
    try:
        with pytest.raises(LauncherTransportError):
            await client.launch(OPERATION_ID)
    finally:
        daemon.close()
        await daemon.wait_closed()


def test_raw_private_key_requires_exact_protected_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "broker.key"
    path.write_bytes(os.urandom(32))
    path.chmod(0o600)
    assert isinstance(load_ed25519_private_key(path), Ed25519PrivateKey)

    path.chmod(0o640)
    with pytest.raises(DaemonError, match="config_invalid"):
        load_ed25519_private_key(path)


@pytest.mark.parametrize(
    "replacement",
    [
        {"application_uid": True},
        {"application_uid": "996"},
        {"broker_incarnation": "short"},
        {"journal_path": "relative.sqlite3"},
        {"journal_path": "/var/lib/yinshi/control/broker-journal.sqlite3"},
        {"launcher_socket_path": None},
    ],
)
def test_broker_config_rejects_coerced_values_before_journal_creation(
    tmp_path: Path,
    replacement: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "application_uid": 996,
        "application_public_key_path": str(APPLICATION_PUBLIC_KEY_PATH),
        "broker_private_key_path": str(BROKER_PRIVATE_KEY_PATH),
        "journal_path": str(BROKER_JOURNAL_PATH),
        "broker_incarnation": "b" * 32,
        "database_incarnation": "d" * 32,
        "launcher_socket_path": str(LAUNCHER_SOCKET_PATH),
    }
    values.update(replacement)
    path = tmp_path / "broker.json"
    path.write_text(json.dumps(values))
    path.chmod(0o640)

    with pytest.raises(DaemonError, match="config_invalid"):
        load_broker_config(path, owner_uid=os.geteuid())
    assert not (tmp_path / "broker-journal.sqlite3").exists()


def test_broker_config_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "broker.json"
    path.write_text('{"application_uid":996,"application_uid":997}')
    path.chmod(0o640)
    with pytest.raises(DaemonError, match="config_invalid"):
        load_broker_config(path, owner_uid=os.geteuid())


@pytest.mark.parametrize("entry_point", ["broker_main", "launcher_main"])
def test_startup_logs_only_stable_code(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    entry_point: str,
) -> None:
    async def fail(**_kwargs: object) -> int:
        raise RuntimeError("sentinel-secret-path")

    target = "_run_broker" if entry_point == "broker_main" else "_run_launcher"
    monkeypatch.setattr(execution_daemons, target, fail)
    caplog.set_level(logging.ERROR)
    main = getattr(execution_daemons, entry_point)

    assert main(["--systemd-socket"]) == 1
    assert "startup_rejected" in caplog.text
    assert "sentinel-secret-path" not in caplog.text


def test_systemd_activation_and_execution_gate_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LISTEN_PID", raising=False)
    monkeypatch.delenv("LISTEN_FDS", raising=False)
    with pytest.raises(DaemonError, match="socket_activation_invalid"):
        acquire_systemd_socket()

    monkeypatch.delenv("YINSHI_LAUNCH_EXECUTION_ENABLED", raising=False)
    assert resolve_execution_gate(explicit=False) is False
    assert resolve_execution_gate(explicit=True) is False
    monkeypatch.setenv("YINSHI_LAUNCH_EXECUTION_ENABLED", "true")
    assert resolve_execution_gate(explicit=False) is False
    assert resolve_execution_gate(explicit=True) is True

    for environment, resolver in (
        ("YINSHI_REPLICA_PREPARE_ENABLED", resolve_prepare_gate),
        ("YINSHI_REPLICA_RECLAIM_ENABLED", resolve_reclaim_gate),
    ):
        monkeypatch.delenv(environment, raising=False)
        assert resolver(explicit=True) is False
        monkeypatch.setenv(environment, "true")
        assert resolver(explicit=False) is False
        assert resolver(explicit=True) is True
        monkeypatch.setenv(environment, "1")
        with pytest.raises(DaemonError, match="config_invalid"):
            resolver(explicit=True)


@pytest.mark.parametrize(
    ("stage", "environment"),
    list(execution_daemons.REPLICA_STAGE_ENABLED_ENVIRONMENTS.items()),
)
def test_replica_lifecycle_and_stage_gates_require_explicit_and_environment(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    environment: str,
) -> None:
    monkeypatch.delenv(execution_daemons.REPLICA_LIFECYCLE_ENABLED_ENVIRONMENT, raising=False)
    assert execution_daemons.resolve_replica_lifecycle_gate(explicit=True) is False
    monkeypatch.setenv(execution_daemons.REPLICA_LIFECYCLE_ENABLED_ENVIRONMENT, "true")
    assert execution_daemons.resolve_replica_lifecycle_gate(explicit=False) is False
    assert execution_daemons.resolve_replica_lifecycle_gate(explicit=True) is True

    monkeypatch.delenv(environment, raising=False)
    assert execution_daemons.resolve_replica_stage_gate(stage, explicit=True) is False
    monkeypatch.setenv(environment, "true")
    assert execution_daemons.resolve_replica_stage_gate(stage, explicit=False) is False
    assert execution_daemons.resolve_replica_stage_gate(stage, explicit=True) is True
    monkeypatch.setenv(environment, "1")
    with pytest.raises(DaemonError, match="config_invalid"):
        execution_daemons.resolve_replica_stage_gate(stage, explicit=True)


def test_unknown_replica_stage_gate_is_invalid() -> None:
    with pytest.raises(DaemonError, match="config_invalid"):
        execution_daemons.resolve_replica_stage_gate("unknown", explicit=True)


def _production_config(tmp_path: Path) -> execution_daemons.BrokerDaemonConfig:
    application_key = Ed25519PrivateKey.generate()
    application_public_path = tmp_path / "application.pub"
    application_public_path.write_bytes(
        application_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    application_public_path.chmod(0o600)
    broker_private_path = tmp_path / "broker.key"
    broker_private_path.write_bytes(b"\x11" * 32)
    broker_private_path.chmod(0o600)
    return execution_daemons.BrokerDaemonConfig(
        application_uid=os.geteuid(),
        application_public_key_path=application_public_path,
        broker_private_key_path=broker_private_path,
        journal_path=tmp_path / "broker.sqlite3",
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        launcher_socket_path=tmp_path / "launcher.sock",
    )


def _prepare_production_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path]:
    replica_journal_v2 = tmp_path / "replica-v2.sqlite3"
    incoming = tmp_path / "incoming"
    publication = tmp_path / "publication"
    incoming.mkdir(mode=0o700)
    publication.mkdir(mode=0o700)
    monkeypatch.setattr(
        execution_daemons,
        "BROKER_REPLICA_JOURNAL_V2_PATH",
        replica_journal_v2,
    )
    monkeypatch.setattr(execution_daemons, "BROKER_ARTIFACT_INCOMING_ROOT", incoming)
    monkeypatch.setattr(
        execution_daemons,
        "BROKER_PUBLICATION_STAGING_ROOT",
        publication,
    )
    return replica_journal_v2, incoming, publication


def test_production_composition_builds_fixed_fail_closed_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replica_journal_v2, incoming, publication = _prepare_production_roots(
        tmp_path,
        monkeypatch,
    )
    legacy_replica_v1 = tmp_path / "legacy-replica-v1.sqlite3"
    legacy_replica_v1.write_bytes(b"legacy-replica-v1-bytes")
    legacy_before = legacy_replica_v1.stat().st_mtime_ns
    monkeypatch.setattr(
        execution_daemons,
        "BROKER_REPLICA_JOURNAL_PATH",
        legacy_replica_v1,
    )
    for environment in (
        execution_daemons.REPLICA_LIFECYCLE_ENABLED_ENVIRONMENT,
        *execution_daemons.REPLICA_STAGE_ENABLED_ENVIRONMENTS.values(),
    ):
        monkeypatch.delenv(environment, raising=False)

    composition = execution_daemons._build_broker_composition(_production_config(tmp_path))

    assert isinstance(composition.control_service, BrokerControlService)
    assert isinstance(composition.replica_journal, BrokerReplicaJournalV2)
    assert composition.replica_journal.path == replica_journal_v2
    assert isinstance(
        composition.replica_coordinator,
        BrokerReplicaLifecycleCoordinatorV2,
    )
    assert composition.incoming_store._root == incoming
    assert composition.publication_store._root == publication
    assert composition.replica_lifecycle_enabled is False
    assert composition.stage_gates_enabled == {
        stage: False for stage in execution_daemons.REPLICA_LIFECYCLE_STAGES
    }
    assert set(composition.stage_effects) == set(execution_daemons.REPLICA_LIFECYCLE_STAGES)
    assert legacy_replica_v1.read_bytes() == b"legacy-replica-v1-bytes"
    assert legacy_replica_v1.stat().st_mtime_ns == legacy_before


@pytest.mark.asyncio
async def test_unfinished_production_stages_remain_fail_closed_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_production_roots(tmp_path, monkeypatch)
    monkeypatch.setenv(execution_daemons.REPLICA_LIFECYCLE_ENABLED_ENVIRONMENT, "true")
    for environment in execution_daemons.REPLICA_STAGE_ENABLED_ENVIRONMENTS.values():
        monkeypatch.setenv(environment, "true")
    composition = execution_daemons._build_broker_composition(
        _production_config(tmp_path),
        replica_lifecycle_explicit=True,
        stage_explicit={stage: True for stage in execution_daemons.REPLICA_LIFECYCLE_STAGES},
    )
    context = SimpleNamespace(request=SimpleNamespace(operation_id=OPERATION_ID))

    assert composition.replica_lifecycle_enabled is True
    assert all(composition.stage_gates_enabled.values())
    for stage in ("admission", "drain", "export", "reclaim"):
        with pytest.raises(StageRejected) as rejected:
            await composition.stage_effects[stage].apply(context)
        assert rejected.value.status == execution_daemons._STAGE_REJECTION_STATUSES[stage]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", execution_daemons.REPLICA_LIFECYCLE_STAGES)
async def test_production_stage_effects_fail_closed_with_typed_outcomes(
    stage: str,
) -> None:
    context = SimpleNamespace(request=SimpleNamespace(operation_id=OPERATION_ID))
    disabled = execution_daemons._FailClosedStageEffect(stage)

    with pytest.raises(StageRejected) as rejected:
        await disabled.apply(context)
    assert rejected.value.status == execution_daemons._STAGE_REJECTION_STATUSES[stage]
    assert rejected.value.receipt_id == execution_daemons._gate_rejected_receipt_id(
        stage,
        OPERATION_ID,
    )
    with pytest.raises(StageOutcomeUnknown) as unknown:
        await disabled.reconcile(context)
    assert unknown.value.reason == execution_daemons._STAGE_UNRESOLVED_REASONS[stage]


@pytest.mark.asyncio
async def test_disabled_stage_gate_does_not_call_delegate() -> None:
    class UnexpectedDelegate:
        async def apply(self, _context: object) -> object:
            raise AssertionError("delegate apply called")

        async def reconcile(self, _context: object) -> object:
            raise AssertionError("delegate reconcile called")

    context = SimpleNamespace(request=SimpleNamespace(operation_id=OPERATION_ID))
    gated = execution_daemons._GatedStageEffect(
        "ingest",
        UnexpectedDelegate(),
        enabled=False,
    )
    with pytest.raises(StageRejected, match="artifact_rejected"):
        await gated.apply(context)
    with pytest.raises(StageOutcomeUnknown, match="transfer_unknown"):
        await gated.reconcile(context)


def test_broker_main_forwards_replica_gate_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def run_broker(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(execution_daemons, "_run_broker", run_broker)
    assert (
        execution_daemons.broker_main(
            [
                "--systemd-socket",
                "--replica-lifecycle-enabled",
                "--replica-ingest-enabled",
                "--replica-publish-enabled",
            ]
        )
        == 0
    )
    assert captured["replica_lifecycle_explicit"] is True
    assert captured["stage_explicit"] == {
        "ingest": True,
        "verify": False,
        "publish": True,
        "admission": False,
        "drain": False,
        "export": False,
        "reclaim": False,
    }


class SleepingLauncher(RootLauncher):
    """Model a privileged action that keeps the worker child busy."""

    def __init__(self, *, delay: float, execution_enabled: bool = False) -> None:
        super().__init__(execution_enabled=execution_enabled)
        self.delay = delay

    def execute(self, frame: bytes, *, peer_uid: int) -> tuple[str, ...]:
        del frame, peer_uid
        time.sleep(self.delay)
        return ()


@pytest.mark.asyncio
async def test_root_worker_executes_action_in_child_and_reaps_it() -> None:
    worker = ManagedRootWorker(RootLauncher(), timeout_seconds=5.0)
    await worker.start()
    child_pid = worker.child_pid
    try:
        assert child_pid > 0
        assert child_pid != os.getpid()
        reply = await worker.apply(canonical_launcher_request(OPERATION_ID), peer_uid=BROKER_UID)
        assert b'"error":"launch_disabled"' in reply
    finally:
        await worker.stop()

    assert worker.child_pid == 0
    with pytest.raises(ProcessLookupError):
        os.killpg(child_pid, 0)


@pytest.mark.asyncio
async def test_root_worker_timeout_kills_group_and_disables_until_restart() -> None:
    worker = ManagedRootWorker(
        SleepingLauncher(delay=30.0, execution_enabled=True),
        timeout_seconds=0.1,
    )
    await worker.start()
    child_pid = worker.child_pid
    try:
        with pytest.raises(DaemonError, match="worker_timeout"):
            await worker.apply(canonical_launcher_request(OPERATION_ID), peer_uid=BROKER_UID)
        assert worker.disabled is True
        assert worker.child_pid == 0
        with pytest.raises(ProcessLookupError):
            os.killpg(child_pid, 0)
        reply = await worker.apply(
            canonical_launcher_request("b" * 32),
            peer_uid=BROKER_UID,
        )
        assert b'"error":"launch_disabled"' in reply
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_root_worker_cancellation_kills_child_and_disables_worker() -> None:
    worker = ManagedRootWorker(
        SleepingLauncher(delay=30.0, execution_enabled=True),
        timeout_seconds=5.0,
    )
    await worker.start()
    child_pid = worker.child_pid
    task = asyncio.create_task(
        worker.apply(canonical_launcher_request(OPERATION_ID), peer_uid=BROKER_UID)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker.disabled is True
        assert worker.child_pid == 0
        with pytest.raises(ProcessLookupError):
            os.killpg(child_pid, 0)
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_root_worker_keeps_event_loop_responsive() -> None:
    ticks = 0
    stop = asyncio.Event()

    async def tick() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    worker = ManagedRootWorker(
        SleepingLauncher(delay=0.25, execution_enabled=True),
        timeout_seconds=5.0,
    )
    await worker.start()
    ticker = asyncio.create_task(tick())
    try:
        reply = await worker.apply(canonical_launcher_request(OPERATION_ID), peer_uid=BROKER_UID)
    finally:
        stop.set()
        await ticker
        await worker.stop()

    assert b'"status":"launched"' in reply
    assert ticks >= 5


@pytest.mark.asyncio
async def test_launcher_daemon_sends_no_reply_after_worker_deadline(
    socket_path: Path,
) -> None:
    worker = ManagedRootWorker(
        SleepingLauncher(delay=30.0, execution_enabled=True),
        timeout_seconds=0.1,
    )
    await worker.start()
    daemon = await serve(
        _listener(socket_path),
        handle=launcher_connection_handler(
            launcher=worker,
            peer_uid_of=_peer(BROKER_UID),
        ),
    )
    try:
        assert await _exchange(socket_path, canonical_launcher_request(OPERATION_ID)) == b""
        assert worker.disabled is True
    finally:
        daemon.close()
        await daemon.wait_closed()
        await worker.stop()


@pytest.mark.asyncio
async def test_root_worker_serializes_concurrent_connections(socket_path: Path) -> None:
    worker = ManagedRootWorker(
        SleepingLauncher(delay=0.1, execution_enabled=True),
        timeout_seconds=5.0,
    )
    await worker.start()
    daemon = await serve(
        _listener(socket_path),
        handle=launcher_connection_handler(
            launcher=worker,
            peer_uid_of=_peer(BROKER_UID),
        ),
    )
    try:
        first = asyncio.create_task(_exchange(socket_path, canonical_launcher_request("b" * 32)))
        await asyncio.sleep(0.03)
        second = asyncio.create_task(_exchange(socket_path, canonical_launcher_request("c" * 32)))
        first_reply, second_reply = await asyncio.gather(first, second)
    finally:
        daemon.close()
        await daemon.wait_closed()
        await worker.stop()

    assert b'"operation_id":"' + b"b" * 32 + b'"' in first_reply
    assert b'"operation_id":"' + b"c" * 32 + b'"' in second_reply


@pytest.mark.asyncio
async def test_launcher_client_deadline_raises_typed_timeout(
    socket_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stall(
        _reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await asyncio.sleep(5)
        finally:
            writer.close()

    daemon = await serve(_listener(socket_path), handle=stall)
    client = LauncherSocketClient(socket_path=socket_path, peer_uid_of=_peer(0))
    monkeypatch.setattr(execution_daemons, "LAUNCHER_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(LauncherTimeoutError):
            await client.launch(OPERATION_ID)
    finally:
        daemon.close()
        await daemon.wait_closed()


@pytest.mark.asyncio
async def test_launcher_client_malformed_response_is_verification_failure(
    socket_path: Path,
) -> None:
    async def garbage(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await execution_daemons._read_frame(reader, maximum=LAUNCHER_FRAME_BYTES_MAX)
        execution_daemons._write_frame(writer, b"garbage")
        await writer.drain()
        writer.close()

    daemon = await serve(_listener(socket_path), handle=garbage)
    client = LauncherSocketClient(socket_path=socket_path, peer_uid_of=_peer(0))
    try:
        with pytest.raises(LauncherVerificationError, match="response invalid") as excinfo:
            await client.launch(OPERATION_ID)
    finally:
        daemon.close()
        await daemon.wait_closed()

    assert not isinstance(excinfo.value, LauncherTimeoutError)
    assert not isinstance(excinfo.value, LauncherTransportError)


@pytest.mark.asyncio
async def test_launcher_client_invalid_frame_is_verification_failure(
    socket_path: Path,
) -> None:
    async def oversized(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await execution_daemons._read_frame(reader, maximum=LAUNCHER_FRAME_BYTES_MAX)
        execution_daemons._write_frame(writer, b"x" * (LAUNCHER_FRAME_BYTES_MAX + 1))
        await writer.drain()
        writer.close()

    daemon = await serve(_listener(socket_path), handle=oversized)
    client = LauncherSocketClient(socket_path=socket_path, peer_uid_of=_peer(0))
    try:
        with pytest.raises(LauncherVerificationError, match="framing invalid"):
            await client.launch(OPERATION_ID)
    finally:
        daemon.close()
        await daemon.wait_closed()


@pytest.mark.asyncio
async def test_launcher_clean_eof_is_transport_unknown_with_exact_replay(
    tmp_path: Path,
    socket_path: Path,
) -> None:
    contacts = 0

    async def close_without_reply(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        nonlocal contacts
        await execution_daemons._read_frame(reader, maximum=LAUNCHER_FRAME_BYTES_MAX)
        contacts += 1
        writer.close()

    daemon = await serve(_listener(socket_path), handle=close_without_reply)
    application_key = Ed25519PrivateKey.generate()
    journal = BrokerJournal(tmp_path / "journal.sqlite3", application_id="yinshi-desktop")
    service = BrokerService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=application_key.public_key(),
        broker_private_key=Ed25519PrivateKey.generate(),
        journal=journal,
        launcher=LauncherSocketClient(socket_path=socket_path, peer_uid_of=_peer(0)),
        runtime=RecordingRuntime(),
    )
    request_frame = _request(application_key)
    try:
        response = await service.handle(request_frame, peer_uid=APPLICATION_UID)
        replay = await service.handle(request_frame, peer_uid=APPLICATION_UID)
    finally:
        daemon.close()
        await daemon.wait_closed()

    with sqlite3.connect(journal.path) as database:
        reason = database.execute(
            "SELECT unresolved_reason FROM broker_journal_events "
            "WHERE event_type = 'stage_unresolved'"
        ).fetchone()[0]
    assert reason == "transport_unknown"
    assert replay == response
    assert contacts == 1


@pytest.mark.asyncio
async def test_broker_checks_v1_before_recovery_and_listener_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class StartupConfig:
        application_uid = APPLICATION_UID

    class StartupService:
        application_uid = APPLICATION_UID

        def recover_startup(self) -> None:
            events.append("recover_launch")

    class StartupCoordinator:
        async def recover_incomplete(self) -> None:
            events.append("recover_replica")

    class StartupComposition:
        launch_service = StartupService()
        control_service = StartupService()
        replica_coordinator = StartupCoordinator()

    def load() -> StartupConfig:
        events.append("load")
        return StartupConfig()

    def inspect(path: Path, *, application_id: str) -> None:
        assert path == execution_daemons.LEGACY_BROKER_JOURNAL_PATH
        assert application_id == execution_daemons.BROKER_APPLICATION_ID
        events.append("inspect_v1")

    def inspect_replica(path: Path, *, application_id: str) -> None:
        assert path == execution_daemons.BROKER_REPLICA_JOURNAL_PATH
        assert application_id == execution_daemons.BROKER_APPLICATION_ID
        events.append("inspect_replica_v1")

    def build(
        _config: object,
        *,
        replica_lifecycle_explicit: bool,
        stage_explicit: object,
    ) -> StartupComposition:
        assert replica_lifecycle_explicit is False
        assert stage_explicit is None
        events.append("build_v2")
        return StartupComposition()

    async def run_daemon(
        _handler: object,
        *,
        connection_timeout_seconds: float,
    ) -> int:
        assert connection_timeout_seconds == execution_daemons.BROKER_TRANSPORT_TIMEOUT_SECONDS
        events.append("listen")
        return 0

    monkeypatch.setattr(execution_daemons, "load_broker_config", load)
    monkeypatch.setattr(execution_daemons, "inspect_legacy_v1_journal", inspect)
    monkeypatch.setattr(
        execution_daemons,
        "inspect_legacy_replica_v1_journal",
        inspect_replica,
    )
    monkeypatch.setattr(execution_daemons, "_build_broker_composition", build)
    monkeypatch.setattr(execution_daemons, "_run_daemon", run_daemon)

    assert await execution_daemons._run_broker() == 0
    assert events == [
        "load",
        "inspect_v1",
        "inspect_replica_v1",
        "build_v2",
        "recover_launch",
        "recover_replica",
        "listen",
    ]


@pytest.mark.asyncio
async def test_broker_rejects_unsafe_v1_before_v2_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = False

    class StartupConfig:
        application_uid = APPLICATION_UID

    def reject(_path: Path, *, application_id: str) -> None:
        assert application_id == execution_daemons.BROKER_APPLICATION_ID
        raise LegacyJournalStateError("legacy broker journal sidecar is orphaned")

    def build(_config: object) -> object:
        nonlocal built
        built = True
        return object()

    monkeypatch.setattr(execution_daemons, "load_broker_config", StartupConfig)
    monkeypatch.setattr(execution_daemons, "inspect_legacy_v1_journal", reject)
    monkeypatch.setattr(execution_daemons, "_build_broker", build)

    with pytest.raises(LegacyJournalStateError, match="sidecar"):
        await execution_daemons._run_broker()
    assert built is False


def test_root_and_broker_deadlines_are_strictly_nested() -> None:
    assert (
        execution_daemons.ROOT_WORKER_TIMEOUT_SECONDS < execution_daemons.LAUNCHER_TIMEOUT_SECONDS
    )
    assert (
        execution_daemons.LAUNCHER_TIMEOUT_SECONDS < execution_daemons.BROKER_EFFECT_TIMEOUT_SECONDS
    )
    assert (
        execution_daemons.BROKER_EFFECT_TIMEOUT_SECONDS
        < execution_daemons.CONNECTION_TIMEOUT_SECONDS
    )
    assert (
        len(execution_daemons.REPLICA_LIFECYCLE_STAGES)
        * execution_daemons.BROKER_EFFECT_TIMEOUT_SECONDS
        < execution_daemons.BROKER_REPLICA_LIFECYCLE_TIMEOUT_SECONDS
        < execution_daemons.BROKER_TRANSPORT_TIMEOUT_SECONDS
    )


def test_replica_v2_binds_stable_response_key_and_rejects_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replica_journal_v2, _incoming, _publication = _prepare_production_roots(
        tmp_path,
        monkeypatch,
    )
    config = _production_config(tmp_path)
    composition = execution_daemons._build_broker_composition(config)
    assert composition.replica_journal.path == replica_journal_v2

    changed_incarnation = replace(config, broker_incarnation="c" * 32)
    rebuilt = execution_daemons._build_broker_composition(changed_incarnation)
    assert isinstance(rebuilt.replica_coordinator, BrokerReplicaLifecycleCoordinatorV2)
    assert rebuilt.replica_journal.path == replica_journal_v2

    rotated_private_path = tmp_path / "rotated.key"
    rotated_private_path.write_bytes(b"\x22" * 32)
    rotated_private_path.chmod(0o600)
    rotated_response = replace(config, broker_private_key_path=rotated_private_path)
    with pytest.raises(ReplicaJournalV2SyncError, match="metadata is not configured"):
        execution_daemons._build_broker_composition(rotated_response)

    rotated_request_path = tmp_path / "rotated-application.pub"
    rotated_request_path.write_bytes(
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    rotated_request_path.chmod(0o600)
    rotated_request = replace(config, application_public_key_path=rotated_request_path)
    with pytest.raises(ReplicaJournalV2SyncError, match="metadata is not configured"):
        execution_daemons._build_broker_composition(rotated_request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "legacy replica journal contains an accepted operation",
        "legacy replica journal contains malformed history",
    ],
)
async def test_broker_rejects_unsafe_replica_v1_before_all_later_work(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    events: list[str] = []

    class StartupConfig:
        application_uid = APPLICATION_UID

    def inspect_launch(_path: Path, *, application_id: str) -> None:
        assert application_id == execution_daemons.BROKER_APPLICATION_ID
        events.append("inspect_launch_v1")

    def inspect_replica(_path: Path, *, application_id: str) -> None:
        assert application_id == execution_daemons.BROKER_APPLICATION_ID
        raise LegacyReplicaJournalStateError(reason)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("later startup work must not run")

    monkeypatch.setattr(execution_daemons, "load_broker_config", StartupConfig)
    monkeypatch.setattr(execution_daemons, "inspect_legacy_v1_journal", inspect_launch)
    monkeypatch.setattr(
        execution_daemons,
        "inspect_legacy_replica_v1_journal",
        inspect_replica,
    )
    monkeypatch.setattr(execution_daemons, "_build_broker_composition", fail)
    monkeypatch.setattr(execution_daemons, "_run_daemon", fail)

    with pytest.raises(LegacyReplicaJournalStateError, match="replica journal"):
        await execution_daemons._run_broker()
    assert events == ["inspect_launch_v1"]
