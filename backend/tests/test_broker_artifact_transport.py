"""Check authenticated bounded artifact uploads on their separate broker data plane."""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import uuid
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from yinshi.services.broker_artifact_store import BrokerArtifactLimits, BrokerArtifactStore
from yinshi.services.broker_protocol import (
    BROKER_PROTOCOL_VERSION,
    BrokerProtocolError,
    create_signed_request,
    parse_signed_request,
    verify_broker_response,
)
from yinshi.services.replica_artifact_contract import (
    compute_replica_artifact_set_sha256,
    compute_replica_limits_sha256,
)
from yinshi.services.workspace_replica_publication import DEFAULT_REPLICA_STORE_LIMITS

BROKER_INCARNATION = "b" * 32
DATABASE_INCARNATION = "d" * 32
OPERATION_ID = "a" * 32
APPLICATION_UID = 501
BUNDLE = b"bundle-content"
WORKTREE = b"worktree-content"
INDEX_OBJECTS = b"pack-content"
AUTHORITY = {
    "execution_owner_id": "execution_owner_0000000000000000",
    "physical_target_id": "target_00000000000000000000000000",
    "replica_generation": 3,
}
LIMITS_SHA256 = compute_replica_limits_sha256(asdict(DEFAULT_REPLICA_STORE_LIMITS))


def _reference(role: str, content: bytes) -> dict[str, object]:
    return {
        "artifact_id": f"{role}_000000000000000000000000",
        "byte_length": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _payload(**changes: object) -> dict[str, object]:
    bundle = _reference("bundle", BUNDLE)
    worktree = _reference("worktree", WORKTREE)
    index_objects = _reference("index", INDEX_OBJECTS)
    artifact_set_sha256 = compute_replica_artifact_set_sha256(
        operation_id=OPERATION_ID,
        repository_id="repository_0000000000000000000000",
        workspace_id="workspace_000000000000000000000000",
        physical_target_id=str(AUTHORITY["physical_target_id"]),
        replica_generation=int(AUTHORITY["replica_generation"]),
        execution_owner_id=str(AUTHORITY["execution_owner_id"]),
        object_format="sha256",
        source_state_sha256="4" * 64,
        reconciliation_fingerprint="3" * 64,
        bundle={
            "role": "committed_bundle",
            "media_type": "application/vnd.yinshi.git-bundle.v1",
            **bundle,
        },
        worktree={
            "role": "worktree",
            "media_type": "application/vnd.yinshi.replica-worktree.v1",
            **worktree,
        },
        index_objects={
            "role": "index_objects",
            "media_type": "application/vnd.yinshi.git-index-objects-pack.v1",
            **index_objects,
        },
        limits_sha256=LIMITS_SHA256,
    )
    value: dict[str, object] = {
        "artifact_set_sha256": artifact_set_sha256,
        "authority": dict(AUTHORITY),
        "bundle": bundle,
        "index_objects": index_objects,
        "limits_sha256": LIMITS_SHA256,
        "object_format": "sha256",
        "reconciliation_fingerprint": "3" * 64,
        "repository_id": "repository_0000000000000000000000",
        "source_state_sha256": "4" * 64,
        "workspace_id": "workspace_000000000000000000000000",
        "worktree": worktree,
    }
    value.update(changes)
    return value


def _request(key: Ed25519PrivateKey, **payload_changes: object) -> bytes:
    return create_signed_request(
        private_key=key,
        protocol_version=BROKER_PROTOCOL_VERSION,
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        connection_sequence=1,
        operation_id=OPERATION_ID,
        request_type="replica.artifacts.upload",
        nonce="nonce_00000000001",
        payload=_payload(**payload_changes),  # type: ignore[arg-type]
    )


def _reader(content: bytes, *, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(content)
    if eof:
        reader.feed_eof()
    return reader


def _service(
    tmp_path: Path,
    request_key: Ed25519PrivateKey,
    *,
    enabled: bool = True,
    limits: BrokerArtifactLimits | None = None,
):
    from yinshi.services.broker_artifact_transport import BrokerArtifactUploadService

    root = tmp_path / "incoming"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    response_key = Ed25519PrivateKey.generate()
    store = BrokerArtifactStore(
        root,
        limits=limits
        or BrokerArtifactLimits(
            max_worktree_bytes=DEFAULT_REPLICA_STORE_LIMITS.worktree.max_total_bytes
        ),
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    service = BrokerArtifactUploadService(
        broker_incarnation=BROKER_INCARNATION,
        database_incarnation=DATABASE_INCARNATION,
        application_uid=APPLICATION_UID,
        application_public_key=request_key.public_key(),
        broker_private_key=response_key,
        artifact_store=store,
        expected_limits_sha256=LIMITS_SHA256,
        enabled=enabled,
    )
    return service, response_key, root


@pytest.mark.asyncio
async def test_upload_authenticates_metadata_then_stores_exact_three_artifacts(
    tmp_path: Path,
) -> None:
    request_key = Ed25519PrivateKey.generate()
    service, response_key, root = _service(tmp_path, request_key)
    frame = _request(request_key)

    response = await service.handle(
        frame,
        _reader(BUNDLE + WORKTREE + INDEX_OBJECTS),
        peer_uid=APPLICATION_UID,
    )

    request = parse_signed_request(frame, public_key=request_key.public_key())
    verified = verify_broker_response(
        response,
        public_key=response_key.public_key(),
        expected_request=request,
    )
    assert verified.status == "ok"
    assert verified.result == {
        "artifact_set_sha256": _payload()["artifact_set_sha256"],
        "receipt_id": verified.result["receipt_id"],
        "stored": True,
    }
    final = root / OPERATION_ID
    assert {item.name for item in final.iterdir()} == {
        "committed.bundle",
        "index-objects.pack",
        "worktree.yra",
    }


@pytest.mark.asyncio
async def test_exact_retry_consumes_full_body_and_reuses_receipt(tmp_path: Path) -> None:
    request_key = Ed25519PrivateKey.generate()
    service, _response_key, _root = _service(tmp_path, request_key)
    frame = _request(request_key)
    content = BUNDLE + WORKTREE + INDEX_OBJECTS

    first = await service.handle(frame, _reader(content), peer_uid=APPLICATION_UID)
    second_reader = _reader(content)
    second = await service.handle(frame, second_reader, peer_uid=APPLICATION_UID)

    assert second == first
    assert second_reader.at_eof()


@pytest.mark.asyncio
async def test_peer_and_signature_fail_before_reading_artifact_bytes(tmp_path: Path) -> None:
    request_key = Ed25519PrivateKey.generate()
    service, _response_key, root = _service(tmp_path, request_key)
    reader = _reader(BUNDLE + WORKTREE + INDEX_OBJECTS, eof=False)

    with pytest.raises(BrokerProtocolError):
        await service.handle(_request(request_key), reader, peer_uid=APPLICATION_UID + 1)
    content = BUNDLE + WORKTREE + INDEX_OBJECTS
    assert await reader.read(len(content)) == content
    assert not any(root.iterdir())

    reader = _reader(content, eof=False)
    other_key = Ed25519PrivateKey.generate()
    with pytest.raises(BrokerProtocolError):
        await service.handle(_request(other_key), reader, peer_uid=APPLICATION_UID)
    assert await reader.read(len(content)) == content
    assert not any(root.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"limits_sha256": "9" * 64}, "upload_rejected"),
        ({"artifact_set_sha256": "8" * 64}, "upload_rejected"),
        ({"authority": {**AUTHORITY, "extra": "forbidden"}}, "upload_rejected"),
    ],
)
async def test_authenticated_invalid_metadata_returns_signed_rejection(
    tmp_path: Path,
    changes: dict[str, object],
    error: str,
) -> None:
    request_key = Ed25519PrivateKey.generate()
    service, response_key, root = _service(tmp_path, request_key)
    frame = _request(request_key, **changes)

    response = await service.handle(
        frame,
        _reader(BUNDLE + WORKTREE + INDEX_OBJECTS),
        peer_uid=APPLICATION_UID,
    )

    request = parse_signed_request(frame, public_key=request_key.public_key())
    verified = verify_broker_response(
        response,
        public_key=response_key.public_key(),
        expected_request=request,
    )
    assert verified.status == "error"
    assert verified.error == error
    assert verified.result["stored"] is False
    assert not any(root.iterdir())


@pytest.mark.asyncio
async def test_disabled_gate_consumes_body_without_storage_and_signs_result(tmp_path: Path) -> None:
    request_key = Ed25519PrivateKey.generate()
    service, response_key, root = _service(tmp_path, request_key, enabled=False)
    frame = _request(request_key)
    reader = _reader(BUNDLE + WORKTREE + INDEX_OBJECTS)

    response = await service.handle(frame, reader, peer_uid=APPLICATION_UID)

    verified = verify_broker_response(
        response,
        public_key=response_key.public_key(),
        expected_request=parse_signed_request(frame, public_key=request_key.public_key()),
    )
    assert verified.status == "error"
    assert verified.error == "upload_disabled"
    assert verified.result["stored"] is False
    assert reader.at_eof()
    assert not any(root.iterdir())


@pytest.mark.asyncio
async def test_truncated_transfer_returns_signed_rejection(tmp_path: Path) -> None:
    request_key = Ed25519PrivateKey.generate()
    service, response_key, root = _service(tmp_path, request_key)
    frame = _request(request_key)

    response = await service.handle(frame, _reader(BUNDLE[:-1]), peer_uid=APPLICATION_UID)

    verified = verify_broker_response(
        response,
        public_key=response_key.public_key(),
        expected_request=parse_signed_request(frame, public_key=request_key.public_key()),
    )
    assert verified.status == "error"
    assert verified.error == "upload_rejected"
    assert verified.result["stored"] is False
    assert not (root / OPERATION_ID).exists()


@pytest.mark.asyncio
async def test_transfer_timeout_returns_signed_unknown(tmp_path: Path) -> None:
    request_key = Ed25519PrivateKey.generate()
    service, response_key, root = _service(
        tmp_path,
        request_key,
        limits=BrokerArtifactLimits(
            max_worktree_bytes=DEFAULT_REPLICA_STORE_LIMITS.worktree.max_total_bytes,
            idle_timeout_seconds=0.01,
            transfer_timeout_seconds=0.02,
        ),
    )
    frame = _request(request_key)

    response = await service.handle(frame, _reader(b"", eof=False), peer_uid=APPLICATION_UID)

    verified = verify_broker_response(
        response,
        public_key=response_key.public_key(),
        expected_request=parse_signed_request(frame, public_key=request_key.public_key()),
    )
    assert verified.status == "error"
    assert verified.error == "upload_unknown"
    assert verified.result["stored"] is False
    assert not (root / OPERATION_ID).exists()


def test_upload_config_gate_and_deadlines_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from yinshi.artifact_upload_daemon import (
        ARTIFACT_CONNECTION_TIMEOUT_SECONDS,
        ARTIFACT_CONTROL_TIMEOUT_SECONDS,
        ARTIFACT_UPLOAD_ROOT,
        load_artifact_upload_config,
        resolve_upload_gate,
    )
    from yinshi.execution_daemons import APPLICATION_PUBLIC_KEY_PATH, BROKER_PRIVATE_KEY_PATH
    from yinshi.services.broker_artifact_store import BrokerArtifactLimits

    config_path = tmp_path / "artifact-upload.json"
    config_path.write_text(
        json.dumps(
            {
                "application_uid": APPLICATION_UID,
                "application_public_key_path": str(APPLICATION_PUBLIC_KEY_PATH),
                "artifact_root": str(ARTIFACT_UPLOAD_ROOT),
                "broker_incarnation": BROKER_INCARNATION,
                "broker_private_key_path": str(BROKER_PRIVATE_KEY_PATH),
                "database_incarnation": DATABASE_INCARNATION,
                "limits_sha256": LIMITS_SHA256,
            }
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o640)
    loaded = load_artifact_upload_config(config_path, owner_uid=os.geteuid())
    assert loaded.artifact_root == ARTIFACT_UPLOAD_ROOT
    assert ARTIFACT_CONNECTION_TIMEOUT_SECONDS > (
        ARTIFACT_CONTROL_TIMEOUT_SECONDS + BrokerArtifactLimits().transfer_timeout_seconds
    )

    monkeypatch.delenv("YINSHI_REPLICA_UPLOAD_ENABLED", raising=False)
    assert resolve_upload_gate(explicit=True) is False
    monkeypatch.setenv("YINSHI_REPLICA_UPLOAD_ENABLED", "true")
    assert resolve_upload_gate(explicit=False) is False
    assert resolve_upload_gate(explicit=True) is True


@pytest.mark.asyncio
async def test_separate_daemon_connection_streams_upload_after_control_frame(
    tmp_path: Path,
) -> None:
    from yinshi.artifact_upload_daemon import (
        artifact_upload_connection_handler,
        serve_artifact_upload,
    )

    request_key = Ed25519PrivateKey.generate()
    service, response_key, root = _service(tmp_path, request_key)
    frame = _request(request_key)
    socket_path = Path("/tmp") / f"yinshi-upload-{uuid.uuid4().hex}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(4)
    daemon = await serve_artifact_upload(
        listener,
        handle=artifact_upload_connection_handler(
            service=service,
            expected_peer_uid=APPLICATION_UID,
            peer_uid_of=lambda _socket: APPLICATION_UID,
        ),
    )
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(len(frame).to_bytes(4, "big") + frame + BUNDLE + WORKTREE + INDEX_OBJECTS)
        await writer.drain()
        writer.write_eof()
        prefix = await reader.readexactly(4)
        response = await reader.readexactly(int.from_bytes(prefix, "big"))
        assert await reader.read(1) == b""
    finally:
        daemon.close()
        await daemon.wait_closed()
        socket_path.unlink(missing_ok=True)
        with suppress(OSError):
            listener.close()

    verified = verify_broker_response(
        response,
        public_key=response_key.public_key(),
        expected_request=parse_signed_request(frame, public_key=request_key.public_key()),
    )
    assert verified.status == "ok"
    assert (root / OPERATION_ID / "committed.bundle").read_bytes() == BUNDLE


@pytest.mark.asyncio
async def test_separate_daemon_rejects_peer_before_control_read(tmp_path: Path) -> None:
    from yinshi.artifact_upload_daemon import (
        artifact_upload_connection_handler,
        serve_artifact_upload,
    )

    request_key = Ed25519PrivateKey.generate()
    service, _response_key, root = _service(tmp_path, request_key)
    socket_path = Path("/tmp") / f"yinshi-upload-{uuid.uuid4().hex}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(4)
    daemon = await serve_artifact_upload(
        listener,
        handle=artifact_upload_connection_handler(
            service=service,
            expected_peer_uid=APPLICATION_UID,
            peer_uid_of=lambda _socket: APPLICATION_UID + 1,
        ),
    )
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        assert await asyncio.wait_for(reader.read(), timeout=0.25) == b""
        assert not any(root.iterdir())
    finally:
        if writer is not None:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        daemon.close()
        await daemon.wait_closed()
        socket_path.unlink(missing_ok=True)
        with suppress(OSError):
            listener.close()
