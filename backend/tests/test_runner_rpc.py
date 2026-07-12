"""Verify the first restricted worker RPC slice over encrypted Noise frames.

A real IK handshake wraps canonical request frames. Tests decrypt responses on
the client and reject replayed application sequence numbers before dispatch.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from noise.connection import Keypair, NoiseConnection

from yinshi.services.runner_capabilities import (
    create_runner_capability,
    runner_capability_signing_public_key,
)
from yinshi.services.runner_noise_session import (
    RUNNER_NOISE_PROLOGUE,
    RunnerCapabilityReplayStore,
    RunnerNoiseSession,
)
from yinshi.services.runner_rpc import EncryptedRunnerRpcSession

_RUNNER_PRIVATE_KEY = bytes.fromhex(
    "4a3acbfdb163dec651dfa3194dece676d437029c62a408b4c5ea9114246e4893"
)
_CLIENT_PRIVATE_KEY = bytes.fromhex(
    "e61ef9919cde45dd5f82166404bd08e38bceb5dfdfded0a34c8df7ed542214d1"
)


def _public_key(private_key: bytes) -> bytes:
    return (
        X25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _open_session(tmp_path: Path) -> tuple[EncryptedRunnerRpcSession, NoiseConnection]:
    runner_public_key = _public_key(_RUNNER_PRIVATE_KEY)
    client_public_key = _public_key(_CLIENT_PRIVATE_KEY)
    capability, claims = create_runner_capability(
        user_id="user-1",
        runner_id="runner-1",
        runner_public_key=_base64url(runner_public_key),
        initiator_public_key=_base64url(client_public_key),
        scopes=["worker.health"],
        max_session_bytes=65_536,
        current_time=1_900_000_000,
    )
    signing_key = base64.urlsafe_b64decode(runner_capability_signing_public_key() + "=")
    noise_session = RunnerNoiseSession(
        runner_id="runner-1",
        runner_static_private_key=_RUNNER_PRIVATE_KEY,
        capability_signing_public_key=signing_key,
        replay_store=RunnerCapabilityReplayStore(tmp_path / "replay.sqlite3"),
    )
    rpc_session = EncryptedRunnerRpcSession(
        transfer_id=claims.transfer_id,
        noise_session=noise_session,
    )

    initiator = NoiseConnection.from_name(b"Noise_IK_25519_ChaChaPoly_SHA256")
    initiator.set_as_initiator()
    initiator.set_keypair_from_private_bytes(Keypair.STATIC, _CLIENT_PRIVATE_KEY)
    initiator.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, runner_public_key)
    initiator.set_prologue(RUNNER_NOISE_PROLOGUE)
    initiator.start_handshake()
    first_message = bytes(initiator.write_message(capability.encode("ascii")))
    response = rpc_session.handle_frame(first_message, current_time=1_900_000_001)
    assert json.loads(initiator.read_message(response))["transfer_id"] == claims.transfer_id
    return rpc_session, initiator


def test_encrypted_health_rpc_returns_allowlisted_metadata(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Health request crosses the authenticated channel without relay plaintext."""
    session, initiator = _open_session(tmp_path)
    request_id = str(uuid.uuid4())
    request = json.dumps(
        {
            "body": None,
            "method": "GET",
            "path": "/health",
            "request_id": request_id,
            "sequence": 0,
            "type": "request",
            "v": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    encrypted_response = session.handle_frame(
        bytes(initiator.encrypt(request)),
        current_time=1_900_000_002,
    )
    response = json.loads(initiator.decrypt(encrypted_response))

    assert response == {
        "body": {"protocol": "yinshi-runner-v1", "status": "ok"},
        "request_id": request_id,
        "sequence": 0,
        "status": 200,
        "type": "response",
        "v": 1,
    }


def test_encrypted_rpc_rejects_replayed_sequence(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Application ordering fails closed even when attacker sends valid ciphertext."""
    session, initiator = _open_session(tmp_path)
    request = json.dumps(
        {
            "body": None,
            "method": "GET",
            "path": "/health",
            "request_id": str(uuid.uuid4()),
            "sequence": 1,
            "type": "request",
            "v": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    with pytest.raises(ValueError, match="sequence"):
        session.handle_frame(
            bytes(initiator.encrypt(request)),
            current_time=1_900_000_002,
        )
