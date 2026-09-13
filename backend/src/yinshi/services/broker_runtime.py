"""Broker-owned runtime sockets for qualified executor operations."""

from __future__ import annotations

import os
import re
import socket
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

_OPERATION_PATTERN = re.compile(r"^[0-9a-f]{32,64}$")
_SESSION_SOCKET_NAME = "session.sock"
_LISTEN_BACKLOG = 8


class BrokerRuntimeError(RuntimeError):
    """Report an unverifiable broker runtime transition."""


@dataclass(frozen=True, slots=True)
class BrokerRuntimeLayout:
    runtime_root: Path


@dataclass(slots=True)
class BrokerRuntimeSession:
    operation_id: str
    operation_directory: Path
    socket_path: Path
    listener: socket.socket


class BrokerRuntime:
    """Create private runtime sockets and retain their listening descriptors."""

    def __init__(
        self,
        *,
        layout: BrokerRuntimeLayout,
        broker_uid: int | None = None,
        broker_gid: int | None = None,
    ) -> None:
        if not layout.runtime_root.is_absolute():
            raise ValueError("broker runtime root must be absolute")
        self._layout = layout
        self._broker_uid = os.geteuid() if broker_uid is None else broker_uid
        self._broker_gid = os.getegid() if broker_gid is None else broker_gid
        self._sessions: dict[str, BrokerRuntimeSession] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _mode(metadata: os.stat_result) -> int:
        return stat.S_IMODE(metadata.st_mode)

    def _open_runtime_root(self) -> int:
        components = self._layout.runtime_root.parts
        if not components or components[0] != os.sep:
            raise BrokerRuntimeError("broker runtime root is invalid")
        descriptor = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for index, component in enumerate(components[1:], start=1):
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
                metadata = os.fstat(descriptor)
                final = index == len(components) - 1
                if not stat.S_ISDIR(metadata.st_mode):
                    raise BrokerRuntimeError("broker runtime root is invalid")
                if final:
                    if (
                        metadata.st_uid != self._broker_uid
                        or metadata.st_gid != self._broker_gid
                        or self._mode(metadata) != 0o711
                    ):
                        raise BrokerRuntimeError("broker runtime root is invalid")
                elif metadata.st_uid not in {0, self._broker_uid} or self._mode(metadata) & 0o022:
                    raise BrokerRuntimeError("broker runtime ancestor is invalid")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def setup(self, operation_id: str) -> BrokerRuntimeSession:
        """Create one new private listener from a fixed operation identity."""
        if not isinstance(operation_id, str) or _OPERATION_PATTERN.fullmatch(operation_id) is None:
            raise BrokerRuntimeError("broker runtime operation is invalid")
        with self._lock:
            if operation_id in self._sessions:
                raise BrokerRuntimeError("broker runtime operation is already active")
            root = self._open_runtime_root()
            listener: socket.socket | None = None
            operation = -1
            try:
                os.mkdir(operation_id, 0o700, dir_fd=root)
                os.fsync(root)
                operation = os.open(
                    operation_id,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=root,
                )
                operation_metadata = os.fstat(operation)
                if (
                    operation_metadata.st_uid != self._broker_uid
                    or operation_metadata.st_gid != self._broker_gid
                    or self._mode(operation_metadata) != 0o700
                    or os.listdir(operation)
                ):
                    raise BrokerRuntimeError("broker runtime operation is invalid")
                operation_directory = self._layout.runtime_root / operation_id
                socket_path = operation_directory / _SESSION_SOCKET_NAME
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                descriptor_root = Path("/proc/self/fd")
                bind_path = (
                    descriptor_root / str(operation) / _SESSION_SOCKET_NAME
                    if descriptor_root.is_dir()
                    else socket_path
                )
                listener.bind(str(bind_path))
                os.chmod(
                    _SESSION_SOCKET_NAME,
                    0o600,
                    dir_fd=operation,
                    follow_symlinks=False,
                )
                listener.listen(_LISTEN_BACKLOG)
                socket_metadata = os.stat(
                    _SESSION_SOCKET_NAME,
                    dir_fd=operation,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISSOCK(socket_metadata.st_mode)
                    or socket_metadata.st_uid != self._broker_uid
                    or socket_metadata.st_gid != self._broker_gid
                    or socket_metadata.st_nlink != 1
                    or self._mode(socket_metadata) != 0o600
                ):
                    raise BrokerRuntimeError("broker runtime socket is invalid")
                os.fsync(operation)
                session = BrokerRuntimeSession(
                    operation_id=operation_id,
                    operation_directory=operation_directory,
                    socket_path=socket_path,
                    listener=listener,
                )
                self._sessions[operation_id] = session
                listener = None
                return session
            except BrokerRuntimeError:
                raise
            except (OSError, ValueError) as exc:
                raise BrokerRuntimeError("broker runtime setup is unresolved") from exc
            finally:
                if listener is not None:
                    listener.close()
                if operation >= 0:
                    os.close(operation)
                os.close(root)

    def session(self, operation_id: str) -> BrokerRuntimeSession | None:
        with self._lock:
            return self._sessions.get(operation_id)

    def close(self, operation_id: str) -> None:
        """Close one retained listener without removing uncertain path state."""
        with self._lock:
            session = self._sessions.pop(operation_id, None)
        if session is not None:
            session.listener.close()
