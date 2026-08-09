"""Noise IK identity storage and encrypted runner transport primitives."""

from __future__ import annotations

import base64
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from noise.connection import Keypair, NoiseConnection
from noise.exceptions import (
    NoiseHandshakeError,
    NoiseInvalidMessage,
    NoiseMaxNonceError,
    NoiseValidationError,
    NoiseValueError,
)

_NOISE_PROTOCOL_NAME = b"Noise_IK_25519_ChaChaPoly_SHA256"
_X25519_KEY_LENGTH = 32
_NOISE_IK_FIRST_MESSAGE_MIN_LENGTH = 96
_NOISE_IK_SECOND_MESSAGE_MIN_LENGTH = 48
_NOISE_MESSAGE_MAX_LENGTH = 65_535


@dataclass(frozen=True, slots=True)
class RunnerNoiseKeypair:
    """Persistent runner static identity represented as raw X25519 keys."""

    private_key: bytes
    public_key: bytes

    @property
    def public_key_base64url(self) -> str:
        """Return the canonical unpadded base64url public key."""
        assert len(self.public_key) == _X25519_KEY_LENGTH
        return base64.urlsafe_b64encode(self.public_key).rstrip(b"=").decode("ascii")


def _require_key_bytes(value: bytes, name: str) -> bytes:
    """Copy one exact-length X25519 key or reject malformed caller state."""
    if not isinstance(value, bytes):
        raise TypeError(f"{name} must be bytes")
    if len(value) != _X25519_KEY_LENGTH:
        raise ValueError(f"{name} must contain exactly {_X25519_KEY_LENGTH} bytes")
    return bytes(value)


def _read_owner_only_key(path: Path) -> bytes:
    """Read a regular owner-only key file without following symlinks."""
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, os.O_RDONLY | no_follow)
    except OSError as exc:
        raise RuntimeError(f"Runner Noise private key could not be opened: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("Runner Noise private key must be a regular file")
        if metadata.st_uid != os.geteuid():
            raise RuntimeError("Runner Noise private key must be owned by the runner user")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError("Runner Noise private key must have owner-only permissions")
        private_key = os.read(descriptor, _X25519_KEY_LENGTH + 1)
    finally:
        os.close(descriptor)
    return _require_key_bytes(private_key, "Runner Noise private key")


def _create_owner_only_key(path: Path, private_key: bytes) -> bool:
    """Atomically create one owner-only key, returning false on a creation race."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | no_follow, 0o600)
    except FileExistsError:
        return False
    try:
        written = os.write(descriptor, private_key)
        if written != len(private_key):
            raise RuntimeError("Runner Noise private key write was incomplete")
        os.fsync(descriptor)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    return True


def load_or_create_runner_noise_keypair(path: Path) -> RunnerNoiseKeypair:
    """Load or atomically create the runner's owner-only static Noise keypair."""
    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")
    if not path.is_absolute():
        raise ValueError("Runner Noise private key path must be absolute")

    if path.exists() or path.is_symlink():
        private_key_bytes = _read_owner_only_key(path)
    else:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        generated_key = X25519PrivateKey.generate()
        generated_bytes = generated_key.private_bytes_raw()
        if _create_owner_only_key(path, generated_bytes):
            private_key_bytes = generated_bytes
        else:
            private_key_bytes = _read_owner_only_key(path)

    private_key = X25519PrivateKey.from_private_bytes(private_key_bytes)
    public_key_bytes = private_key.public_key().public_bytes_raw()
    assert len(public_key_bytes) == _X25519_KEY_LENGTH
    return RunnerNoiseKeypair(
        private_key=private_key_bytes,
        public_key=public_key_bytes,
    )


class NoiseIkResponder:
    """Strict two-message Noise IK responder with authenticated transport ciphers."""

    def __init__(
        self,
        *,
        static_private_key: bytes,
        prologue: bytes = b"",
        ephemeral_private_key: bytes | None = None,
    ) -> None:
        static_key = _require_key_bytes(static_private_key, "static_private_key")
        if not isinstance(prologue, bytes):
            raise TypeError("prologue must be bytes")
        if len(prologue) > _NOISE_MESSAGE_MAX_LENGTH:
            raise ValueError("prologue is too large")

        connection = NoiseConnection.from_name(_NOISE_PROTOCOL_NAME)
        connection.set_as_responder()
        connection.set_keypair_from_private_bytes(Keypair.STATIC, static_key)
        if ephemeral_private_key is not None:
            ephemeral_key = _require_key_bytes(ephemeral_private_key, "ephemeral_private_key")
            connection.set_keypair_from_private_bytes(Keypair.EPHEMERAL, ephemeral_key)
        connection.set_prologue(prologue)
        connection.start_handshake()

        self._connection = connection
        self._first_message_read = False
        self._handshake_complete = False
        self._initiator_static_public_key: bytes | None = None

    @property
    def initiator_static_public_key(self) -> bytes:
        """Return authenticated initiator identity after reading the first message."""
        if self._initiator_static_public_key is None:
            raise RuntimeError("Noise IK initiator identity is not authenticated")
        return bytes(self._initiator_static_public_key)

    @property
    def handshake_hash(self) -> bytes:
        """Return channel-binding hash after handshake completion."""
        if not self._handshake_complete:
            raise RuntimeError("Noise IK handshake is not complete")
        handshake_hash = self._connection.get_handshake_hash()
        if not isinstance(handshake_hash, bytes) or len(handshake_hash) != 32:
            raise RuntimeError("Noise IK handshake hash is invalid")
        return bytes(handshake_hash)

    def read_handshake_message(self, message: bytes) -> bytes:
        """Authenticate and decrypt the initiator's single IK handshake message."""
        if self._first_message_read:
            raise RuntimeError("Noise IK initiator message was already read")
        if not isinstance(message, bytes):
            raise TypeError("message must be bytes")
        if not _NOISE_IK_FIRST_MESSAGE_MIN_LENGTH <= len(message) <= _NOISE_MESSAGE_MAX_LENGTH:
            raise ValueError("Noise IK initiator message has an invalid length")

        try:
            payload = bytes(self._connection.read_message(message))
        except (
            InvalidTag,
            NoiseHandshakeError,
            NoiseInvalidMessage,
            NoiseMaxNonceError,
            NoiseValidationError,
            NoiseValueError,
        ) as exc:
            raise ValueError("Noise IK initiator message failed authentication") from exc
        handshake_state = self._connection.noise_protocol.handshake_state
        remote_static = handshake_state.rs.public_bytes
        self._initiator_static_public_key = _require_key_bytes(
            remote_static,
            "Noise IK initiator public key",
        )
        self._first_message_read = True
        return payload

    def write_handshake_message(self, payload: bytes) -> bytes:
        """Encrypt the responder payload and finish the IK handshake."""
        if not self._first_message_read or self._handshake_complete:
            raise RuntimeError("Noise IK responder is not ready to finish the handshake")
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        if len(payload) > _NOISE_MESSAGE_MAX_LENGTH - _NOISE_IK_SECOND_MESSAGE_MIN_LENGTH:
            raise ValueError("Noise IK responder payload is too large")

        try:
            message = bytes(self._connection.write_message(payload))
        except (
            NoiseHandshakeError,
            NoiseInvalidMessage,
            NoiseMaxNonceError,
            NoiseValidationError,
            NoiseValueError,
        ) as exc:
            raise ValueError("Noise IK responder message could not be created") from exc
        if not _NOISE_IK_SECOND_MESSAGE_MIN_LENGTH <= len(message) <= _NOISE_MESSAGE_MAX_LENGTH:
            raise RuntimeError("Noise IK responder produced an invalid message")
        self._handshake_complete = True
        return message

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt one ordered runner-to-client transport message."""
        if not self._handshake_complete:
            raise RuntimeError("Noise IK transport is not ready")
        if not isinstance(plaintext, bytes):
            raise TypeError("plaintext must be bytes")
        if len(plaintext) > _NOISE_MESSAGE_MAX_LENGTH - 16:
            raise ValueError("Noise transport plaintext is too large")
        try:
            return bytes(self._connection.encrypt(plaintext))
        except (NoiseMaxNonceError, NoiseValidationError, NoiseValueError) as exc:
            raise ValueError("Noise transport encryption failed") from exc

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Authenticate and decrypt one ordered client-to-runner transport message."""
        if not self._handshake_complete:
            raise RuntimeError("Noise IK transport is not ready")
        if not isinstance(ciphertext, bytes):
            raise TypeError("ciphertext must be bytes")
        if not 16 <= len(ciphertext) <= _NOISE_MESSAGE_MAX_LENGTH:
            raise ValueError("Noise transport ciphertext has an invalid length")
        try:
            return bytes(self._connection.decrypt(ciphertext))
        except (
            InvalidTag,
            NoiseInvalidMessage,
            NoiseMaxNonceError,
            NoiseValidationError,
            NoiseValueError,
        ) as exc:
            raise ValueError("Noise transport ciphertext failed authentication") from exc
