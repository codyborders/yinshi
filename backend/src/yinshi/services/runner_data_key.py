"""Portable runner data-protection key storage."""

from __future__ import annotations

import os
import secrets
import stat
import time
from pathlib import Path

_KEY_BYTES = 32
_ERROR = "Runner data-protection key is invalid"
_PUBLISH_READ_ATTEMPTS = 100
_PUBLISH_READ_DELAY_SECONDS = 0.01


def _require_key(value: bytes, name: str) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError(f"{name} must be bytes")
    if len(value) != _KEY_BYTES:
        raise ValueError(f"{name} must contain exactly 32 bytes")
    return bytes(value)


def _read_key(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RuntimeError(_ERROR) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise RuntimeError(_ERROR)
        key = os.read(descriptor, _KEY_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(key) != _KEY_BYTES:
        raise RuntimeError(_ERROR)
    return bytes(key)


def _read_published_key(path: Path) -> bytes:
    """Read a winner while tolerating its brief hard-link publication window."""
    for attempt in range(_PUBLISH_READ_ATTEMPTS):
        try:
            return _read_key(path)
        except RuntimeError:
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                raise
            publishing = stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1
            if not publishing or attempt + 1 == _PUBLISH_READ_ATTEMPTS:
                raise
            time.sleep(_PUBLISH_READ_DELAY_SECONDS)
    raise RuntimeError(_ERROR)


def _create_key(path: Path, key: bytes) -> bool:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    temporary_path: Path | None = None
    descriptor: int | None = None
    for _attempt in range(16):
        candidate = path.with_name(f"{path.name}.tmp-{secrets.token_hex(8)}")
        try:
            descriptor = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue
        temporary_path = candidate
        break
    if temporary_path is None or descriptor is None:
        raise RuntimeError(_ERROR)

    published = False
    publication_durable = False
    try:
        try:
            remaining = memoryview(key)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise RuntimeError(_ERROR)
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
            descriptor = None
        try:
            os.link(temporary_path, path, follow_symlinks=False)
            published = True
        except FileExistsError:
            published = False
        if not published:
            os.unlink(temporary_path)
            temporary_path = None
            return False
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
            publication_durable = True
            os.unlink(temporary_path)
            temporary_path = None
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return True
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if published and not publication_durable:
            try:
                os.unlink(path)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
                temporary_path = None
            except OSError:
                pass
        raise


def load_or_create_runner_data_key(
    key_path: Path,
    database_root: Path,
    legacy_noise_private_key: bytes,
) -> bytes:
    """Load or create the backed-up runner storage key."""
    if not isinstance(key_path, Path) or not isinstance(database_root, Path):
        raise TypeError("key paths must be pathlib.Path values")
    if not key_path.is_absolute() or not database_root.is_absolute():
        raise ValueError("runner data-protection key paths must be absolute")
    if key_path.parent != database_root:
        raise ValueError("runner data-protection key must be inside database root")
    legacy_key = _require_key(legacy_noise_private_key, "legacy_noise_private_key")
    database_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = database_root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise RuntimeError(_ERROR)
    database_root.chmod(0o700)
    if key_path.exists() or key_path.is_symlink():
        return _read_published_key(key_path)
    initialized = False
    for name in ("control.db", "legacy.db"):
        durable_path = database_root / name
        try:
            durable_metadata = durable_path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(durable_metadata.st_mode) or durable_metadata.st_uid != os.geteuid():
            raise RuntimeError(_ERROR)
        initialized = True
    key = legacy_key if initialized else secrets.token_bytes(_KEY_BYTES)
    if _create_key(key_path, key):
        return key
    return _read_published_key(key_path)
