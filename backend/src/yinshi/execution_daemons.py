"""Concrete systemd-activated broker and root-launcher daemons."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import socket
import stat
import struct
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, TypeAlias, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from yinshi.root_launcher import (
    BROKER_UID,
    ROOT_LAUNCHER_PROTOCOL_VERSION,
    LaunchDisabledError,
    LaunchObjectError,
    PrepareObjectError,
    ReclaimObjectError,
    RootLauncher,
    RootLauncherProtocolError,
    RootLauncherRequest,
    parse_launcher_request,
)
from yinshi.services.broker_artifact_store import BrokerArtifactStore
from yinshi.services.broker_journal import (
    BrokerJournal,
    JournalSyncError,
    inspect_legacy_v1_journal,
)
from yinshi.services.broker_protocol import (
    BROKER_FRAME_BYTES_MAX,
    BROKER_RESPONSE_BYTES_MAX,
    BrokerProtocolError,
    canonical_json,
)
from yinshi.services.broker_replica_artifact_effects import (
    BrokerReplicaArtifactEffects,
    broker_artifact_limits_for_replica,
)
from yinshi.services.broker_replica_journal import (
    AdmissionReceipt,
    BrokerReplicaJournal,
    DrainReceipt,
    ExportReceipt,
    IngestReceipt,
    PublishReceipt,
    ReclaimReceipt,
    VerifyReceipt,
)
from yinshi.services.broker_replica_lifecycle import (
    REPLICA_LIFECYCLE_STAGES,
    BrokerReplicaLifecycleCoordinator,
    ReplicaLifecycleContext,
    ReplicaLifecycleEffects,
    ReplicaStageEffect,
    StageOutcomeUnknown,
    StageReconciliation,
    StageRejected,
)
from yinshi.services.broker_runtime import BrokerRuntime, BrokerRuntimeLayout
from yinshi.services.execution_broker import (
    BrokerControlService,
    BrokerService,
    LauncherResult,
    LauncherTimeoutError,
    LauncherTransportError,
    LauncherVerificationError,
)
from yinshi.services.replica_artifact_contract import compute_replica_limits_sha256
from yinshi.services.workspace_replica_publication import (
    DEFAULT_REPLICA_STORE_LIMITS,
    WorkspaceReplicaPublicationStore,
)

LOGGER = logging.getLogger("yinshi.execution_daemons")
BROKER_CONFIG_PATH = Path("/etc/yinshi/broker.json")
BROKER_APPLICATION_ID = "yinshi-desktop"
LAUNCHER_FRAME_BYTES_MAX = 4_096
LENGTH_PREFIX_BYTES = 4
RAW_KEY_BYTES = 32
MAX_CONNECTIONS = 8
CONNECTION_TIMEOUT_SECONDS = 120.0
BROKER_REPLICA_LIFECYCLE_TIMEOUT_SECONDS = 360.0
BROKER_TRANSPORT_TIMEOUT_SECONDS = 375.0
READ_TIMEOUT_SECONDS = 10.0
ROOT_WORKER_TIMEOUT_SECONDS = 30.0
LAUNCHER_TIMEOUT_SECONDS = 35.0
BROKER_EFFECT_TIMEOUT_SECONDS = 40.0
SHUTDOWN_GRACE_SECONDS = 5.0
ROOT_WORKER_FRAME_BYTES_MAX = 4_096 + LAUNCHER_FRAME_BYTES_MAX * 2
_WORKER_LENGTH_PREFIX_BYTES = 4
BROKER_PRIVATE_KEY_PATH = Path("/var/lib/yinshi/control/broker.key")
APPLICATION_PUBLIC_KEY_PATH = Path("/var/lib/yinshi/control/application.pub")
LEGACY_BROKER_JOURNAL_PATH = Path("/var/lib/yinshi/control/broker-journal.sqlite3")
BROKER_JOURNAL_PATH = Path("/var/lib/yinshi/control/broker-journal-v2.sqlite3")
BROKER_REPLICA_JOURNAL_PATH = Path("/var/lib/yinshi/control/broker-replica-journal.sqlite3")
BROKER_ARTIFACT_INCOMING_ROOT = Path("/var/lib/yinshi/artifact-incoming")
BROKER_PUBLICATION_STAGING_ROOT = Path("/var/lib/yinshi-launcher/staging")
BROKER_RUNTIME_ROOT = Path("/run/yinshi-launcher-state/workloads")
LAUNCHER_SOCKET_PATH = Path("/run/yinshi-launcher/control.sock")
LAUNCH_EXECUTION_ENABLED_ENVIRONMENT = "YINSHI_LAUNCH_EXECUTION_ENABLED"
REPLICA_PREPARE_ENABLED_ENVIRONMENT = "YINSHI_REPLICA_PREPARE_ENABLED"
REPLICA_RECLAIM_ENABLED_ENVIRONMENT = "YINSHI_REPLICA_RECLAIM_ENABLED"
REPLICA_LIFECYCLE_ENABLED_ENVIRONMENT = "YINSHI_REPLICA_LIFECYCLE_ENABLED"
REPLICA_STAGE_ENABLED_ENVIRONMENTS = MappingProxyType(
    {stage: f"YINSHI_REPLICA_STAGE_{stage.upper()}_ENABLED" for stage in REPLICA_LIFECYCLE_STAGES}
)

PeerUidResolver: TypeAlias = Callable[[socket.socket], int]
ConnectionHandler: TypeAlias = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class RootActionExecutor(Protocol):
    """Apply one validated launcher request and return its canonical reply."""

    async def apply(self, frame: bytes, *, peer_uid: int) -> bytes: ...


class BrokerFrameService(Protocol):
    """Serve broker frames for one authorized application peer."""

    @property
    def application_uid(self) -> int: ...

    async def handle(self, frame: bytes, *, peer_uid: int) -> bytes: ...


class DaemonError(RuntimeError):
    """Carry one stable daemon error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class BrokerDaemonConfig:
    application_uid: int
    application_public_key_path: Path
    broker_private_key_path: Path
    journal_path: Path
    broker_incarnation: str
    database_incarnation: str
    launcher_socket_path: Path


@dataclass(slots=True)
class DaemonServer:
    server: asyncio.Server
    connections: set[asyncio.Task[None]]

    def close(self) -> None:
        self.server.close()

    async def wait_closed(self) -> None:
        self.server.close()
        await self.server.wait_closed()
        if not self.connections:
            return
        _done, pending = await asyncio.wait(
            self.connections,
            timeout=SHUTDOWN_GRACE_SECONDS,
        )
        for task in pending:
            task.cancel()
        if pending:
            _done, pending = await asyncio.wait(
                pending,
                timeout=SHUTDOWN_GRACE_SECONDS,
            )
        if pending:
            LOGGER.error("shutdown_timeout")


def linux_peer_uid(sock: socket.socket) -> int:
    """Read Linux kernel peer credentials from an AF_UNIX socket."""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise DaemonError("peer_unavailable")
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, option, 12)
    except OSError as exc:
        raise DaemonError("peer_unavailable") from exc
    if len(raw) != 12:
        raise DaemonError("peer_unavailable")
    _pid, uid, _gid = struct.unpack("3i", raw)
    if uid < 0:
        raise DaemonError("peer_unavailable")
    return int(uid)


def _read_protected_file(
    path: Path,
    *,
    owner_uid: int,
    mode: int,
    maximum: int,
) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise DaemonError("config_invalid") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size > maximum
        ):
            raise DaemonError("config_invalid")
        payload = os.read(descriptor, maximum + 1)
        if len(payload) != metadata.st_size:
            raise DaemonError("config_invalid")
        return payload
    finally:
        os.close(descriptor)


def load_ed25519_private_key(
    path: Path,
    *,
    owner_uid: int | None = None,
) -> Ed25519PrivateKey:
    expected_uid = os.geteuid() if owner_uid is None else owner_uid
    raw = _read_protected_file(
        path,
        owner_uid=expected_uid,
        mode=0o600,
        maximum=RAW_KEY_BYTES,
    )
    if len(raw) != RAW_KEY_BYTES:
        raise DaemonError("config_invalid")
    try:
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError as exc:
        raise DaemonError("config_invalid") from exc


def load_ed25519_public_key(
    path: Path,
    *,
    owner_uid: int | None = None,
) -> Ed25519PublicKey:
    expected_uid = os.geteuid() if owner_uid is None else owner_uid
    raw = _read_protected_file(
        path,
        owner_uid=expected_uid,
        mode=0o600,
        maximum=RAW_KEY_BYTES,
    )
    if len(raw) != RAW_KEY_BYTES:
        raise DaemonError("config_invalid")
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise DaemonError("config_invalid") from exc


def resolve_boolean_gate(environment: str, *, explicit: bool) -> bool:
    """Require one fixed environment gate plus an explicit flag."""
    value = os.environ.get(environment, "false")
    if value not in {"true", "false"}:
        raise DaemonError("config_invalid")
    return explicit and value == "true"


def resolve_execution_gate(*, explicit: bool) -> bool:
    return resolve_boolean_gate(LAUNCH_EXECUTION_ENABLED_ENVIRONMENT, explicit=explicit)


def resolve_prepare_gate(*, explicit: bool) -> bool:
    return resolve_boolean_gate(REPLICA_PREPARE_ENABLED_ENVIRONMENT, explicit=explicit)


def resolve_reclaim_gate(*, explicit: bool) -> bool:
    return resolve_boolean_gate(REPLICA_RECLAIM_ENABLED_ENVIRONMENT, explicit=explicit)


def resolve_replica_lifecycle_gate(*, explicit: bool) -> bool:
    return resolve_boolean_gate(REPLICA_LIFECYCLE_ENABLED_ENVIRONMENT, explicit=explicit)


def resolve_replica_stage_gate(stage: str, *, explicit: bool) -> bool:
    environment = REPLICA_STAGE_ENABLED_ENVIRONMENTS.get(stage)
    if environment is None:
        raise DaemonError("config_invalid")
    return resolve_boolean_gate(environment, explicit=explicit)


def acquire_systemd_socket() -> socket.socket:
    """Adopt exactly one systemd listener from descriptor 3."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        raise DaemonError("socket_activation_invalid")
    if os.environ.get("LISTEN_FDS") != "1":
        raise DaemonError("socket_activation_invalid")
    try:
        listener = socket.socket(fileno=3)
    except OSError as exc:
        raise DaemonError("socket_activation_invalid") from exc
    if (
        listener.family != socket.AF_UNIX
        or listener.type != socket.SOCK_STREAM
        or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
    ):
        listener.close()
        raise DaemonError("socket_activation_invalid")
    os.environ.pop("LISTEN_PID", None)
    os.environ.pop("LISTEN_FDS", None)
    return listener


async def _read_frame(reader: asyncio.StreamReader, *, maximum: int) -> bytes:
    try:
        prefix = await reader.readexactly(LENGTH_PREFIX_BYTES)
        length = int.from_bytes(prefix, "big")
        if not 1 <= length <= maximum:
            raise DaemonError("frame_invalid")
        body = await reader.readexactly(length)
        if await reader.read(1):
            raise DaemonError("frame_invalid")
        return body
    except asyncio.IncompleteReadError as exc:
        raise DaemonError("frame_invalid") from exc


def _write_frame(writer: asyncio.StreamWriter, body: bytes) -> None:
    writer.write(len(body).to_bytes(LENGTH_PREFIX_BYTES, "big") + body)


def _connection_socket(writer: asyncio.StreamWriter) -> socket.socket:
    value = writer.get_extra_info("socket")
    if value is None or not hasattr(value, "getsockopt"):
        raise DaemonError("peer_unavailable")
    return cast(socket.socket, value)


def canonical_launcher_request(operation_id: str, *, action: str = "launch") -> bytes:
    if action not in {"prepare", "launch", "reclaim"}:
        raise DaemonError("frame_invalid")
    message = {
        "action": action,
        "operation_id": operation_id,
        "protocol_version": ROOT_LAUNCHER_PROTOCOL_VERSION,
    }
    body = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    if len(body) > LAUNCHER_FRAME_BYTES_MAX:
        raise DaemonError("frame_invalid")
    return body


def _launcher_response(
    operation_id: str,
    *,
    action: str,
    status: str,
    error: str | None,
) -> bytes:
    message = {
        "action": action,
        "operation_id": operation_id,
        "protocol_version": ROOT_LAUNCHER_PROTOCOL_VERSION,
        "status": status,
    }
    if error is not None:
        message["error"] = error
    return json.dumps(message, sort_keys=True, separators=(",", ":")).encode()


def _parse_launcher_response(
    operation_id: str, body: bytes, *, action: str = "launch"
) -> LauncherResult:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LauncherVerificationError("launcher response invalid") from exc
    if not isinstance(value, dict):
        raise LauncherVerificationError("launcher response invalid")
    if json.dumps(value, sort_keys=True, separators=(",", ":")).encode() != body:
        raise LauncherVerificationError("launcher response invalid")
    if value.get("protocol_version") != ROOT_LAUNCHER_PROTOCOL_VERSION:
        raise LauncherVerificationError("launcher response invalid")
    if value.get("operation_id") != operation_id or value.get("action") != action:
        raise LauncherVerificationError("launcher response invalid")
    if set(value) not in (
        {"action", "operation_id", "protocol_version", "status"},
        {"action", "error", "operation_id", "protocol_version", "status"},
    ):
        raise LauncherVerificationError("launcher response invalid")
    status_value = value.get("status")
    error_value = value.get("error")
    expected_status = {
        "prepare": "prepared",
        "launch": "launched",
        "reclaim": "reclaimed",
    }.get(action)
    if status_value == expected_status and error_value is None:
        return LauncherResult(status=str(status_value), error=None)
    if status_value == "rejected" and isinstance(error_value, str):
        return LauncherResult(status="rejected", error=error_value)
    raise LauncherVerificationError("launcher response invalid")


class LauncherSocketClient:
    """Contact the root-owned launcher through one bounded exchange."""

    def __init__(
        self,
        *,
        socket_path: Path,
        peer_uid_of: PeerUidResolver = linux_peer_uid,
    ) -> None:
        self._socket_path = socket_path
        self._peer_uid_of = peer_uid_of

    async def prepare(self, operation_id: str) -> LauncherResult:
        return await self._request(operation_id, action="prepare")

    async def launch(self, operation_id: str) -> LauncherResult:
        return await self._request(operation_id, action="launch")

    async def reclaim(self, operation_id: str) -> LauncherResult:
        return await self._request(operation_id, action="reclaim")

    async def _request(self, operation_id: str, *, action: str) -> LauncherResult:
        try:
            return await asyncio.wait_for(
                self._exchange(operation_id, action=action),
                timeout=LAUNCHER_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise LauncherTimeoutError("launcher outcome timed out") from exc
        except (DaemonError, OSError, EOFError, asyncio.IncompleteReadError) as exc:
            raise LauncherTransportError("launcher outcome unconfirmed") from exc

    async def _exchange(self, operation_id: str, *, action: str) -> LauncherResult:
        reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
        try:
            if self._peer_uid_of(_connection_socket(writer)) != 0:
                raise LauncherTransportError("launcher peer invalid")
            _write_frame(writer, canonical_launcher_request(operation_id, action=action))
            await writer.drain()
            writer.write_eof()
            try:
                body = await _read_frame(reader, maximum=LAUNCHER_FRAME_BYTES_MAX)
            except DaemonError as exc:
                if isinstance(exc.__cause__, asyncio.IncompleteReadError):
                    raise LauncherTransportError("launcher response incomplete") from exc
                raise LauncherVerificationError("launcher response framing invalid") from exc
            return _parse_launcher_response(operation_id, body, action=action)
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


async def _exchange_broker(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    service: BrokerFrameService,
    peer_uid_of: PeerUidResolver,
    expected_peer_uid: int,
) -> None:
    peer_uid = peer_uid_of(_connection_socket(writer))
    if peer_uid != expected_peer_uid:
        raise DaemonError("peer_rejected")
    body = await asyncio.wait_for(
        _read_frame(reader, maximum=BROKER_FRAME_BYTES_MAX),
        timeout=READ_TIMEOUT_SECONDS,
    )
    response = await service.handle(body, peer_uid=peer_uid)
    if len(response) > BROKER_RESPONSE_BYTES_MAX:
        raise DaemonError("response_invalid")
    _write_frame(writer, response)
    await writer.drain()


def broker_connection_handler(
    *,
    service: BrokerFrameService,
    expected_peer_uid: int | None = None,
    peer_uid_of: PeerUidResolver = linux_peer_uid,
) -> ConnectionHandler:
    authorized_uid = service.application_uid if expected_peer_uid is None else expected_peer_uid

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _exchange_broker(
            reader,
            writer,
            service=service,
            peer_uid_of=peer_uid_of,
            expected_peer_uid=authorized_uid,
        )

    return handle


def _parse_worker_envelope(body: bytes) -> tuple[bytes, int]:
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise DaemonError("frame_invalid") from exc
    if not isinstance(value, dict) or set(value) != {"frame", "peer_uid"}:
        raise DaemonError("frame_invalid")
    frame_hex = value["frame"]
    peer_uid = value["peer_uid"]
    if not isinstance(frame_hex, str) or type(peer_uid) is not int or peer_uid < 0:
        raise DaemonError("frame_invalid")
    try:
        frame = bytes.fromhex(frame_hex)
    except ValueError as exc:
        raise DaemonError("frame_invalid") from exc
    if not 1 <= len(frame) <= LAUNCHER_FRAME_BYTES_MAX:
        raise DaemonError("frame_invalid")
    return frame, peer_uid


def _write_blocking_frame(connection: socket.socket, body: bytes) -> bool:
    framed = len(body).to_bytes(_WORKER_LENGTH_PREFIX_BYTES, "big") + body
    try:
        connection.sendall(framed)
    except OSError:
        return False
    return True


def _root_worker_child(connection: socket.socket, launcher: RootLauncher) -> int:
    """Serve serialized framed root actions until the parent disconnects."""
    signal.set_wakeup_fd(-1)
    with suppress(OSError, ValueError):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        os.setsid()
    except OSError:
        return 1
    connection.setblocking(True)
    if not _write_blocking_frame(connection, b"ready"):
        return 1
    reader = connection.makefile("rb")
    try:
        while True:
            prefix = reader.read(_WORKER_LENGTH_PREFIX_BYTES)
            if not prefix:
                return 0
            if len(prefix) < _WORKER_LENGTH_PREFIX_BYTES:
                return 1
            length = int.from_bytes(prefix, "big")
            if not 1 <= length <= ROOT_WORKER_FRAME_BYTES_MAX:
                return 1
            envelope = reader.read(length)
            if len(envelope) != length:
                return 1
            try:
                frame, peer_uid = _parse_worker_envelope(envelope)
                request = parse_launcher_request(frame, peer_uid=peer_uid, broker_uid=BROKER_UID)
                reply = _launcher_reply(launcher, request, frame, peer_uid=peer_uid)
            except (DaemonError, RootLauncherProtocolError):
                return 1
            if not _write_blocking_frame(connection, reply):
                return 1
    finally:
        with suppress(OSError):
            reader.close()
        connection.close()


async def _read_worker_frame(reader: asyncio.StreamReader) -> bytes:
    prefix = await reader.readexactly(_WORKER_LENGTH_PREFIX_BYTES)
    length = int.from_bytes(prefix, "big")
    if not 1 <= length <= ROOT_WORKER_FRAME_BYTES_MAX:
        raise DaemonError("frame_invalid")
    return await reader.readexactly(length)


class ManagedRootWorker:
    """Run privileged root actions inside one bounded managed child process."""

    def __init__(
        self,
        launcher: RootLauncher,
        *,
        timeout_seconds: float = ROOT_WORKER_TIMEOUT_SECONDS,
    ) -> None:
        if (
            type(timeout_seconds) not in {int, float}
            or not 0 < timeout_seconds <= CONNECTION_TIMEOUT_SECONDS
        ):
            raise ValueError("root worker timeout seconds must be a bounded positive number")
        self._launcher = launcher
        self._timeout_seconds = float(timeout_seconds)
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._child_pid = 0
        self._disabled = False

    @property
    def child_pid(self) -> int:
        return self._child_pid

    @property
    def disabled(self) -> bool:
        return self._disabled

    async def start(self) -> None:
        """Fork one root-action child and connect the private channel."""
        if self._child_pid:
            raise DaemonError("worker_unavailable")
        parent_connection, child_connection = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        try:
            pid = os.fork()
        except OSError as exc:
            parent_connection.close()
            child_connection.close()
            raise DaemonError("worker_unavailable") from exc
        if pid == 0:
            status = 1
            try:
                parent_connection.close()
                status = _root_worker_child(child_connection, self._launcher)
            except BaseException:  # noqa: BLE001
                status = 1
            finally:
                os._exit(status)
        child_connection.close()
        self._child_pid = pid
        parent_connection.setblocking(False)
        try:
            self._reader, self._writer = await asyncio.open_connection(sock=parent_connection)
            ready = await asyncio.wait_for(
                _read_worker_frame(self._reader),
                timeout=READ_TIMEOUT_SECONDS,
            )
            if ready != b"ready":
                raise DaemonError("worker_unavailable")
        except (DaemonError, OSError, ValueError, EOFError, TimeoutError) as exc:
            self._disable("worker_startup")
            raise DaemonError("worker_unavailable") from exc

    def _terminate_child(self) -> None:
        if self._child_pid <= 0:
            return
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(self._child_pid, signal.SIGKILL)
        with suppress(ChildProcessError, OSError):
            os.waitpid(self._child_pid, 0)
        self._child_pid = 0

    def _disable(self, reason: str) -> None:
        LOGGER.warning("root_worker_%s", reason)
        self._disabled = True
        if self._writer is not None:
            self._writer.close()
        self._terminate_child()
        self._reader = None
        self._writer = None

    def _disabled_reply(self, frame: bytes, *, peer_uid: int) -> bytes:
        request = parse_launcher_request(frame, peer_uid=peer_uid, broker_uid=BROKER_UID)
        return _launcher_response(
            request.operation_id,
            action=request.action,
            status="rejected",
            error=f"{request.action}_disabled",
        )

    async def apply(self, frame: bytes, *, peer_uid: int) -> bytes:
        """Forward one validated request under a strict deadline."""
        if self._disabled or self._reader is None or self._writer is None:
            return self._disabled_reply(frame, peer_uid=peer_uid)
        async with self._lock:
            if self._disabled or self._reader is None or self._writer is None:
                return self._disabled_reply(frame, peer_uid=peer_uid)
            reader, writer = self._reader, self._writer
            envelope = json.dumps(
                {"frame": frame.hex(), "peer_uid": peer_uid},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            if len(envelope) > ROOT_WORKER_FRAME_BYTES_MAX:
                raise DaemonError("frame_invalid")
            try:
                _write_frame(writer, envelope)
                await writer.drain()
                return await asyncio.wait_for(
                    _read_worker_frame(reader),
                    timeout=self._timeout_seconds,
                )
            except asyncio.CancelledError:
                self._disable("worker_cancelled")
                raise
            except TimeoutError:
                self._disable("worker_timeout")
                raise DaemonError("worker_timeout") from None
            except DaemonError:
                self._disable("worker_protocol")
                raise
            except (EOFError, OSError, asyncio.IncompleteReadError):
                self._disable("worker_unavailable")
                raise DaemonError("worker_unavailable") from None

    async def stop(self) -> None:
        """Disconnect and reap the child."""
        self._disabled = True
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        self._terminate_child()


async def _apply_root_action(
    launcher: RootLauncher | RootActionExecutor,
    request: RootLauncherRequest,
    body: bytes,
    *,
    peer_uid: int,
) -> bytes:
    if isinstance(launcher, RootLauncher):
        return _launcher_reply(launcher, request, body, peer_uid=peer_uid)
    return await launcher.apply(body, peer_uid=peer_uid)


async def _exchange_launcher(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    launcher: RootLauncher | RootActionExecutor,
    peer_uid_of: PeerUidResolver,
    expected_peer_uid: int,
) -> None:
    peer_uid = peer_uid_of(_connection_socket(writer))
    if peer_uid != expected_peer_uid:
        raise DaemonError("peer_rejected")
    body = await asyncio.wait_for(
        _read_frame(reader, maximum=LAUNCHER_FRAME_BYTES_MAX),
        timeout=READ_TIMEOUT_SECONDS,
    )
    request = parse_launcher_request(body, peer_uid=peer_uid, broker_uid=BROKER_UID)
    reply = await _apply_root_action(launcher, request, body, peer_uid=peer_uid)
    if len(reply) > LAUNCHER_FRAME_BYTES_MAX:
        raise DaemonError("response_invalid")
    _write_frame(writer, reply)
    await writer.drain()


def _launcher_reply(
    launcher: RootLauncher,
    request: RootLauncherRequest,
    body: bytes,
    *,
    peer_uid: int,
) -> bytes:
    def reply(status: str, error: str | None) -> bytes:
        return _launcher_response(
            request.operation_id,
            action=request.action,
            status=status,
            error=error,
        )

    if request.action == "prepare":
        if not launcher.prepare_enabled:
            return reply("rejected", "prepare_disabled")
        try:
            launcher.prepare(body, peer_uid=peer_uid)
        except PrepareObjectError:
            return reply("rejected", "prepare_rejected")
        return reply("prepared", None)
    if request.action == "reclaim":
        if not launcher.reclaim_enabled:
            return reply("rejected", "reclaim_disabled")
        try:
            launcher.reclaim(body, peer_uid=peer_uid)
        except ReclaimObjectError:
            return reply("rejected", "reclaim_rejected")
        return reply("reclaimed", None)
    if not launcher.execution_enabled:
        return reply("rejected", "launch_disabled")
    try:
        launcher.execute(body, peer_uid=peer_uid)
    except (LaunchDisabledError, LaunchObjectError):
        return reply("rejected", "launch_rejected")
    return reply("launched", None)


def launcher_connection_handler(
    *,
    launcher: RootLauncher | RootActionExecutor,
    expected_peer_uid: int = BROKER_UID,
    peer_uid_of: PeerUidResolver = linux_peer_uid,
) -> ConnectionHandler:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _exchange_launcher(
            reader,
            writer,
            launcher=launcher,
            peer_uid_of=peer_uid_of,
            expected_peer_uid=expected_peer_uid,
        )

    return handle


async def _serve_connection(
    handler: ConnectionHandler,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    timeout_seconds: float,
) -> None:
    try:
        await asyncio.wait_for(
            handler(reader, writer),
            timeout=timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except (BrokerProtocolError, RootLauncherProtocolError):
        LOGGER.warning("protocol_invalid")
    except DaemonError:
        LOGGER.warning("daemon_rejected")
    except JournalSyncError:
        LOGGER.warning("journal_unavailable")
    except LauncherTransportError:
        LOGGER.warning("launcher_unreachable")
    except TimeoutError:
        LOGGER.warning("connection_timeout")
    except OSError:
        LOGGER.warning("connection_aborted")
    except Exception:  # noqa: BLE001
        LOGGER.error("internal_error")
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def serve(
    listener: socket.socket,
    *,
    handle: ConnectionHandler,
    connection_timeout_seconds: float = CONNECTION_TIMEOUT_SECONDS,
) -> DaemonServer:
    connections: set[asyncio.Task[None]] = set()

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(connections) >= MAX_CONNECTIONS:
            writer.close()
            return
        task = asyncio.create_task(
            _serve_connection(
                handle,
                reader,
                writer,
                timeout_seconds=connection_timeout_seconds,
            )
        )
        connections.add(task)
        task.add_done_callback(connections.discard)

    server = await asyncio.start_unix_server(connected, sock=listener)
    return DaemonServer(server=server, connections=connections)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = value
    return result


def load_broker_config(
    path: Path = BROKER_CONFIG_PATH,
    *,
    owner_uid: int = 0,
) -> BrokerDaemonConfig:
    raw = _read_protected_file(path, owner_uid=owner_uid, mode=0o640, maximum=4_096)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise DaemonError("config_invalid") from exc
    expected = {
        "application_uid",
        "application_public_key_path",
        "broker_private_key_path",
        "journal_path",
        "broker_incarnation",
        "database_incarnation",
        "launcher_socket_path",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise DaemonError("config_invalid")
    application_uid = value["application_uid"]
    broker_incarnation = value["broker_incarnation"]
    database_incarnation = value["database_incarnation"]
    path_values = (
        value["application_public_key_path"],
        value["broker_private_key_path"],
        value["journal_path"],
        value["launcher_socket_path"],
    )
    if type(application_uid) is not int or application_uid < 0:
        raise DaemonError("config_invalid")
    if (
        not isinstance(broker_incarnation, str)
        or _TOKEN_PATTERN.fullmatch(broker_incarnation) is None
        or not isinstance(database_incarnation, str)
        or _TOKEN_PATTERN.fullmatch(database_incarnation) is None
        or not all(isinstance(item, str) for item in path_values)
    ):
        raise DaemonError("config_invalid")
    paths = tuple(Path(cast(str, item)) for item in path_values)
    expected_paths = (
        APPLICATION_PUBLIC_KEY_PATH,
        BROKER_PRIVATE_KEY_PATH,
        BROKER_JOURNAL_PATH,
        LAUNCHER_SOCKET_PATH,
    )
    if paths != expected_paths or not all(item.is_absolute() for item in paths):
        raise DaemonError("config_invalid")
    return BrokerDaemonConfig(
        application_uid=application_uid,
        application_public_key_path=paths[0],
        broker_private_key_path=paths[1],
        journal_path=paths[2],
        broker_incarnation=broker_incarnation,
        database_incarnation=database_incarnation,
        launcher_socket_path=paths[3],
    )


_STAGE_REJECTION_STATUSES = MappingProxyType(
    {
        "ingest": "artifact_rejected",
        "verify": "verification_rejected",
        "publish": "publication_rejected",
        "admission": "admission_rejected",
        "drain": "drain_rejected",
        "export": "export_rejected",
        "reclaim": "reclaim_rejected",
    }
)
_STAGE_UNRESOLVED_REASONS = MappingProxyType(
    {
        "ingest": "transfer_unknown",
        "verify": "verification_unknown",
        "publish": "publication_unknown",
        "admission": "admission_unknown",
        "drain": "acknowledgment_unknown",
        "export": "transfer_unknown",
        "reclaim": "effect_unknown",
    }
)


def _gate_rejected_receipt_id(stage: str, operation_id: str) -> str:
    status = _STAGE_REJECTION_STATUSES[stage]
    digest = hashlib.sha256(
        b"yinshi-replica-stage-gate-rejection-v1\0"
        + canonical_json(
            {
                "code": status,
                "operation_id": operation_id,
                "stage": stage,
            }
        )
    ).hexdigest()
    return f"rejected_{digest}"


class _GatedStageEffect:
    """Keep one production stage disabled unless both gates authorize it."""

    def __init__(
        self,
        stage: str,
        delegate: ReplicaStageEffect[object],
        *,
        enabled: bool,
    ) -> None:
        self._stage = stage
        self._delegate = delegate
        self._enabled = enabled

    async def apply(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[object]:
        if not self._enabled:
            raise StageRejected(
                _STAGE_REJECTION_STATUSES[self._stage],
                _gate_rejected_receipt_id(self._stage, context.request.operation_id),
            )
        return await self._delegate.apply(context)

    async def reconcile(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[object]:
        if not self._enabled:
            raise StageOutcomeUnknown(_STAGE_UNRESOLVED_REASONS[self._stage])
        return await self._delegate.reconcile(context)


class _FailClosedStageEffect:
    """Reject an unavailable production effect without performing work."""

    def __init__(self, stage: str) -> None:
        self._stage = stage

    async def apply(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[object]:
        raise StageRejected(
            _STAGE_REJECTION_STATUSES[self._stage],
            _gate_rejected_receipt_id(self._stage, context.request.operation_id),
        )

    async def reconcile(
        self,
        context: ReplicaLifecycleContext,
    ) -> StageReconciliation[object]:
        del context
        raise StageOutcomeUnknown(_STAGE_UNRESOLVED_REASONS[self._stage])


@dataclass(frozen=True, slots=True)
class BrokerProductionComposition:
    """Hold the production broker services with explicit recovery owners."""

    launch_service: BrokerService
    control_service: BrokerControlService
    replica_coordinator: BrokerReplicaLifecycleCoordinator
    replica_journal: BrokerReplicaJournal
    artifact_effects: BrokerReplicaArtifactEffects
    incoming_store: BrokerArtifactStore
    publication_store: WorkspaceReplicaPublicationStore
    limits_sha256: str
    replica_lifecycle_enabled: bool
    stage_gates_enabled: Mapping[str, bool]
    stage_effects: Mapping[str, ReplicaStageEffect[object]]


def _build_broker(
    config: BrokerDaemonConfig,
    *,
    application_public_key: Ed25519PublicKey | None = None,
    broker_private_key: Ed25519PrivateKey | None = None,
) -> BrokerService:
    request_key = application_public_key or load_ed25519_public_key(
        config.application_public_key_path
    )
    response_key = broker_private_key or load_ed25519_private_key(config.broker_private_key_path)
    return BrokerService(
        broker_incarnation=config.broker_incarnation,
        database_incarnation=config.database_incarnation,
        application_uid=config.application_uid,
        application_public_key=request_key,
        broker_private_key=response_key,
        journal=BrokerJournal(config.journal_path, application_id=BROKER_APPLICATION_ID),
        launcher=LauncherSocketClient(socket_path=config.launcher_socket_path),
        runtime=BrokerRuntime(layout=BrokerRuntimeLayout(runtime_root=BROKER_RUNTIME_ROOT)),
        launch_timeout_seconds=BROKER_EFFECT_TIMEOUT_SECONDS,
    )


def _build_broker_composition(
    config: BrokerDaemonConfig,
    *,
    replica_lifecycle_explicit: bool = False,
    stage_explicit: Mapping[str, bool] | None = None,
) -> BrokerProductionComposition:
    if type(replica_lifecycle_explicit) is not bool:
        raise DaemonError("config_invalid")
    requested_stages = {} if stage_explicit is None else dict(stage_explicit)
    if any(
        stage not in REPLICA_LIFECYCLE_STAGES or type(explicit) is not bool
        for stage, explicit in requested_stages.items()
    ):
        raise DaemonError("config_invalid")
    lifecycle_enabled = resolve_replica_lifecycle_gate(explicit=replica_lifecycle_explicit)
    stage_enabled = MappingProxyType(
        {
            stage: resolve_replica_stage_gate(
                stage,
                explicit=requested_stages.get(stage, False),
            )
            for stage in REPLICA_LIFECYCLE_STAGES
        }
    )

    application_public_key = load_ed25519_public_key(config.application_public_key_path)
    broker_private_key = load_ed25519_private_key(config.broker_private_key_path)
    launch_service = _build_broker(
        config,
        application_public_key=application_public_key,
        broker_private_key=broker_private_key,
    )
    limits = DEFAULT_REPLICA_STORE_LIMITS
    limits_sha256 = compute_replica_limits_sha256(asdict(limits))
    replica_journal = BrokerReplicaJournal(
        BROKER_REPLICA_JOURNAL_PATH,
        application_id=BROKER_APPLICATION_ID,
        expected_limits_sha256=limits_sha256,
        request_public_key=application_public_key,
        response_public_keys={
            config.broker_incarnation: broker_private_key.public_key(),
        },
    )
    incoming_store = BrokerArtifactStore(
        BROKER_ARTIFACT_INCOMING_ROOT,
        limits=broker_artifact_limits_for_replica(limits),
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    publication_store = WorkspaceReplicaPublicationStore(
        BROKER_PUBLICATION_STAGING_ROOT,
        ceilings=limits,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    artifact_effects = BrokerReplicaArtifactEffects(
        incoming=incoming_store,
        publication=publication_store,
        limits=limits,
    )
    stage_effects: Mapping[str, ReplicaStageEffect[object]] = MappingProxyType(
        {
            "ingest": _GatedStageEffect(
                "ingest",
                artifact_effects.ingest,
                enabled=stage_enabled["ingest"],
            ),
            "verify": _GatedStageEffect(
                "verify",
                artifact_effects.verify,
                enabled=stage_enabled["verify"],
            ),
            "publish": _GatedStageEffect(
                "publish",
                artifact_effects.publish,
                enabled=stage_enabled["publish"],
            ),
            "admission": _FailClosedStageEffect("admission"),
            "drain": _FailClosedStageEffect("drain"),
            "export": _FailClosedStageEffect("export"),
            "reclaim": _FailClosedStageEffect("reclaim"),
        }
    )
    lifecycle_effects = ReplicaLifecycleEffects(
        ingest=cast(ReplicaStageEffect[IngestReceipt], stage_effects["ingest"]),
        verify=cast(ReplicaStageEffect[VerifyReceipt], stage_effects["verify"]),
        publish=cast(ReplicaStageEffect[PublishReceipt], stage_effects["publish"]),
        admission=cast(ReplicaStageEffect[AdmissionReceipt], stage_effects["admission"]),
        drain=cast(ReplicaStageEffect[DrainReceipt], stage_effects["drain"]),
        export=cast(ReplicaStageEffect[ExportReceipt], stage_effects["export"]),
        reclaim=cast(ReplicaStageEffect[ReclaimReceipt], stage_effects["reclaim"]),
    )
    replica_coordinator = BrokerReplicaLifecycleCoordinator(
        replica_journal,
        broker_incarnation=config.broker_incarnation,
        broker_private_keys={config.broker_incarnation: broker_private_key},
        effects=lifecycle_effects,
        effect_timeout_seconds=BROKER_EFFECT_TIMEOUT_SECONDS,
    )
    control_service = BrokerControlService(
        broker_incarnation=config.broker_incarnation,
        database_incarnation=config.database_incarnation,
        application_uid=config.application_uid,
        application_public_key=application_public_key,
        broker_private_key=broker_private_key,
        launch_service=launch_service,
        replica_coordinator=replica_coordinator,
        replica_lifecycle_enabled=lifecycle_enabled,
        launch_request_timeout_seconds=CONNECTION_TIMEOUT_SECONDS,
        replica_lifecycle_timeout_seconds=BROKER_REPLICA_LIFECYCLE_TIMEOUT_SECONDS,
    )
    return BrokerProductionComposition(
        launch_service=launch_service,
        control_service=control_service,
        replica_coordinator=replica_coordinator,
        replica_journal=replica_journal,
        artifact_effects=artifact_effects,
        incoming_store=incoming_store,
        publication_store=publication_store,
        limits_sha256=limits_sha256,
        replica_lifecycle_enabled=lifecycle_enabled,
        stage_gates_enabled=stage_enabled,
        stage_effects=stage_effects,
    )


async def _run_daemon(
    handle: ConnectionHandler,
    *,
    connection_timeout_seconds: float = CONNECTION_TIMEOUT_SECONDS,
) -> int:
    listener = acquire_systemd_socket()
    daemon = await serve(
        listener,
        handle=handle,
        connection_timeout_seconds=connection_timeout_seconds,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(stop_signal, stop.set)
    try:
        await stop.wait()
    finally:
        await daemon.wait_closed()
    return 0


async def _run_broker(
    *,
    replica_lifecycle_explicit: bool = False,
    stage_explicit: Mapping[str, bool] | None = None,
) -> int:
    config = load_broker_config()
    inspect_legacy_v1_journal(
        LEGACY_BROKER_JOURNAL_PATH,
        application_id=BROKER_APPLICATION_ID,
    )
    composition = _build_broker_composition(
        config,
        replica_lifecycle_explicit=replica_lifecycle_explicit,
        stage_explicit=stage_explicit,
    )
    composition.launch_service.recover_startup()
    await composition.replica_coordinator.recover_incomplete()
    return await _run_daemon(
        broker_connection_handler(
            service=composition.control_service,
            expected_peer_uid=config.application_uid,
        ),
        connection_timeout_seconds=BROKER_TRANSPORT_TIMEOUT_SECONDS,
    )


async def _run_launcher(*, explicit: bool, prepare_explicit: bool, reclaim_explicit: bool) -> int:
    worker = ManagedRootWorker(
        RootLauncher(
            execution_enabled=resolve_execution_gate(explicit=explicit),
            prepare_enabled=resolve_prepare_gate(explicit=prepare_explicit),
            reclaim_enabled=resolve_reclaim_gate(explicit=reclaim_explicit),
        )
    )
    await worker.start()
    try:
        return await _run_daemon(
            launcher_connection_handler(launcher=worker),
        )
    finally:
        await worker.stop()


def broker_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yinshi-broker")
    parser.add_argument("--systemd-socket", action="store_true", required=True)
    parser.add_argument("--replica-lifecycle-enabled", action="store_true")
    for stage in REPLICA_LIFECYCLE_STAGES:
        parser.add_argument(f"--replica-{stage}-enabled", action="store_true")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(
            _run_broker(
                replica_lifecycle_explicit=bool(args.replica_lifecycle_enabled),
                stage_explicit={
                    stage: bool(getattr(args, f"replica_{stage}_enabled"))
                    for stage in REPLICA_LIFECYCLE_STAGES
                },
            )
        )
    except (DaemonError, JournalSyncError):
        LOGGER.error("startup_rejected")
        return 1
    except Exception:  # noqa: BLE001
        LOGGER.error("startup_rejected")
        return 1


def launcher_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yinshi-root-launcher")
    parser.add_argument("--systemd-socket", action="store_true", required=True)
    parser.add_argument("--launch-execution-enabled", action="store_true")
    parser.add_argument("--replica-prepare-enabled", action="store_true")
    parser.add_argument("--replica-reclaim-enabled", action="store_true")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(
            _run_launcher(
                explicit=bool(args.launch_execution_enabled),
                prepare_explicit=bool(args.replica_prepare_enabled),
                reclaim_explicit=bool(args.replica_reclaim_enabled),
            )
        )
    except (DaemonError, JournalSyncError):
        LOGGER.error("startup_rejected")
        return 1
    except Exception:  # noqa: BLE001
        LOGGER.error("startup_rejected")
        return 1
