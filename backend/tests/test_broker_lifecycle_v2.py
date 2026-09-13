"""Broker lifecycle v2 stage ordering and runtime ownership contracts."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from yinshi.services.broker_journal import (
    LEGACY_V1_SCHEMA_STATEMENTS,
    BrokerJournal,
    LegacyJournalStateError,
    inspect_legacy_v1_journal,
)
from yinshi.services.broker_protocol import create_signed_request, parse_signed_request
from yinshi.services.broker_runtime import BrokerRuntime, BrokerRuntimeLayout
from yinshi.services.execution_broker import BrokerService, LauncherResult

APPLICATION_ID = "yinshi-desktop"
BROKER_INCARNATION = "b" * 32
DATABASE_INCARNATION = "d" * 32
OPERATION_ID = "a" * 32
APPLICATION_UID = 501


class OrderedLauncher:
    def __init__(self, events: list[str], journal_path: Path) -> None:
        self.events = events
        self.journal_path = journal_path

    def _record(self, action: str, expected_status: str | None) -> None:
        with sqlite3.connect(self.journal_path) as database:
            row = database.execute(
                "SELECT stage_status FROM broker_journal_events "
                "WHERE stage = ? AND event_type = 'stage_outcome'",
                (expected_status,),
            ).fetchone()
        if expected_status is not None:
            assert row is not None
        self.events.append(action)

    async def prepare(self, operation_id: str) -> LauncherResult:
        assert operation_id == OPERATION_ID
        self.events.append("prepare")
        return LauncherResult(status="prepared", error=None)

    async def launch(self, operation_id: str) -> LauncherResult:
        assert operation_id == OPERATION_ID
        with sqlite3.connect(self.journal_path) as database:
            ready = database.execute(
                "SELECT 1 FROM broker_journal_events "
                "WHERE stage = 'runtime_setup' AND stage_status = 'runtime_ready'"
            ).fetchone()
            launch_started = database.execute(
                "SELECT 1 FROM broker_journal_events "
                "WHERE stage = 'launch' AND event_type = 'stage_started'"
            ).fetchone()
        assert ready is not None
        assert launch_started is not None
        self.events.append("launch")
        return LauncherResult(status="launched", error=None)

    async def reclaim(self, operation_id: str) -> LauncherResult:
        assert operation_id == OPERATION_ID
        self.events.append("reclaim")
        return LauncherResult(status="reclaimed", error=None)


class OrderedRuntime(BrokerRuntime):
    def __init__(self, *, layout: BrokerRuntimeLayout, events: list[str]) -> None:
        super().__init__(layout=layout)
        self.events = events

    def setup(self, operation_id: str):
        self.events.append("runtime_setup")
        return super().setup(operation_id)


def request(key: Ed25519PrivateKey) -> bytes:
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


@pytest.mark.asyncio
async def test_lifecycle_orders_prepare_runtime_and_launch_with_durable_starts(
    tmp_path: Path,
) -> None:
    key = Ed25519PrivateKey.generate()
    journal_path = tmp_path / "broker-journal-v2.sqlite3"
    runtime_root = Path(tempfile.mkdtemp(prefix="yb-", dir=Path.home()))
    runtime_root.chmod(0o711)
    events: list[str] = []
    launcher = OrderedLauncher(events, journal_path)
    runtime = OrderedRuntime(layout=BrokerRuntimeLayout(runtime_root=runtime_root), events=events)
    service = BrokerService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=key.public_key(),
        broker_private_key=Ed25519PrivateKey.generate(),
        journal=BrokerJournal(journal_path, application_id=APPLICATION_ID),
        launcher=launcher,
        runtime=runtime,
    )

    await service.handle(request(key), peer_uid=APPLICATION_UID)

    assert events == ["prepare", "runtime_setup", "launch"]
    with sqlite3.connect(journal_path) as database:
        rows = database.execute(
            "SELECT event_type, stage, stage_status FROM broker_journal_events ORDER BY event_id"
        ).fetchall()
    assert rows == [
        ("accepted", None, None),
        ("lifecycle_claimed", None, None),
        ("stage_started", "prepare", None),
        ("stage_outcome", "prepare", "prepared"),
        ("stage_started", "runtime_setup", None),
        ("stage_outcome", "runtime_setup", "runtime_ready"),
        ("stage_started", "launch", None),
        ("stage_outcome", "launch", "launched"),
    ]
    runtime.close(OPERATION_ID)
    shutil.rmtree(runtime_root)


@pytest.mark.asyncio
async def test_runtime_setup_creates_and_retains_broker_private_socket(tmp_path: Path) -> None:
    del tmp_path
    runtime_root = Path(tempfile.mkdtemp(prefix="yb-", dir=Path.home()))
    runtime_root.chmod(0o711)
    runtime = BrokerRuntime(layout=BrokerRuntimeLayout(runtime_root=runtime_root))

    session = runtime.setup(OPERATION_ID)

    directory = runtime_root / OPERATION_ID
    socket_path = directory / "session.sock"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    socket_stat = socket_path.lstat()
    assert stat.S_ISSOCK(socket_stat.st_mode)
    assert stat.S_IMODE(socket_stat.st_mode) == 0o600
    assert runtime.session(OPERATION_ID) is session
    assert session.listener.fileno() >= 0
    assert socket_stat.st_uid == os.geteuid()
    assert socket_stat.st_gid == os.getegid()
    runtime.close(OPERATION_ID)
    shutil.rmtree(runtime_root)


def test_startup_marks_abandoned_stage_unresolved_without_repeating_effect(
    tmp_path: Path,
) -> None:
    application_key = Ed25519PrivateKey.generate()
    frame = request(application_key)
    parsed = parse_signed_request(frame, public_key=application_key.public_key())
    journal = BrokerJournal(
        tmp_path / "broker-journal-v2.sqlite3",
        application_id=APPLICATION_ID,
    )
    owner_token = "o" * 32
    journal.accept(parsed, frame)
    journal.claim_lifecycle(
        parsed,
        owner_token=owner_token,
        broker_incarnation=BROKER_INCARNATION,
    )
    journal.begin_stage(parsed, owner_token=owner_token, stage="prepare")
    launcher = OrderedLauncher([], journal.path)
    runtime_root = Path(tempfile.mkdtemp(prefix="yb-", dir=Path.home()))
    runtime_root.chmod(0o711)
    runtime = BrokerRuntime(layout=BrokerRuntimeLayout(runtime_root=runtime_root))
    service = BrokerService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=application_key.public_key(),
        broker_private_key=Ed25519PrivateKey.generate(),
        journal=journal,
        launcher=launcher,
        runtime=runtime,
    )

    service.recover_startup()

    decision = journal.status(parsed)
    assert decision.state == "unresolved"
    assert decision.stage == "prepare"
    assert decision.response_frame is not None
    assert launcher.events == []
    shutil.rmtree(runtime_root)


def _create_legacy_v1(path: Path, *, schema_version: int = 1) -> None:
    with sqlite3.connect(path) as database:
        for statement in LEGACY_V1_SCHEMA_STATEMENTS:
            database.execute(statement)
        database.execute(
            "INSERT INTO broker_journal_meta (singleton, application_id, schema_version) "
            "VALUES (0, ?, ?)",
            (APPLICATION_ID, schema_version),
        )


def test_legacy_v1_inspection_accepts_absence_without_creating_files(tmp_path: Path) -> None:
    path = tmp_path / "broker-journal.sqlite3"

    inspect_legacy_v1_journal(path, application_id=APPLICATION_ID)

    assert not path.exists()
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_legacy_v1_inspection_is_read_only_for_empty_journal(tmp_path: Path) -> None:
    path = tmp_path / "broker-journal.sqlite3"
    _create_legacy_v1(path)
    before = path.read_bytes()
    before_stat = path.stat()

    inspect_legacy_v1_journal(path, application_id=APPLICATION_ID)

    after_stat = path.stat()
    assert path.read_bytes() == before
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_legacy_v1_inspection_rejects_accepted_work(tmp_path: Path) -> None:
    path = tmp_path / "broker-journal.sqlite3"
    _create_legacy_v1(path)
    with sqlite3.connect(path) as database:
        database.execute(
            "INSERT INTO broker_journal_events (database_incarnation, operation_id, "
            "request_type, event_type, nonce, connection_sequence, payload_digest, "
            "request_frame) VALUES (?, ?, ?, 'accepted', ?, ?, ?, ?)",
            (DATABASE_INCARNATION, OPERATION_ID, "executor.launch", "n" * 32, 1, "f" * 64, b"x"),
        )

    with pytest.raises(LegacyJournalStateError, match="accepted operation"):
        inspect_legacy_v1_journal(path, application_id=APPLICATION_ID)


@pytest.mark.parametrize("suffixes", [("-wal",), ("-shm",), ("-wal", "-shm")])
def test_legacy_v1_inspection_rejects_orphan_sidecars(
    tmp_path: Path,
    suffixes: tuple[str, ...],
) -> None:
    path = tmp_path / "broker-journal.sqlite3"
    sidecars = [Path(f"{path}{suffix}") for suffix in suffixes]
    for sidecar in sidecars:
        sidecar.write_bytes(sidecar.name.encode("ascii"))
    before = {sidecar: (sidecar.read_bytes(), sidecar.stat().st_mtime_ns) for sidecar in sidecars}

    with pytest.raises(LegacyJournalStateError, match="orphaned"):
        inspect_legacy_v1_journal(path, application_id=APPLICATION_ID)

    for sidecar, (content, modified_ns) in before.items():
        assert sidecar.read_bytes() == content
        assert sidecar.stat().st_mtime_ns == modified_ns
    assert not path.exists()


def test_legacy_v1_inspection_rejects_orphan_sidecar_symlink(tmp_path: Path) -> None:
    path = tmp_path / "broker-journal.sqlite3"
    target = tmp_path / "target"
    target.write_bytes(b"unchanged")
    Path(f"{path}-wal").symlink_to(target)

    with pytest.raises(LegacyJournalStateError, match="orphaned"):
        inspect_legacy_v1_journal(path, application_id=APPLICATION_ID)

    assert target.read_bytes() == b"unchanged"
    assert not path.exists()


def test_legacy_v1_inspection_rejects_sidecars_without_changing_them(tmp_path: Path) -> None:
    path = tmp_path / "broker-journal.sqlite3"
    _create_legacy_v1(path)
    sidecar = Path(f"{path}-wal")
    sidecar.write_bytes(b"unresolved")
    before = sidecar.read_bytes()

    with pytest.raises(LegacyJournalStateError, match="sidecar"):
        inspect_legacy_v1_journal(path, application_id=APPLICATION_ID)

    assert sidecar.read_bytes() == before


def test_legacy_v1_inspection_rejects_future_and_malformed_state(tmp_path: Path) -> None:
    future = tmp_path / "future.sqlite3"
    _create_legacy_v1(future, schema_version=2)
    with pytest.raises(LegacyJournalStateError, match="version is unsupported"):
        inspect_legacy_v1_journal(future, application_id=APPLICATION_ID)

    malformed = tmp_path / "malformed.sqlite3"
    malformed.write_bytes(b"not sqlite")
    with pytest.raises(LegacyJournalStateError):
        inspect_legacy_v1_journal(malformed, application_id=APPLICATION_ID)


@pytest.mark.asyncio
async def test_launch_capacity_is_recorded_after_durable_stage_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yinshi.services import execution_broker

    key = Ed25519PrivateKey.generate()
    journal_path = tmp_path / "broker-journal-v2.sqlite3"
    runtime_root = Path(tempfile.mkdtemp(prefix="yb-", dir=Path.home()))
    runtime_root.chmod(0o711)
    events: list[str] = []
    launcher = OrderedLauncher(events, journal_path)
    runtime = OrderedRuntime(layout=BrokerRuntimeLayout(runtime_root=runtime_root), events=events)
    service = BrokerService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=key.public_key(),
        broker_private_key=Ed25519PrivateKey.generate(),
        journal=BrokerJournal(journal_path, application_id=APPLICATION_ID),
        launcher=launcher,
        runtime=runtime,
    )

    async def remain_pending() -> LauncherResult:
        await asyncio.sleep(30)
        return LauncherResult(status="launched", error=None)

    blocker = asyncio.create_task(remain_pending())
    original_setup = runtime.setup

    def fill_capacity(operation_id: str) -> object:
        result = original_setup(operation_id)
        service._abandoned_effects.add(blocker)
        return result

    monkeypatch.setattr(runtime, "setup", fill_capacity)
    monkeypatch.setattr(execution_broker, "_MAX_ABANDONED_EFFECTS", 1)
    try:
        response = await service.handle(request(key), peer_uid=APPLICATION_UID)
    finally:
        blocker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocker

    assert response
    assert events == ["prepare", "runtime_setup"]
    with sqlite3.connect(journal_path) as database:
        launch_rows = database.execute(
            "SELECT event_type, unresolved_reason FROM broker_journal_events "
            "WHERE stage = 'launch' ORDER BY event_id"
        ).fetchall()
    assert launch_rows == [("stage_started", None), ("stage_unresolved", "timeout_unknown")]
    runtime.close(OPERATION_ID)
    shutil.rmtree(runtime_root)
