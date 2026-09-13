"""Separate systemd-activated daemon for authenticated broker artifact uploads."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import socket
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeAlias, cast

from yinshi.execution_daemons import (
    APPLICATION_PUBLIC_KEY_PATH,
    BROKER_PRIVATE_KEY_PATH,
    DaemonError,
    DaemonServer,
    acquire_systemd_socket,
    linux_peer_uid,
    load_ed25519_private_key,
    load_ed25519_public_key,
    resolve_boolean_gate,
)
from yinshi.services.broker_artifact_store import BrokerArtifactLimits, BrokerArtifactStore
from yinshi.services.broker_artifact_transport import BrokerArtifactUploadService
from yinshi.services.broker_protocol import (
    BROKER_FRAME_BYTES_MAX,
    BROKER_RESPONSE_BYTES_MAX,
    BrokerProtocolError,
)
from yinshi.services.replica_artifact_contract import compute_replica_limits_sha256
from yinshi.services.workspace_replica_publication import DEFAULT_REPLICA_STORE_LIMITS

LOGGER = logging.getLogger("yinshi.artifact_upload_daemon")
ARTIFACT_UPLOAD_CONFIG_PATH = Path("/etc/yinshi/artifact-upload.json")
ARTIFACT_UPLOAD_ROOT = Path("/var/lib/yinshi/artifact-incoming")
REPLICA_UPLOAD_ENABLED_ENVIRONMENT = "YINSHI_REPLICA_UPLOAD_ENABLED"
ARTIFACT_CONNECTION_TIMEOUT_SECONDS = 135.0
ARTIFACT_CONTROL_TIMEOUT_SECONDS = 10.0
ARTIFACT_MAX_CONNECTIONS = 4
SHUTDOWN_GRACE_SECONDS = 5.0
LENGTH_PREFIX_BYTES = 4
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")

PeerUidResolver: TypeAlias = Callable[[socket.socket], int]
UploadHandler: TypeAlias = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ArtifactUploadDaemonConfig:
    application_uid: int
    application_public_key_path: Path
    broker_private_key_path: Path
    broker_incarnation: str
    database_incarnation: str
    artifact_root: Path
    limits_sha256: str


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = value
    return result


def _read_config(path: Path, *, owner_uid: int) -> bytes:
    from yinshi.execution_daemons import _read_protected_file

    return _read_protected_file(path, owner_uid=owner_uid, mode=0o640, maximum=4_096)


def load_artifact_upload_config(
    path: Path = ARTIFACT_UPLOAD_CONFIG_PATH,
    *,
    owner_uid: int = 0,
) -> ArtifactUploadDaemonConfig:
    """Load one exact root-owned configuration for the upload daemon only."""
    raw = _read_config(path, owner_uid=owner_uid)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as error:
        raise DaemonError("config_invalid") from error
    required = {
        "application_uid",
        "application_public_key_path",
        "artifact_root",
        "broker_incarnation",
        "broker_private_key_path",
        "database_incarnation",
        "limits_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise DaemonError("config_invalid")
    application_uid = value["application_uid"]
    broker_incarnation = value["broker_incarnation"]
    database_incarnation = value["database_incarnation"]
    limits_sha256 = value["limits_sha256"]
    paths_raw = (
        value["application_public_key_path"],
        value["broker_private_key_path"],
        value["artifact_root"],
    )
    if type(application_uid) is not int or application_uid < 0:
        raise DaemonError("config_invalid")
    if (
        type(broker_incarnation) is not str
        or _TOKEN_PATTERN.fullmatch(broker_incarnation) is None
        or type(database_incarnation) is not str
        or _TOKEN_PATTERN.fullmatch(database_incarnation) is None
        or type(limits_sha256) is not str
        or len(limits_sha256) != 64
        or any(character not in "0123456789abcdef" for character in limits_sha256)
        or not all(type(item) is str for item in paths_raw)
    ):
        raise DaemonError("config_invalid")
    paths = tuple(Path(cast(str, item)) for item in paths_raw)
    if paths != (APPLICATION_PUBLIC_KEY_PATH, BROKER_PRIVATE_KEY_PATH, ARTIFACT_UPLOAD_ROOT):
        raise DaemonError("config_invalid")
    expected_limits_sha256 = compute_replica_limits_sha256(asdict(DEFAULT_REPLICA_STORE_LIMITS))
    if limits_sha256 != expected_limits_sha256:
        raise DaemonError("config_invalid")
    return ArtifactUploadDaemonConfig(
        application_uid=application_uid,
        application_public_key_path=paths[0],
        broker_private_key_path=paths[1],
        broker_incarnation=broker_incarnation,
        database_incarnation=database_incarnation,
        artifact_root=paths[2],
        limits_sha256=limits_sha256,
    )


def resolve_upload_gate(*, explicit: bool) -> bool:
    """Require both explicit startup intent and the fixed environment gate."""
    return resolve_boolean_gate(REPLICA_UPLOAD_ENABLED_ENVIRONMENT, explicit=explicit)


def _artifact_limits() -> BrokerArtifactLimits:
    configured = DEFAULT_REPLICA_STORE_LIMITS
    return BrokerArtifactLimits(
        max_bundle_bytes=configured.bundle.max_bundle_bytes,
        max_worktree_bytes=configured.worktree.max_total_bytes,
        max_index_objects_bytes=configured.index_objects.max_pack_bytes,
        max_set_bytes=configured.max_set_bytes,
    )


def build_artifact_upload_service(
    config: ArtifactUploadDaemonConfig,
    *,
    enabled: bool,
) -> BrokerArtifactUploadService:
    """Build the isolated upload service from one immutable configured profile."""
    if type(config) is not ArtifactUploadDaemonConfig:
        raise TypeError("artifact upload daemon configuration is invalid")
    return BrokerArtifactUploadService(
        broker_incarnation=config.broker_incarnation,
        database_incarnation=config.database_incarnation,
        application_uid=config.application_uid,
        application_public_key=load_ed25519_public_key(config.application_public_key_path),
        broker_private_key=load_ed25519_private_key(config.broker_private_key_path),
        artifact_store=BrokerArtifactStore(
            config.artifact_root,
            limits=_artifact_limits(),
            expected_uid=os.geteuid(),
            expected_gid=os.getegid(),
        ),
        expected_limits_sha256=config.limits_sha256,
        enabled=enabled,
    )


def _connection_socket(writer: asyncio.StreamWriter) -> socket.socket:
    connection = writer.get_extra_info("socket")
    if connection is None:
        raise DaemonError("peer_unavailable")
    return cast(socket.socket, connection)


async def _read_control_frame(reader: asyncio.StreamReader) -> bytes:
    try:
        prefix = await reader.readexactly(LENGTH_PREFIX_BYTES)
    except asyncio.IncompleteReadError as error:
        raise DaemonError("frame_invalid") from error
    length = int.from_bytes(prefix, "big")
    if not 1 <= length <= BROKER_FRAME_BYTES_MAX:
        raise DaemonError("frame_invalid")
    try:
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError as error:
        raise DaemonError("frame_invalid") from error


def _write_frame(writer: asyncio.StreamWriter, body: bytes) -> None:
    if not 1 <= len(body) <= BROKER_RESPONSE_BYTES_MAX:
        raise DaemonError("response_invalid")
    writer.write(len(body).to_bytes(LENGTH_PREFIX_BYTES, "big") + body)


def artifact_upload_connection_handler(
    *,
    service: BrokerArtifactUploadService,
    expected_peer_uid: int,
    peer_uid_of: PeerUidResolver = linux_peer_uid,
) -> UploadHandler:
    """Authenticate the kernel peer before reading the signed control frame."""
    if type(expected_peer_uid) is not int or expected_peer_uid < 0:
        raise ValueError("artifact upload expected peer UID is invalid")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer_uid = peer_uid_of(_connection_socket(writer))
        if peer_uid != expected_peer_uid:
            raise DaemonError("peer_rejected")
        frame = await asyncio.wait_for(
            _read_control_frame(reader),
            timeout=ARTIFACT_CONTROL_TIMEOUT_SECONDS,
        )
        response = await service.handle(frame, reader, peer_uid=peer_uid)
        _write_frame(writer, response)
        await writer.drain()

    return handle


async def _serve_connection(
    handler: UploadHandler,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        await asyncio.wait_for(
            handler(reader, writer),
            timeout=ARTIFACT_CONNECTION_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        LOGGER.warning("connection_timeout")
    except (BrokerProtocolError, DaemonError):
        LOGGER.warning("upload_rejected")
    except OSError:
        LOGGER.warning("connection_aborted")
    except Exception:  # noqa: BLE001
        LOGGER.error("internal_error")
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def serve_artifact_upload(
    listener: socket.socket,
    *,
    handle: UploadHandler,
) -> DaemonServer:
    """Serve bounded upload connections independently from broker control traffic."""
    connections: set[asyncio.Task[None]] = set()

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(connections) >= ARTIFACT_MAX_CONNECTIONS:
            writer.close()
            return
        task = asyncio.create_task(_serve_connection(handle, reader, writer))
        connections.add(task)
        task.add_done_callback(connections.discard)

    server = await asyncio.start_unix_server(connected, sock=listener)
    return DaemonServer(server=server, connections=connections)


async def _run_upload(*, explicit: bool) -> int:
    config = load_artifact_upload_config()
    service = build_artifact_upload_service(
        config,
        enabled=resolve_upload_gate(explicit=explicit),
    )
    listener = acquire_systemd_socket()
    daemon = await serve_artifact_upload(
        listener,
        handle=artifact_upload_connection_handler(
            service=service,
            expected_peer_uid=config.application_uid,
        ),
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


def main(argv: Sequence[str] | None = None) -> int:
    """Start the separate upload daemon from one systemd listener."""
    parser = argparse.ArgumentParser(prog="yinshi-artifact-upload")
    parser.add_argument("--systemd-socket", action="store_true", required=True)
    parser.add_argument("--replica-upload-enabled", action="store_true")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run_upload(explicit=bool(args.replica_upload_enabled)))
    except (DaemonError, BrokerProtocolError):
        LOGGER.error("startup_rejected")
        return 1
    except Exception:  # noqa: BLE001
        LOGGER.error("startup_rejected")
        return 1
