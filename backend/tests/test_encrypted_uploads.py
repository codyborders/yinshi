"""Verify bounded idempotent encrypted upload assembly in runner memory.

Tests split a payload into chunks, retry one chunk, and require exact size and
SHA-256 verification before bytes can reach an import service.
"""

from __future__ import annotations

import hashlib

import pytest

from yinshi.services.encrypted_uploads import EncryptedUploadManager


def test_encrypted_upload_retries_chunks_and_verifies_completion() -> None:
    """A repeated exact chunk is idempotent while altered retries fail closed."""
    payload = b"pi-config-archive"
    manager = EncryptedUploadManager(clock=lambda: 1_000.0)
    upload = manager.start(
        user_id="account-1",
        purpose="pi_config",
        filename="config.zip",
        size_bytes=len(payload),
        sha256_hex=hashlib.sha256(payload).hexdigest(),
    )

    first = manager.append(
        user_id="account-1",
        upload_id=upload.id,
        chunk_index=0,
        chunk=payload[:8],
    )
    repeated = manager.append(
        user_id="account-1",
        upload_id=upload.id,
        chunk_index=0,
        chunk=payload[:8],
    )
    manager.append(
        user_id="account-1",
        upload_id=upload.id,
        chunk_index=1,
        chunk=payload[8:],
    )

    assert first.next_chunk_index == 1
    assert repeated.next_chunk_index == 1
    completed = manager.complete(user_id="account-1", upload_id=upload.id)
    assert completed.data == payload
    assert completed.filename == "config.zip"
    with pytest.raises(LookupError, match="not found"):
        manager.complete(user_id="account-1", upload_id=upload.id)


def test_encrypted_upload_rejects_wrong_owner_order_and_digest() -> None:
    """Ownership, ordering, and declared digest are checked independently."""
    manager = EncryptedUploadManager(clock=lambda: 1_000.0)
    upload = manager.start(
        user_id="account-1",
        purpose="pi_config",
        filename="config.zip",
        size_bytes=3,
        sha256_hex=hashlib.sha256(b"abc").hexdigest(),
    )

    with pytest.raises(LookupError, match="not found"):
        manager.append(
            user_id="account-2",
            upload_id=upload.id,
            chunk_index=0,
            chunk=b"abc",
        )
    with pytest.raises(ValueError, match="sequence"):
        manager.append(
            user_id="account-1",
            upload_id=upload.id,
            chunk_index=1,
            chunk=b"abc",
        )
    manager.append(
        user_id="account-1",
        upload_id=upload.id,
        chunk_index=0,
        chunk=b"abd",
    )
    with pytest.raises(ValueError, match="digest"):
        manager.complete(user_id="account-1", upload_id=upload.id)
