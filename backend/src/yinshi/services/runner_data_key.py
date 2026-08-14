"""Portable runner data-protection key storage."""

from __future__ import annotations

import os
import re
import secrets
import stat
import time
from pathlib import Path

_KEY_BYTES = 32
_ERROR = "Runner data-protection key is invalid"
_PUBLISH_READ_ATTEMPTS = 100
_PUBLISH_READ_DELAY_SECONDS = 0.01
_TEMPORARY_NAME_PATTERN = re.compile(
    rf"^{re.escape('.yinshi-data-protection-key.tmp-')}[0-9a-f]{{16}}$"
)


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


def _valid_key_metadata(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_size == _KEY_BYTES
    )


def _recover_published_key(path: Path) -> bytes:
    """Finish durable cleanup left between publication directory syncs."""
    directory_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    final_descriptor: int | None = None
    temporary_descriptors: list[tuple[str, int]] = []
    try:
        final_descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        final_metadata = os.fstat(final_descriptor)
        if not _valid_key_metadata(final_metadata):
            raise RuntimeError(_ERROR)
        temporary_names = sorted(
            name
            for name in os.listdir(directory_descriptor)
            if _TEMPORARY_NAME_PATTERN.fullmatch(name)
        )
        if not temporary_names:
            raise RuntimeError(_ERROR)
        for name in temporary_names:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
            temporary_descriptors.append((name, descriptor))
            metadata = os.fstat(descriptor)
            if (
                not _valid_key_metadata(metadata)
                or (metadata.st_dev, metadata.st_ino)
                != (final_metadata.st_dev, final_metadata.st_ino)
                or metadata.st_nlink != final_metadata.st_nlink
            ):
                raise RuntimeError(_ERROR)
        if final_metadata.st_nlink != len(temporary_descriptors) + 1:
            raise RuntimeError(_ERROR)
        key = os.read(final_descriptor, _KEY_BYTES + 1)
        if len(key) != _KEY_BYTES:
            raise RuntimeError(_ERROR)
        for name, _descriptor in temporary_descriptors:
            os.unlink(name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
        if os.fstat(final_descriptor).st_nlink != 1:
            raise RuntimeError(_ERROR)
        return bytes(key)
    except OSError as exc:
        raise RuntimeError(_ERROR) from exc
    finally:
        for _name, descriptor in temporary_descriptors:
            os.close(descriptor)
        if final_descriptor is not None:
            os.close(final_descriptor)
        os.close(directory_descriptor)


def _read_published_key(path: Path) -> bytes:
    """Read a winner or recover its durable interrupted cleanup."""
    for attempt in range(_PUBLISH_READ_ATTEMPTS):
        try:
            return _read_key(path)
        except RuntimeError:
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                raise
            publishing = stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1
            if not publishing:
                raise
            if attempt + 1 == _PUBLISH_READ_ATTEMPTS:
                return _recover_published_key(path)
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
