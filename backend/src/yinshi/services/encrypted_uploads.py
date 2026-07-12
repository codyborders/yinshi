"""Owner-scoped in-memory assembly for encrypted runner file uploads."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

_UPLOAD_BYTES_MAX = 50 * 1024 * 1024
_UPLOAD_BYTES_RESERVED_MAX = 100 * 1024 * 1024
_UPLOAD_COUNT_MAX = 8
_UPLOAD_CHUNK_BYTES_MAX = 24_000
_UPLOAD_LIFETIME_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class EncryptedUpload:
    id: str
    purpose: str
    filename: str
    size_bytes: int
    next_chunk_index: int


@dataclass(frozen=True, slots=True)
class CompletedEncryptedUpload:
    purpose: str
    filename: str
    data: bytes


@dataclass(slots=True)
class _UploadState:
    id: str
    user_id: str
    purpose: str
    filename: str
    size_bytes: int
    sha256_hex: str
    expires_at: float
    data: bytearray = field(default_factory=bytearray)
    chunk_digests: list[tuple[int, str]] = field(default_factory=list)


class EncryptedUploadManager:
    """Assemble retry-safe chunks only after Noise has decrypted each request."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._uploads: dict[str, _UploadState] = {}

    def start(
        self,
        *,
        user_id: str,
        purpose: str,
        filename: str,
        size_bytes: int,
        sha256_hex: str,
    ) -> EncryptedUpload:
        """Reserve one bounded upload after validating immutable metadata."""
        self._validate_user_id(user_id)
        self._validate_metadata(purpose, filename, size_bytes, sha256_hex)
        self._reap_expired()
        if len(self._uploads) >= _UPLOAD_COUNT_MAX:
            raise RuntimeError("encrypted upload count limit reached")
        reserved_bytes = sum(upload.size_bytes for upload in self._uploads.values())
        if reserved_bytes + size_bytes > _UPLOAD_BYTES_RESERVED_MAX:
            raise RuntimeError("encrypted upload byte limit reached")
        upload_id = uuid.uuid4().hex
        if upload_id in self._uploads:
            raise RuntimeError("encrypted upload ID collision")
        state = _UploadState(
            id=upload_id,
            user_id=user_id,
            purpose=purpose,
            filename=filename,
            size_bytes=size_bytes,
            sha256_hex=sha256_hex,
            expires_at=self._clock() + _UPLOAD_LIFETIME_SECONDS,
        )
        self._uploads[upload_id] = state
        return self._public_upload(state)

    def append(
        self,
        *,
        user_id: str,
        upload_id: str,
        chunk_index: int,
        chunk: bytes,
    ) -> EncryptedUpload:
        """Append the next chunk or accept an exact digest-matching retry."""
        state = self._owned_upload(user_id, upload_id)
        if type(chunk_index) is not int or chunk_index < 0:
            raise ValueError("encrypted upload chunk_index must be non-negative")
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > _UPLOAD_CHUNK_BYTES_MAX:
            raise ValueError("encrypted upload chunk has an invalid length")
        chunk_digest = hashlib.sha256(chunk).hexdigest()
        next_chunk_index = len(state.chunk_digests)
        if chunk_index < next_chunk_index:
            stored_length, stored_digest = state.chunk_digests[chunk_index]
            if stored_length != len(chunk) or stored_digest != chunk_digest:
                raise ValueError("encrypted upload retry does not match stored chunk")
            return self._public_upload(state)
        if chunk_index != next_chunk_index:
            raise ValueError("encrypted upload chunk sequence is not contiguous")
        if len(state.data) + len(chunk) > state.size_bytes:
            raise ValueError("encrypted upload exceeds declared size")
        state.data.extend(chunk)
        state.chunk_digests.append((len(chunk), chunk_digest))
        state.expires_at = self._clock() + _UPLOAD_LIFETIME_SECONDS
        return self._public_upload(state)

    def complete(self, *, user_id: str, upload_id: str) -> CompletedEncryptedUpload:
        """Consume one upload only when size and SHA-256 match declared metadata."""
        state = self._owned_upload(user_id, upload_id)
        self._uploads.pop(upload_id, None)
        if len(state.data) != state.size_bytes:
            raise ValueError("encrypted upload size does not match declaration")
        if hashlib.sha256(state.data).hexdigest() != state.sha256_hex:
            state.data[:] = b"\x00" * len(state.data)
            raise ValueError("encrypted upload digest does not match declaration")
        data = bytes(state.data)
        state.data[:] = b"\x00" * len(state.data)
        return CompletedEncryptedUpload(
            purpose=state.purpose,
            filename=state.filename,
            data=data,
        )

    def cancel(self, *, user_id: str, upload_id: str) -> None:
        """Discard one upload idempotently without exposing another owner's state."""
        self._validate_user_id(user_id)
        self._validate_upload_id(upload_id)
        state = self._uploads.get(upload_id)
        if state is None:
            return
        if state.user_id != user_id:
            raise LookupError("encrypted upload not found")
        self._uploads.pop(upload_id, None)
        state.data[:] = b"\x00" * len(state.data)

    def _owned_upload(self, user_id: str, upload_id: str) -> _UploadState:
        self._validate_user_id(user_id)
        self._validate_upload_id(upload_id)
        self._reap_expired()
        state = self._uploads.get(upload_id)
        if state is None or state.user_id != user_id:
            raise LookupError("encrypted upload not found")
        return state

    def _reap_expired(self) -> None:
        current_time = self._clock()
        expired_ids = [
            upload_id
            for upload_id, state in self._uploads.items()
            if state.expires_at <= current_time
        ]
        for upload_id in expired_ids:
            state = self._uploads.pop(upload_id)
            state.data[:] = b"\x00" * len(state.data)

    @staticmethod
    def _public_upload(state: _UploadState) -> EncryptedUpload:
        return EncryptedUpload(
            id=state.id,
            purpose=state.purpose,
            filename=state.filename,
            size_bytes=state.size_bytes,
            next_chunk_index=len(state.chunk_digests),
        )

    @staticmethod
    def _validate_metadata(
        purpose: str,
        filename: str,
        size_bytes: int,
        sha256_hex: str,
    ) -> None:
        if purpose != "pi_config":
            raise ValueError("encrypted upload purpose is unsupported")
        if (
            not isinstance(filename, str)
            or not filename
            or len(filename) > 255
            or filename != filename.strip()
            or "/" in filename
            or "\\" in filename
            or not filename.lower().endswith(".zip")
        ):
            raise ValueError("encrypted upload filename must be a simple zip name")
        if type(size_bytes) is not int or not 1 <= size_bytes <= _UPLOAD_BYTES_MAX:
            raise ValueError("encrypted upload size is invalid")
        if (
            not isinstance(sha256_hex, str)
            or len(sha256_hex) != 64
            or any(character not in "0123456789abcdef" for character in sha256_hex)
        ):
            raise ValueError("encrypted upload SHA-256 is invalid")

    @staticmethod
    def _validate_user_id(user_id: str) -> None:
        if not isinstance(user_id, str) or not user_id or len(user_id) > 256:
            raise ValueError("encrypted upload user_id is invalid")

    @staticmethod
    def _validate_upload_id(upload_id: str) -> None:
        if (
            not isinstance(upload_id, str)
            or len(upload_id) != 32
            or any(character not in "0123456789abcdef" for character in upload_id)
        ):
            raise ValueError("encrypted upload ID is invalid")
