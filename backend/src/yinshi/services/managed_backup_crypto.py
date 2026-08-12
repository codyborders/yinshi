"""Seal managed-backup jobs to one persisted runner identity."""

from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_ENVELOPE_MAGIC = b"YINSHI-MANAGED-JOB-V1\n"
_X25519_KEY_BYTES = 32
_NONCE_BYTES = 12
_ARCHIVE_KEY_ENVELOPE = b"YINSHI-MANAGED-ARCHIVE-KEY-V1\n"


def _decode_public_key(value: str) -> bytes:
    """Decode one canonical unpadded X25519 public key."""
    if not isinstance(value, str) or not value:
        raise ValueError("runner_public_key must not be empty")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise ValueError("runner_public_key is not canonical base64url") from error
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if len(decoded) != _X25519_KEY_BYTES or canonical != value:
        raise ValueError("runner_public_key must contain 32 canonical bytes")
    return decoded


def _job_aad(job_id: str) -> bytes:
    """Return authenticated context for one exact maintenance job."""
    if not isinstance(job_id, str) or not job_id or len(job_id) > 128:
        raise ValueError("job_id must be bounded non-empty text")
    return _ENVELOPE_MAGIC + job_id.encode("utf-8")


def _job_key(shared_secret: bytes, ephemeral_public_key: bytes, runner_public_key: bytes) -> bytes:
    """Derive one job key from both envelope identities."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"yinshi-managed-job-seal-v1",
        info=ephemeral_public_key + runner_public_key,
    ).derive(shared_secret)


def _archive_key_aad(user_id: str, archive_id: str, key_id: str) -> bytes:
    """Build exact authenticated context for one stored archive key."""
    values = (user_id, archive_id, key_id)
    if any(not isinstance(value, str) or not value or len(value) > 256 for value in values):
        raise ValueError("archive key context must contain bounded non-empty text")
    return _ARCHIVE_KEY_ENVELOPE + "\x1f".join(values).encode("utf-8")


def wrap_managed_archive_key(
    archive_key: bytes,
    *,
    user_id: str,
    archive_id: str,
    key_id: str,
    wrapping_key: bytes,
) -> bytes:
    """Wrap one random archive key under server-controlled key material."""
    if not isinstance(archive_key, bytes) or len(archive_key) != 32:
        raise ValueError("archive_key must contain exactly 32 bytes")
    if not isinstance(wrapping_key, bytes) or len(wrapping_key) < 32:
        raise ValueError("wrapping_key must contain at least 32 bytes")
    aad = _archive_key_aad(user_id, archive_id, key_id)
    derived_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"yinshi-managed-archive-key-wrap-v1",
        info=key_id.encode("utf-8"),
    ).derive(wrapping_key)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(derived_key).encrypt(nonce, archive_key, aad)
    payload = {
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        "key_id": key_id,
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "version": 1,
    }
    return _ARCHIVE_KEY_ENVELOPE + json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def unwrap_managed_archive_key(
    envelope: bytes,
    *,
    user_id: str,
    archive_id: str,
    keyring: dict[str, bytes],
) -> bytes:
    """Unwrap one archive key after checking every stored context field."""
    if not isinstance(envelope, bytes) or not envelope.startswith(_ARCHIVE_KEY_ENVELOPE):
        raise ValueError("managed archive key could not be unwrapped")
    try:
        payload = json.loads(envelope[len(_ARCHIVE_KEY_ENVELOPE) :].decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "ciphertext",
            "key_id",
            "nonce",
            "version",
        }:
            raise ValueError
        key_id = payload["key_id"]
        if payload["version"] != 1 or not isinstance(key_id, str) or key_id not in keyring:
            raise ValueError
        wrapping_key = keyring[key_id]
        if not isinstance(wrapping_key, bytes) or len(wrapping_key) < 32:
            raise ValueError
        derived_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"yinshi-managed-archive-key-wrap-v1",
            info=key_id.encode("utf-8"),
        ).derive(wrapping_key)
        nonce = base64.b64decode(payload["nonce"], altchars=b"-_", validate=True)
        ciphertext = base64.b64decode(payload["ciphertext"], altchars=b"-_", validate=True)
        archive_key = AESGCM(derived_key).decrypt(
            nonce,
            ciphertext,
            _archive_key_aad(user_id, archive_id, key_id),
        )
    except Exception:
        raise ValueError("managed archive key could not be unwrapped") from None
    if len(archive_key) != 32:
        raise ValueError("managed archive key could not be unwrapped")
    return archive_key


def seal_managed_backup_job(
    payload: dict[str, Any],
    *,
    runner_public_key: str,
    job_id: str,
) -> bytes:
    """Encrypt one JSON job so only the intended runner can open it."""
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dictionary")
    try:
        plaintext = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("payload must be JSON-serializable") from error
    runner_public_bytes = _decode_public_key(runner_public_key)
    runner_public = X25519PublicKey.from_public_bytes(runner_public_bytes)
    ephemeral_private = X25519PrivateKey.generate()
    ephemeral_public = ephemeral_private.public_key().public_bytes_raw()
    shared_secret = ephemeral_private.exchange(runner_public)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(_job_key(shared_secret, ephemeral_public, runner_public_bytes)).encrypt(
        nonce, plaintext, _job_aad(job_id)
    )
    return _ENVELOPE_MAGIC + ephemeral_public + nonce + ciphertext


def open_managed_backup_job(
    envelope: bytes,
    *,
    runner_private_key: bytes,
    expected_job_id: str,
) -> dict[str, Any]:
    """Open one identity-bound job and return its strict JSON object."""
    if not isinstance(envelope, bytes) or not envelope.startswith(_ENVELOPE_MAGIC):
        raise ValueError("managed backup job could not be opened")
    if not isinstance(runner_private_key, bytes) or len(runner_private_key) != 32:
        raise ValueError("runner_private_key must contain exactly 32 bytes")
    minimum = len(_ENVELOPE_MAGIC) + _X25519_KEY_BYTES + _NONCE_BYTES + 16
    if len(envelope) <= minimum:
        raise ValueError("managed backup job could not be opened")
    offset = len(_ENVELOPE_MAGIC)
    ephemeral_public_bytes = envelope[offset : offset + _X25519_KEY_BYTES]
    offset += _X25519_KEY_BYTES
    nonce = envelope[offset : offset + _NONCE_BYTES]
    ciphertext = envelope[offset + _NONCE_BYTES :]
    try:
        runner_private = X25519PrivateKey.from_private_bytes(runner_private_key)
        runner_public_bytes = runner_private.public_key().public_bytes_raw()
        shared_secret = runner_private.exchange(
            X25519PublicKey.from_public_bytes(ephemeral_public_bytes)
        )
        plaintext = AESGCM(
            _job_key(shared_secret, ephemeral_public_bytes, runner_public_bytes)
        ).decrypt(nonce, ciphertext, _job_aad(expected_job_id))
        payload = json.loads(plaintext.decode("utf-8"))
    except Exception:
        raise ValueError("managed backup job could not be opened") from None
    if not isinstance(payload, dict):
        raise ValueError("managed backup job must contain a JSON object")
    return payload
