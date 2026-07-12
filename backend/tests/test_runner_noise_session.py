"""Verify capability-bound Noise sessions and durable replay rejection.

Tests drive both protocol roles in memory, then reopen the replay database to
prove one-time grants remain consumed across runner process restarts.
"""

from __future__ import annotations

import base64
import json
import sqlite3
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


def _private_key(hex_value: str) -> bytes:
    return bytes.fromhex(hex_value)


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


def _initiator(
    *,
    static_private_key: bytes,
    runner_public_key: bytes,
) -> NoiseConnection:
    connection = NoiseConnection.from_name(b"Noise_IK_25519_ChaChaPoly_SHA256")
    connection.set_as_initiator()
    connection.set_keypair_from_private_bytes(Keypair.STATIC, static_private_key)
    connection.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, runner_public_key)
    connection.set_prologue(RUNNER_NOISE_PROLOGUE)
    connection.start_handshake()
    return connection


def _capability(
    *,
    client_public_key: bytes,
    runner_public_key: bytes,
    current_time: int = 1_900_000_000,
) -> tuple[str, int]:
    token, claims = create_runner_capability(
        user_id="user-1",
        runner_id="runner-1",
        runner_public_key=_base64url(runner_public_key),
        initiator_public_key=_base64url(client_public_key),
        scopes=["worker.health"],
        max_session_bytes=65_536,
        current_time=current_time,
    )
    return token, claims.expires_at


def _session(
    *,
    runner_private_key: bytes,
    replay_path: Path,
) -> RunnerNoiseSession:
    signing_public_key = base64.urlsafe_b64decode(runner_capability_signing_public_key() + "=")
    return RunnerNoiseSession(
        runner_id="runner-1",
        runner_static_private_key=runner_private_key,
        capability_signing_public_key=signing_public_key,
        replay_store=RunnerCapabilityReplayStore(replay_path),
    )


def test_noise_session_binds_capability_identity_and_transport(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Authenticated capability payload opens one mutually identified channel."""
    runner_private_key = _private_key(
        "4a3acbfdb163dec651dfa3194dece676d437029c62a408b4c5ea9114246e4893"
    )
    client_private_key = _private_key(
        "e61ef9919cde45dd5f82166404bd08e38bceb5dfdfded0a34c8df7ed542214d1"
    )
    runner_public_key = _public_key(runner_private_key)
    client_public_key = _public_key(client_private_key)
    capability, _ = _capability(
        client_public_key=client_public_key,
        runner_public_key=runner_public_key,
    )
    initiator = _initiator(
        static_private_key=client_private_key,
        runner_public_key=runner_public_key,
    )
    first_message = bytes(initiator.write_message(capability.encode("ascii")))
    session = _session(
        runner_private_key=runner_private_key,
        replay_path=tmp_path / "replay.sqlite3",
    )

    response = session.accept_handshake(first_message, current_time=1_900_000_001)
    response_payload = initiator.read_message(response)
    assert json.loads(response_payload) == {
        "protocol": "yinshi-runner-v1",
        "transfer_id": session.capability.transfer_id,
    }
    assert session.capability.initiator_public_key == _base64url(client_public_key)
    assert session.capability.scopes == ("worker.health",)

    encrypted_request = initiator.encrypt(b'{"kind":"health"}')
    assert session.decrypt(encrypted_request) == b'{"kind":"health"}'
    encrypted_response = session.encrypt(b'{"status":"ok"}')
    assert initiator.decrypt(encrypted_response) == b'{"status":"ok"}'
    with pytest.raises(ValueError, match="authentication"):
        session.decrypt(encrypted_request)


def test_noise_session_rejects_wrong_identity_and_durable_replay(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Capability cannot move to another key or be reused after process restart."""
    runner_private_key = _private_key(
        "4a3acbfdb163dec651dfa3194dece676d437029c62a408b4c5ea9114246e4893"
    )
    authorized_private_key = _private_key(
        "e61ef9919cde45dd5f82166404bd08e38bceb5dfdfded0a34c8df7ed542214d1"
    )
    wrong_private_key = _private_key(
        "893e28b9dc6ca8d611ab664754b8ceb7bac5117349a4439a6b0569da977c464a"
    )
    runner_public_key = _public_key(runner_private_key)
    capability, _ = _capability(
        client_public_key=_public_key(authorized_private_key),
        runner_public_key=runner_public_key,
    )
    replay_path = tmp_path / "replay.sqlite3"

    wrong_initiator = _initiator(
        static_private_key=wrong_private_key,
        runner_public_key=runner_public_key,
    )
    wrong_message = bytes(wrong_initiator.write_message(capability.encode("ascii")))
    tampered_message = bytearray(wrong_message)
    tampered_message[-1] ^= 1
    with pytest.raises(ValueError, match="authentication"):
        _session(
            runner_private_key=runner_private_key,
            replay_path=replay_path,
        ).accept_handshake(bytes(tampered_message), current_time=1_900_000_001)

    with pytest.raises(ValueError, match="initiator identity"):
        _session(
            runner_private_key=runner_private_key,
            replay_path=replay_path,
        ).accept_handshake(wrong_message, current_time=1_900_000_001)

    authorized_initiator = _initiator(
        static_private_key=authorized_private_key,
        runner_public_key=runner_public_key,
    )
    first_message = bytes(authorized_initiator.write_message(capability.encode("ascii")))
    first_session = _session(
        runner_private_key=runner_private_key,
        replay_path=replay_path,
    )
    first_session.accept_handshake(first_message, current_time=1_900_000_001)

    replay_initiator = _initiator(
        static_private_key=authorized_private_key,
        runner_public_key=runner_public_key,
    )
    replay_message = bytes(replay_initiator.write_message(capability.encode("ascii")))
    restarted_session = _session(
        runner_private_key=runner_private_key,
        replay_path=replay_path,
    )
    with pytest.raises(ValueError, match="already been consumed"):
        restarted_session.accept_handshake(replay_message, current_time=1_900_000_002)
