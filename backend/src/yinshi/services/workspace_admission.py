"""Durable admission markers serialize work on a physical Git checkout.

Markers contain opaque authority hashes, not credentials. A live advisory lock
also distinguishes an executing legacy SSE request from a crashed owner. Closing
a lease retains its marker. Only explicit completion removes the exact owner.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from yinshi.exceptions import GitError
from yinshi.services.git import run_git_bytes
from yinshi.services.repository_lifecycle import repository_lifecycle
from yinshi.services.thread_git_ownership import (
    ThreadGitOwnershipError,
    _common_directory,
    _storage_node,
    _validate_storage_layout,
    _validate_workspace_binding,
)

_DIRECTORY = ".yinshi-workspace-admission-v1"
_DELETION_SUFFIX = ".deletion.json"
logger = logging.getLogger(__name__)


class WorkspaceAdmissionError(RuntimeError):
    """Admission is busy or its ownership cannot be established safely."""

    def __init__(self) -> None:
        super().__init__("Workspace is busy or its admission ownership is unavailable")


def _validate_file(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise WorkspaceAdmissionError()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 1024:
        raise WorkspaceAdmissionError()


def _open_directory(common: Path) -> int:
    root = common / _DIRECTORY
    _storage_node(common, directory=True)
    created = False
    try:
        root.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise WorkspaceAdmissionError()
        if created:
            parent = os.open(common, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_marker(directory: int, key: str) -> dict[str, object] | None:
    try:
        fd = os.open(key + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        _validate_file(fd)
        payload = json.loads(os.read(fd, 1025))
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "authority",
            "operation",
            "kind",
        }:
            raise WorkspaceAdmissionError()
        if type(payload["version"]) is not int or payload["version"] != 1:
            raise WorkspaceAdmissionError()
        if payload["kind"] not in ("integration", "prompt", "legacy"):
            raise WorkspaceAdmissionError()
        for name, length in (("authority", 64), ("operation", 32)):
            value = payload[name]
            if not isinstance(value, str) or re.fullmatch(f"[0-9a-f]{{{length}}}", value) is None:
                raise WorkspaceAdmissionError()
        return payload
    except (ValueError, TypeError) as error:
        raise WorkspaceAdmissionError() from error
    finally:
        os.close(fd)


def _publish_marker(
    directory: int, key: str, payload: dict[str, object], *, suffix: str = ".json"
) -> None:
    temporary = f"{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        remaining = memoryview(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
        )
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise WorkspaceAdmissionError()
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(
            temporary,
            key + suffix,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
    finally:
        os.unlink(temporary, dir_fd=directory)
    os.fsync(directory)


def _read_deletion_marker(directory: int, key: str) -> dict[str, object] | None:
    """Read the exact-owner physical deletion claim for one workspace."""
    try:
        fd = os.open(
            key + _DELETION_SUFFIX, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except FileNotFoundError:
        return None
    try:
        _validate_file(fd)
        payload = json.loads(os.read(fd, 1025))
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "authority",
            "operation",
            "kind",
        }:
            raise WorkspaceAdmissionError()
        if (
            type(payload["version"]) is not int
            or payload["version"] != 1
            or payload["kind"] != "deletion"
        ):
            raise WorkspaceAdmissionError()
        for name, length in (("authority", 64), ("operation", 32)):
            value = payload[name]
            if not isinstance(value, str) or re.fullmatch(f"[0-9a-f]{{{length}}}", value) is None:
                raise WorkspaceAdmissionError()
        return payload
    except (ValueError, TypeError) as error:
        raise WorkspaceAdmissionError() from error
    finally:
        os.close(fd)


def _publish_deletion(directory: int, key: str, owner: dict[str, object]) -> None:
    existing = _read_deletion_marker(directory, key)
    if existing == owner:
        # Re-publishing this exact claim is idempotent across crash retries.
        return
    if existing is not None:
        raise WorkspaceAdmissionError()
    _publish_marker(directory, key, owner, suffix=_DELETION_SUFFIX)


def _publish_deletion_opened(common: Path, key: str, owner: dict[str, object]) -> None:
    directory = _open_directory(common)
    try:
        _publish_deletion(directory, key, owner)
    finally:
        os.close(directory)


def _fsync_directory(path: Path) -> None:
    """Synchronize one already-validated directory without creating storage."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_deletion(directory: int, key: str, owner: dict[str, object]) -> None:
    existing = _read_deletion_marker(directory, key)
    if existing is None:
        # Confirmed absence inside the validated storage directory: an earlier
        # authorized release already unlinked this exact marker. Synchronize
        # the containing directory so the recovered absence outlives a power
        # loss before any terminal bookkeeping records the release.
        os.fsync(directory)
        return
    if existing != owner:
        raise WorkspaceAdmissionError()
    os.unlink(key + _DELETION_SUFFIX, dir_fd=directory)
    os.fsync(directory)


@dataclass
class WorkspaceAdmissionLease:
    """One live owner whose durable marker survives uncertain work or restart."""

    common: Path = field(repr=False)
    key: str
    owner: dict[str, object] = field(repr=False)
    descriptor: int = field(repr=False)

    def close(self) -> None:
        """Drop liveness without claiming that durable work completed."""
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def _release(self) -> None:
        if self.descriptor < 0:
            raise WorkspaceAdmissionError()
        directory = -1
        try:
            directory = _open_directory(self.common)
            existing = _read_marker(directory, self.key)
            if existing != self.owner:
                raise WorkspaceAdmissionError()
            os.unlink(self.key + ".json", dir_fd=directory)
            os.fsync(directory)
        finally:
            if directory >= 0:
                os.close(directory)
            self.close()

    async def release(self) -> None:
        """Remove only this exact marker after execution is known to have stopped."""
        async with repository_lifecycle("admission:" + self.key, self.common):
            attempt = asyncio.create_task(asyncio.to_thread(self._release))
            try:
                await asyncio.shield(attempt)
            except asyncio.CancelledError:
                while not attempt.done():
                    try:
                        await asyncio.shield(attempt)
                    except asyncio.CancelledError:
                        continue
                attempt.result()
                raise


def _claim(
    common: Path, key: str, owner: dict[str, object], *, resume: bool
) -> WorkspaceAdmissionLease:
    directory = _open_directory(common)
    live = -1
    try:
        if _read_deletion_marker(directory, key) is not None:
            raise WorkspaceAdmissionError()
        live = os.open(
            key + ".live",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        _validate_file(live)
        try:
            fcntl.flock(live, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WorkspaceAdmissionError() from error
        existing = _read_marker(directory, key)
        if existing is not None and (not resume or existing != owner):
            raise WorkspaceAdmissionError()
        if existing is None:
            _publish_marker(directory, key, owner)
        lease = WorkspaceAdmissionLease(common, key, owner, live)
        live = -1
        return lease
    except OSError as error:
        raise WorkspaceAdmissionError() from error
    finally:
        if live >= 0:
            os.close(live)
        os.close(directory)


async def _workspace_location(repo_path: str, workspace_path: str) -> tuple[Path, str, str]:
    common = await _common_directory(repo_path)
    await asyncio.to_thread(_storage_node, common, directory=True)
    await asyncio.to_thread(_validate_storage_layout, common)
    await _validate_workspace_binding(repo_path, common)
    if await _common_directory(workspace_path) != common:
        raise WorkspaceAdmissionError()
    await _validate_workspace_binding(workspace_path, common)
    raw = await run_git_bytes(["rev-parse", "--absolute-git-dir"], cwd=workspace_path)
    metadata = Path(os.fsdecode(raw.removesuffix(b"\n"))).resolve(strict=True)
    relative = str(metadata.relative_to(common))
    if relative != "." and (
        metadata.parent != common / "worktrees" or metadata.name in {".", ".."}
    ):
        raise WorkspaceAdmissionError()
    key = hashlib.sha256(relative.encode("utf-8")).hexdigest()
    workspace = await asyncio.to_thread(Path(workspace_path).resolve, strict=True)
    for name in ("HEAD", "ORIG_HEAD", "index", "logs/HEAD"):
        await asyncio.to_thread(_storage_node, metadata / name, directory=False)
    metadata_stat = await asyncio.to_thread(_storage_node, metadata, directory=True)
    workspace_stat = await asyncio.to_thread(_storage_node, workspace, directory=True)
    if metadata_stat is None or workspace_stat is None:
        raise WorkspaceAdmissionError()
    identity = json.dumps(
        {
            "common": str(common),
            "metadata": relative,
            "workspace": str(workspace),
            "metadata_device": metadata_stat.st_dev,
            "metadata_inode": metadata_stat.st_ino,
            "workspace_device": workspace_stat.st_dev,
            "workspace_inode": workspace_stat.st_ino,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return common, key, identity


async def read_workspace_identity(*, repo_path: str, workspace_path: str) -> str:
    """Capture stable Git metadata identity with validation facts, without opening a database."""
    try:
        return (await _workspace_location(repo_path, workspace_path))[2]
    except (ThreadGitOwnershipError, GitError, OSError, ValueError) as error:
        raise WorkspaceAdmissionError() from error


@dataclass(frozen=True)
class WorkspaceAdmissionSnapshot:
    owner: dict[str, object]
    live: bool
    physical_identity: str


def _read_snapshot(
    common: Path, key: str, authority: str, identity: str
) -> WorkspaceAdmissionSnapshot | None:
    if not os.path.lexists(common / _DIRECTORY):
        return None
    directory = _open_directory(common)
    live = -1
    try:
        owner = _read_marker(directory, key)
        if owner is None:
            return None
        if owner["authority"] != authority:
            raise WorkspaceAdmissionError()
        live = os.open(key + ".live", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        _validate_file(live)
        try:
            fcntl.flock(live, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return WorkspaceAdmissionSnapshot(owner, True, identity)
        fcntl.flock(live, fcntl.LOCK_UN)
        return WorkspaceAdmissionSnapshot(owner, False, identity)
    except OSError as error:
        raise WorkspaceAdmissionError() from error
    finally:
        if live >= 0:
            os.close(live)
        os.close(directory)


async def inspect_workspace_admission(
    *, repo_path: str, workspace_path: str, authority_hash: str
) -> WorkspaceAdmissionSnapshot | None:
    """Read only this authority's validated marker. Other authorities remain opaque."""
    common, key, identity = await _workspace_location(repo_path, workspace_path)
    async with repository_lifecycle("admission:" + key, common):
        if (await _workspace_location(repo_path, workspace_path))[2] != identity:
            raise WorkspaceAdmissionError()
        return await asyncio.to_thread(_read_snapshot, common, key, authority_hash, identity)


async def _claim_cancellation_safe(
    common: Path,
    key: str,
    owner: dict[str, object],
    *,
    resume: bool,
) -> WorkspaceAdmissionLease:
    """Drain a cancelled worker and close any lease it won after cancellation."""
    attempt = asyncio.create_task(asyncio.to_thread(_claim, common, key, owner, resume=resume))
    try:
        return await asyncio.shield(attempt)
    except asyncio.CancelledError:
        while not attempt.done():
            try:
                await asyncio.shield(attempt)
            except asyncio.CancelledError:
                continue
            except BaseException:  # noqa: BLE001
                break
        try:
            acquired = attempt.result()
        except BaseException:
            logger.warning(
                "Workspace admission failed while cancellation was pending", exc_info=True
            )
        else:
            acquired.close()
        raise


async def acquire_workspace_admission(
    *,
    repo_path: str,
    workspace_path: str,
    authority_hash: str,
    operation_id: str,
    kind: str,
    resume: bool = False,
    physical_identity: str | None = None,
) -> WorkspaceAdmissionLease:
    """Publish physical admission only after the caller's durable reservation."""
    if (
        re.fullmatch(r"[0-9a-f]{64}", authority_hash) is None
        or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None
    ):
        raise WorkspaceAdmissionError()
    if kind not in {"integration", "prompt", "legacy"}:
        raise WorkspaceAdmissionError()
    try:
        common, key, observed = await _workspace_location(repo_path, workspace_path)
        if physical_identity is not None and observed != physical_identity:
            raise WorkspaceAdmissionError()
        owner: dict[str, object] = {
            "version": 1,
            "authority": authority_hash,
            "operation": operation_id,
            "kind": kind,
        }
        async with repository_lifecycle("admission:" + key, common):
            if (await _workspace_location(repo_path, workspace_path))[2] != observed:
                raise WorkspaceAdmissionError()
            return await _claim_cancellation_safe(common, key, owner, resume=resume)
    except GitError as exc:
        raise WorkspaceAdmissionError() from exc


async def resume_workspace_admission_marker(
    *,
    common_directory: str,
    marker_key: str,
    authority_hash: str,
    operation_id: str,
    kind: str,
) -> WorkspaceAdmissionLease:
    """Resume an exact durable marker when private backlinks already name final paths."""
    if re.fullmatch(r"[0-9a-f]{64}", marker_key) is None:
        raise WorkspaceAdmissionError()
    if re.fullmatch(r"[0-9a-f]{64}", authority_hash) is None:
        raise WorkspaceAdmissionError()
    if re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
        raise WorkspaceAdmissionError()
    if kind not in {"integration", "prompt", "legacy"}:
        raise WorkspaceAdmissionError()
    common = Path(common_directory)
    await asyncio.to_thread(_storage_node, common, directory=True)
    await asyncio.to_thread(_validate_storage_layout, common)
    owner: dict[str, object] = {
        "version": 1,
        "authority": authority_hash,
        "operation": operation_id,
        "kind": kind,
    }
    async with repository_lifecycle("admission:" + marker_key, common):
        return await _claim_cancellation_safe(common, marker_key, owner, resume=True)


async def publish_workspace_deletion_marker(
    *,
    repo_path: str,
    workspace_path: str,
    authority_hash: str,
    operation_id: str,
) -> dict[str, str]:
    """Publish one exact-owner physical deletion claim for one workspace."""
    if (
        re.fullmatch(r"[0-9a-f]{64}", authority_hash) is None
        or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None
    ):
        raise WorkspaceAdmissionError()
    common, key, identity = await _workspace_location(repo_path, workspace_path)
    owner: dict[str, object] = {
        "version": 1,
        "authority": authority_hash,
        "operation": operation_id,
        "kind": "deletion",
    }
    async with repository_lifecycle("admission:" + key, common):
        if (await _workspace_location(repo_path, workspace_path))[2] != identity:
            raise WorkspaceAdmissionError()
        await asyncio.to_thread(_publish_deletion_opened, common, key, owner)
    return {"identity": identity, "directory": str(common / _DIRECTORY), "key": key}


def read_workspace_deletion_marker_sync(
    marker_directory: str, key: str
) -> dict[str, object] | None:
    """Read one validated deletion claim without requiring a live workspace."""
    if re.fullmatch(r"[0-9a-f]{64}", key) is None:
        raise WorkspaceAdmissionError()
    common = _validated_deletion_directory(marker_directory)
    directory = _open_directory(common.parent)
    try:
        return _read_deletion_marker(directory, key)
    finally:
        os.close(directory)


def remove_workspace_deletion_marker_sync(
    marker_directory: str, key: str, authority_hash: str, operation_id: str
) -> None:
    """Remove only this exact deletion claim after the workspace row is gone."""
    if (
        re.fullmatch(r"[0-9a-f]{64}", authority_hash) is None
        or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None
    ):
        raise WorkspaceAdmissionError()
    if re.fullmatch(r"[0-9a-f]{64}", key) is None:
        raise WorkspaceAdmissionError()
    owner: dict[str, object] = {
        "version": 1,
        "authority": authority_hash,
        "operation": operation_id,
        "kind": "deletion",
    }
    common = _validated_deletion_directory(marker_directory)
    if not os.path.lexists(common):
        # The whole validated admission directory is absent: no marker of any
        # owner can remain there, so this authorized release is complete once
        # the parent directory itself is synchronized.
        _fsync_directory(common.parent)
        return
    directory = _open_directory(common.parent)
    try:
        _remove_deletion(directory, key, owner)
    finally:
        os.close(directory)


def _validated_deletion_directory(marker_directory: str) -> Path:
    """Reject redirected or foreign marker directories before any marker IO."""
    if (
        not isinstance(marker_directory, str)
        or not os.path.isabs(marker_directory)
        or len(marker_directory) > 4096
    ):
        raise WorkspaceAdmissionError()
    common = Path(marker_directory)
    if common.name != _DIRECTORY:
        raise WorkspaceAdmissionError()
    _storage_node(common.parent, directory=True)
    return common
