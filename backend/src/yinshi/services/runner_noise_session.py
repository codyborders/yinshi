"""Capability-bound Noise sessions for the outbound BYOC runner channel."""

from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from yinshi.services.runner_capabilities import (
    RUNNER_PROTOCOL_VERSION,
    VerifiedRunnerCapability,
    verify_runner_capability,
)
from yinshi.services.runner_noise import NoiseIkResponder

RUNNER_NOISE_PROLOGUE = b"yinshi-runner-v1"
_MESSAGES_BEFORE_REHANDSHAKE = 1_048_576
_X25519_KEY_LENGTH = 32
_ED25519_KEY_LENGTH = 32


class RunnerCapabilityReplayStore:
    """Durably consume capability IDs without retaining request or response content."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib.Path")
        if not path.is_absolute():
            raise ValueError("Runner capability replay database path must be absolute")
        self._path = path
        self._prepare_file()
        self._initialize_schema()

    def _prepare_file(self) -> None:
        """Create or validate the owner-only regular SQLite file."""
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                self._path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow,
                0o600,
            )
        except FileExistsError:
            try:
                descriptor = os.open(self._path, os.O_RDONLY | no_follow)
            except OSError as exc:
                raise RuntimeError("Runner replay database could not be opened safely") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("Runner replay database must be a regular file")
            if metadata.st_uid != os.geteuid():
                raise RuntimeError("Runner replay database must be owned by the runner user")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise RuntimeError("Runner replay database must have owner-only permissions")
        finally:
            os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        """Open one bounded SQLite transaction connection."""
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize_schema(self) -> None:
        """Create the minimal replay table before accepting capabilities."""
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS consumed_runner_capabilities (
                    token_id TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER NOT NULL,
                    CHECK (expires_at > consumed_at)
                )
                """)
            connection.commit()

    def consume(self, token_id: str, *, expires_at: int, current_time: int) -> bool:
        """Atomically consume one unexpired token ID, returning false for replay."""
        if not isinstance(token_id, str) or not token_id:
            raise ValueError("token_id must not be empty")
        if len(token_id) > 128:
            raise ValueError("token_id is too large")
        if type(expires_at) is not int or type(current_time) is not int:
            raise TypeError("replay timestamps must be integers")
        if expires_at <= current_time:
            raise ValueError("capability must be unexpired when consumed")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM consumed_runner_capabilities WHERE expires_at <= ?",
                (current_time,),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO consumed_runner_capabilities (
                        token_id, expires_at, consumed_at
                    ) VALUES (?, ?, ?)
                    """,
                    (token_id, expires_at, current_time),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
            connection.commit()
            return True
        finally:
            connection.close()


class RunnerNoiseSession:
    """One authenticated, bounded, ordered browser-to-runner Noise channel."""

    def __init__(
        self,
        *,
        runner_id: str,
        runner_static_private_key: bytes,
        capability_signing_public_key: bytes,
        replay_store: RunnerCapabilityReplayStore,
    ) -> None:
        if not isinstance(runner_id, str) or not runner_id:
            raise ValueError("runner_id must not be empty")
        if not isinstance(runner_static_private_key, bytes):
            raise TypeError("runner_static_private_key must be bytes")
        if len(runner_static_private_key) != _X25519_KEY_LENGTH:
            raise ValueError("runner_static_private_key must contain exactly 32 bytes")
        if not isinstance(capability_signing_public_key, bytes):
            raise TypeError("capability_signing_public_key must be bytes")
        if len(capability_signing_public_key) != _ED25519_KEY_LENGTH:
            raise ValueError("capability_signing_public_key must contain exactly 32 bytes")
        if not isinstance(replay_store, RunnerCapabilityReplayStore):
            raise TypeError("replay_store must be RunnerCapabilityReplayStore")

        runner_public_key = (
            X25519PrivateKey.from_private_bytes(runner_static_private_key)
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        )
        self._runner_id = runner_id
        self._runner_public_key = (
            base64.urlsafe_b64encode(runner_public_key).rstrip(b"=").decode("ascii")
        )
        self._capability_signing_public_key = bytes(capability_signing_public_key)
        self._replay_store = replay_store
        self._responder = NoiseIkResponder(
            static_private_key=runner_static_private_key,
            prologue=RUNNER_NOISE_PROLOGUE,
        )
        self._capability: VerifiedRunnerCapability | None = None
        self._failed = False
        self._messages_sent = 0
        self._messages_received = 0
        self._ciphertext_bytes = 0

    @property
    def capability(self) -> VerifiedRunnerCapability:
        """Return verified authority after successful handshake acceptance."""
        if self._capability is None:
            raise RuntimeError("Runner Noise capability is not authenticated")
        return self._capability

    def accept_handshake(self, message: bytes, *, current_time: int) -> bytes:
        """Verify client identity, signed scope, expiry, and one-time use."""
        if self._failed or self._capability is not None:
            raise RuntimeError("Runner Noise session cannot accept another handshake")
        if type(current_time) is not int or current_time < 0:
            raise ValueError("current_time must be a non-negative integer")
        try:
            token_bytes = self._responder.read_handshake_message(message)
            token = token_bytes.decode("ascii")
            verified = verify_runner_capability(
                token,
                signing_public_key=self._capability_signing_public_key,
                expected_runner_id=self._runner_id,
                expected_runner_public_key=self._runner_public_key,
                current_time=current_time,
            )
            if verified is None:
                raise ValueError("Runner capability is invalid or expired")
            initiator_public_key = (
                base64.urlsafe_b64encode(self._responder.initiator_static_public_key)
                .rstrip(b"=")
                .decode("ascii")
            )
            if not secrets.compare_digest(
                initiator_public_key,
                verified.initiator_public_key,
            ):
                raise ValueError("Runner capability initiator identity does not match")
            consumed = self._replay_store.consume(
                verified.token_id,
                expires_at=verified.expires_at,
                current_time=current_time,
            )
            if not consumed:
                raise ValueError("Runner capability has already been consumed")
            response_payload = json.dumps(
                {
                    "protocol": RUNNER_PROTOCOL_VERSION,
                    "transfer_id": verified.transfer_id,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            response = self._responder.write_handshake_message(response_payload)
            self._capability = verified
            return response
        except (UnicodeDecodeError, ValueError):
            self._failed = True
            raise

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt one runner response within session byte and message limits."""
        capability = self._require_transport()
        if not isinstance(plaintext, bytes):
            raise TypeError("plaintext must be bytes")
        if self._messages_sent >= _MESSAGES_BEFORE_REHANDSHAKE:
            raise RuntimeError("Runner Noise transport requires a fresh handshake")
        predicted_ciphertext_length = len(plaintext) + 16
        self._require_session_capacity(capability, predicted_ciphertext_length)
        try:
            ciphertext = self._responder.encrypt(plaintext)
        except BaseException:
            self._failed = True
            raise
        self._ciphertext_bytes += len(ciphertext)
        self._messages_sent += 1
        return ciphertext

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt one client request within session byte and message limits."""
        capability = self._require_transport()
        if not isinstance(ciphertext, bytes):
            raise TypeError("ciphertext must be bytes")
        if self._messages_received >= _MESSAGES_BEFORE_REHANDSHAKE:
            raise RuntimeError("Runner Noise transport requires a fresh handshake")
        self._require_session_capacity(capability, len(ciphertext))
        try:
            plaintext = self._responder.decrypt(ciphertext)
        except BaseException:
            self._failed = True
            raise
        self._ciphertext_bytes += len(ciphertext)
        self._messages_received += 1
        return plaintext

    def _require_transport(self) -> VerifiedRunnerCapability:
        """Return authority only while the established channel remains usable."""
        if self._failed:
            raise RuntimeError("Runner Noise transport failed and cannot be reused")
        if self._capability is None:
            raise RuntimeError("Runner Noise transport is not ready")
        return self._capability

    def _require_session_capacity(
        self,
        capability: VerifiedRunnerCapability,
        ciphertext_length: int,
    ) -> None:
        """Reject per-frame and cumulative ciphertext limit violations."""
        if ciphertext_length < 16 or ciphertext_length > capability.max_frame_bytes:
            raise ValueError("Runner Noise ciphertext exceeds the frame limit")
        if self._ciphertext_bytes + ciphertext_length > capability.max_session_bytes:
            raise ValueError("Runner Noise session exceeds the byte limit")
