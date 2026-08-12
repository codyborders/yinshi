"""Repository lifecycle locking and reversible managed-path moves."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import os
import shutil
import sqlite3
import stat
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from yinshi.config import get_settings
from yinshi.tenant import TenantContext
from yinshi.utils.paths import is_path_inside


@dataclass(slots=True)
class _LockState:
    """Track one keyed lock while callers hold or await it."""

    lock: asyncio.Lock
    references: int = 0


@dataclass(frozen=True, slots=True)
class QuarantinedPath:
    """Record one reversible managed-path move."""

    source: Path
    target: Path
    run_directory: Path


_LOCK_STATES: dict[str, _LockState] = {}
_LOCK_STATES_GUARD = threading.Lock()
_LOCK_DIRECTORY = ".repository-lifecycle-locks"
_LOCK_POLL_INTERVAL_SECONDS = 0.05


def _validate_directory(path_stat: os.stat_result, *, private: bool) -> None:
    """Require an owner-controlled directory."""
    if not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError("repository lock path must be a directory")
    if path_stat.st_uid != os.geteuid():
        raise ValueError("repository lock directory must be owned by this user")
    mode = stat.S_IMODE(path_stat.st_mode)
    if mode & 0o022 or private and mode != 0o700:
        raise ValueError("repository lock directory permissions are unsafe")


def _directory_open_flags() -> int:
    """Return secure flags for opening a directory descriptor."""
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return flags


def _open_lock_file(lock_root: Path, repo_id: str) -> int:
    """Open a stable owner-only lock file below a trusted root."""
    if not lock_root.is_absolute():
        raise ValueError("repository lock root must be absolute")
    try:
        root_path_stat = os.lstat(lock_root)
    except OSError as exc:
        raise ValueError("repository lock root is unavailable") from exc
    if stat.S_ISLNK(root_path_stat.st_mode):
        raise ValueError("repository lock root must not be a link")
    _validate_directory(root_path_stat, private=False)

    root_fd = -1
    lock_directory_fd = -1
    try:
        root_fd = os.open(lock_root, _directory_open_flags())
        root_fd_stat = os.fstat(root_fd)
        if (root_fd_stat.st_dev, root_fd_stat.st_ino) != (
            root_path_stat.st_dev,
            root_path_stat.st_ino,
        ):
            raise ValueError("repository lock root changed during validation")
        _validate_directory(root_fd_stat, private=False)

        try:
            os.mkdir(_LOCK_DIRECTORY, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        lock_directory_stat = os.stat(
            _LOCK_DIRECTORY,
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(lock_directory_stat.st_mode):
            raise ValueError("repository lock directory must not be a link")
        _validate_directory(lock_directory_stat, private=True)

        lock_directory_fd = os.open(
            _LOCK_DIRECTORY,
            _directory_open_flags(),
            dir_fd=root_fd,
        )
        opened_directory_stat = os.fstat(lock_directory_fd)
        if (opened_directory_stat.st_dev, opened_directory_stat.st_ino) != (
            lock_directory_stat.st_dev,
            lock_directory_stat.st_ino,
        ):
            raise ValueError("repository lock directory changed during validation")
        _validate_directory(opened_directory_stat, private=True)

        lock_name = f"{hashlib.sha256(repo_id.encode('utf-8')).hexdigest()}.lock"
        file_flags = os.O_RDWR | os.O_CREAT
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        lock_fd = os.open(lock_name, file_flags, 0o600, dir_fd=lock_directory_fd)
        try:
            lock_path_stat = os.stat(
                lock_name,
                dir_fd=lock_directory_fd,
                follow_symlinks=False,
            )
            lock_fd_stat = os.fstat(lock_fd)
            if stat.S_ISLNK(lock_path_stat.st_mode) or not stat.S_ISREG(lock_fd_stat.st_mode):
                raise ValueError("repository lock file must be a regular non-link file")
            if (lock_fd_stat.st_dev, lock_fd_stat.st_ino) != (
                lock_path_stat.st_dev,
                lock_path_stat.st_ino,
            ):
                raise ValueError("repository lock file changed during validation")
            if lock_fd_stat.st_uid != os.geteuid():
                raise ValueError("repository lock file must be owned by this user")
            if stat.S_IMODE(lock_fd_stat.st_mode) != 0o600:
                raise ValueError("repository lock file permissions are unsafe")
            return lock_fd
        except BaseException:
            os.close(lock_fd)
            raise
    except OSError as exc:
        raise ValueError("repository lock path validation failed") from exc
    finally:
        if lock_directory_fd >= 0:
            os.close(lock_directory_fd)
        if root_fd >= 0:
            os.close(root_fd)


def repository_lifecycle_root(
    db: sqlite3.Connection,
    tenant: TenantContext | None,
) -> Path:
    """Choose a shared lock root from the operation database and tenant."""
    application_root = Path(get_settings().db_path).expanduser().absolute().parent
    if tenant is None:
        return application_root
    database_row = db.execute("PRAGMA database_list").fetchone()
    if database_row is None or not database_row[2]:
        return application_root
    database_path = Path(str(database_row[2])).expanduser().absolute()
    if not is_path_inside(str(database_path), tenant.data_dir):
        return application_root
    return Path(tenant.data_dir)


async def _acquire_file_lock(lock_fd: int) -> None:
    """Acquire an advisory lock with cancellation-friendly polling."""
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        await asyncio.sleep(_LOCK_POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def repository_lifecycle(repo_id: str, lock_root: Path) -> AsyncIterator[None]:
    """Serialize lifecycle work for one repository across worker processes."""
    if not repo_id:
        raise ValueError("repo_id must not be empty")

    state_key = f"{lock_root.absolute()}:{repo_id}"
    with _LOCK_STATES_GUARD:
        state = _LOCK_STATES.setdefault(state_key, _LockState(lock=asyncio.Lock()))
        state.references += 1

    acquired = False
    lock_fd = -1
    try:
        await state.lock.acquire()
        acquired = True
        lock_fd = _open_lock_file(lock_root, repo_id)
        await _acquire_file_lock(lock_fd)
        yield
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        if acquired:
            state.lock.release()
        with _LOCK_STATES_GUARD:
            state.references -= 1
            if state.references == 0 and not state.lock.locked():
                _LOCK_STATES.pop(state_key, None)


class ManagedPathQuarantine:
    """Move trusted managed paths aside until database deletion succeeds."""

    def __init__(self) -> None:
        self._token = uuid.uuid4().hex
        self._entries: list[QuarantinedPath] = []

    @property
    def entries(self) -> tuple[QuarantinedPath, ...]:
        """Return current moved-path records for cleanup reporting."""
        return tuple(self._entries)

    def validate(self, source: Path, trusted_root: Path) -> None:
        """Validate one existing source against its trusted owner-controlled root."""
        if not source.is_absolute() or not trusted_root.is_absolute():
            raise ValueError("managed deletion paths must be absolute")
        if not source.exists() and not source.is_symlink():
            return
        if source.is_symlink():
            raise ValueError("managed deletion source must not be a symlink")
        if trusted_root.is_symlink() or not trusted_root.is_dir():
            raise ValueError("managed deletion root must be a real directory")
        if not is_path_inside(str(source), str(trusted_root)):
            raise ValueError("managed deletion source is outside its trusted root")

        root_stat = trusted_root.stat()
        if root_stat.st_uid != os.geteuid():
            raise ValueError("managed deletion root must be owned by this user")

    def move(self, source: Path, trusted_root: Path) -> None:
        """Move one validated source into an owner-only directory on its filesystem."""
        self.validate(source, trusted_root)
        if not source.exists():
            return

        parent = trusted_root / ".yinshi-delete-quarantine"
        run_directory = parent / self._token
        self._ensure_private_directory(parent)
        self._ensure_private_directory(run_directory)
        target = run_directory / f"{len(self._entries):04d}"
        if target.exists() or target.is_symlink():
            raise RuntimeError("managed deletion target already exists")

        os.rename(source, target)
        self._entries.append(
            QuarantinedPath(
                source=source,
                target=target,
                run_directory=run_directory,
            )
        )

    def restore(self) -> None:
        """Restore every moved path in reverse order."""
        first_error: OSError | RuntimeError | ValueError | None = None
        for entry in reversed(self._entries.copy()):
            try:
                if entry.source.exists() or entry.source.is_symlink():
                    raise RuntimeError("managed deletion source was recreated during rollback")
                if entry.target.exists() or entry.target.is_symlink():
                    os.rename(entry.target, entry.source)
                self._entries.remove(entry)
                self._remove_empty_directories(entry.run_directory)
            except (OSError, RuntimeError, ValueError) as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def discard(self) -> None:
        """Permanently remove every moved path after database commit."""
        first_error: OSError | RuntimeError | ValueError | None = None
        for entry in reversed(self._entries.copy()):
            try:
                self._remove_target(entry.target)
                self._entries.remove(entry)
                self._remove_empty_directories(entry.run_directory)
            except (OSError, RuntimeError, ValueError) as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        """Create or validate one owner-only non-symlink directory."""
        if path.is_symlink():
            raise ValueError("managed deletion directory must not be a symlink")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path_stat = path.stat()
        if not stat.S_ISDIR(path_stat.st_mode):
            raise ValueError("managed deletion path must be a directory")
        if path_stat.st_uid != os.geteuid():
            raise ValueError("managed deletion directory must be owned by this user")
        if stat.S_IMODE(path_stat.st_mode) != 0o700:
            os.chmod(path, 0o700)

    @staticmethod
    def _remove_target(target: Path) -> None:
        """Remove one non-symlink target without following directory links."""
        if target.is_symlink():
            raise ValueError("managed deletion target must not be a symlink")
        if not target.exists():
            return
        if target.is_dir():
            if not shutil.rmtree.avoids_symlink_attacks:
                raise RuntimeError("safe descriptor-based directory deletion is unavailable")
            shutil.rmtree(target)
            return
        target.unlink()

    @staticmethod
    def _remove_empty_directories(run_directory: Path) -> None:
        """Remove empty per-run and shared quarantine directories."""
        try:
            run_directory.rmdir()
        except OSError:
            return
        try:
            run_directory.parent.rmdir()
        except OSError:
            return
