"""Host-local process lock for the in-memory opaque runner relay."""

from __future__ import annotations

import fcntl
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class _HeldProcessLock:
    file_descriptor: int
    reference_count: int
    process_id: int


_REGISTRY_LOCK = threading.RLock()
_HELD_PROCESS_LOCKS: dict[Path, _HeldProcessLock] = {}


class RelayProcessLock:
    """Prevent unsupported multi-process relay routing on one deployment host."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("relay process lock path must be absolute")
        self._path = path
        self._acquired = False

    def acquire(self) -> None:
        """Acquire exclusive ownership without blocking another API process."""
        with _REGISTRY_LOCK:
            if self._acquired:
                return
            process_id = os.getpid()
            held_lock = _HELD_PROCESS_LOCKS.get(self._path)
            if held_lock is not None and held_lock.process_id == process_id:
                held_lock.reference_count += 1
                self._acquired = True
                return
            if held_lock is not None:
                # A fork inherits Python memory and file descriptors. The child must
                # discard that inherited descriptor before testing the kernel lock.
                os.close(held_lock.file_descriptor)
                del _HELD_PROCESS_LOCKS[self._path]

            file_descriptor = self._acquire_file_descriptor(process_id)
            _HELD_PROCESS_LOCKS[self._path] = _HeldProcessLock(
                file_descriptor=file_descriptor,
                reference_count=1,
                process_id=process_id,
            )
            self._acquired = True

    def _acquire_file_descriptor(self, process_id: int) -> int:
        """Open, validate, and lock the owner-controlled relay lock file."""
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(self._path, flags, 0o600)
        try:
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise RuntimeError("relay process lock must be an owner-controlled file")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                os.fchmod(file_descriptor, 0o600)
            try:
                fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "runner relay requires a single hosted process or affinity routing"
                ) from exc
            os.ftruncate(file_descriptor, 0)
            os.write(file_descriptor, str(process_id).encode("ascii"))
            os.fsync(file_descriptor)
        except (OSError, RuntimeError):
            os.close(file_descriptor)
            raise
        return file_descriptor

    def release(self) -> None:
        """Release process ownership during graceful application shutdown."""
        with _REGISTRY_LOCK:
            if not self._acquired:
                return
            self._acquired = False
            held_lock = _HELD_PROCESS_LOCKS.get(self._path)
            if held_lock is None or held_lock.process_id != os.getpid():
                return
            held_lock.reference_count -= 1
            assert held_lock.reference_count >= 0
            if held_lock.reference_count > 0:
                return
            del _HELD_PROCESS_LOCKS[self._path]
            try:
                fcntl.flock(held_lock.file_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(held_lock.file_descriptor)
