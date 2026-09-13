"""Broker-owned bounded storage for authenticated replica artifact uploads."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from yinshi.services.broker_protocol import canonical_json
from yinshi.services.broker_replica_journal import ArtifactReference, IngestReceipt
from yinshi.services.replica_artifact_contract import (
    REPLICA_ARTIFACT_FILENAMES,
    REPLICA_OPERATION_PATTERN,
    validate_distinct_artifact_ids,
)
from yinshi.services.workspace_publication import (
    WorkspacePublicationCollisionError,
    WorkspacePublicationError,
    atomic_rename_no_replace,
    require_atomic_no_replace_support,
)

_T = TypeVar("_T")
_OPERATION_PATTERN = REPLICA_OPERATION_PATTERN
_NAMES = REPLICA_ARTIFACT_FILENAMES


class BrokerArtifactStoreRejectedError(Exception):
    """The upload or stored artifact set is conclusively invalid."""


class BrokerArtifactStoreCollisionError(BrokerArtifactStoreRejectedError):
    """A different state already owns the requested operation name."""


class BrokerArtifactStoreUnresolvedError(Exception):
    """The durable upload outcome cannot be established safely."""


@dataclass(frozen=True, slots=True)
class BrokerArtifactLimits:
    max_bundle_bytes: int = 512 * 1024 * 1024
    max_worktree_bytes: int = 512 * 1024 * 1024
    max_index_objects_bytes: int = 512 * 1024 * 1024
    max_set_bytes: int = 1024 * 1024 * 1024
    max_root_entries: int = 4096
    chunk_bytes: int = 64 * 1024
    idle_timeout_seconds: float = 30.0
    transfer_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        for name in (
            "max_bundle_bytes",
            "max_worktree_bytes",
            "max_index_objects_bytes",
            "max_set_bytes",
            "max_root_entries",
            "chunk_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name, value in (
            ("idle", self.idle_timeout_seconds),
            ("transfer", self.transfer_timeout_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"artifact {name} timeout is invalid")
        if self.transfer_timeout_seconds < self.idle_timeout_seconds:
            raise ValueError("artifact transfer timeout is shorter than idle timeout")


@dataclass(frozen=True, slots=True)
class ReplicaArtifactManifest:
    operation_id: str
    bundle: ArtifactReference
    worktree: ArtifactReference
    index_objects: ArtifactReference

    def __post_init__(self) -> None:
        if type(self.operation_id) is not str or not _OPERATION_PATTERN.fullmatch(
            self.operation_id
        ):
            raise ValueError("artifact operation ID is invalid")
        references = (self.bundle, self.worktree, self.index_objects)
        if any(type(reference) is not ArtifactReference for reference in references):
            raise TypeError("artifact manifest reference is invalid")
        validate_distinct_artifact_ids(tuple(reference.artifact_id for reference in references))


@dataclass(frozen=True, slots=True)
class ArtifactReconciliationResult:
    entry_name: str
    operation_id: str | None
    state: str

    def __post_init__(self) -> None:
        if type(self.entry_name) is not str or not self.entry_name:
            raise ValueError("reconciliation entry name is invalid")
        if self.operation_id is not None and not _OPERATION_PATTERN.fullmatch(self.operation_id):
            raise ValueError("reconciliation operation ID is invalid")
        if self.state not in {
            "incoming_verified",
            "unverified_final_present",
            "unjournaled_present",
            "pending_quarantined",
            "conflict_quarantined",
            "retained_foreign",
            "unresolved",
        }:
            raise ValueError("artifact reconciliation state is invalid")


@dataclass(frozen=True, slots=True)
class OpenedArtifact:
    descriptor: int
    reference: ArtifactReference
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class OpenedReplicaArtifacts:
    operation_id: str
    root_descriptor: int
    directory_descriptor: int
    root_identity: str
    directory_identity: str
    bundle: OpenedArtifact
    worktree: OpenedArtifact
    index_objects: OpenedArtifact


@dataclass(frozen=True, slots=True)
class _WrittenArtifact:
    device: int
    inode: int


def _identity(value: os.stat_result) -> str:
    return json.dumps(
        {"device": value.st_dev, "inode": value.st_ino},
        sort_keys=True,
        separators=(",", ":"),
    )


def _matches_identity(value: os.stat_result, expected: str) -> bool:
    return _identity(value) == expected


def _has_fixed_entries(directory: int) -> bool:
    expected = frozenset(_NAMES.values())
    seen: set[str] = set()
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name not in expected or entry.name in seen or len(seen) == len(expected):
                return False
            seen.add(entry.name)
    return seen == expected


def _receipt(manifest: ReplicaArtifactManifest) -> IngestReceipt:
    value = {
        "bundle": {
            "artifact_id": manifest.bundle.artifact_id,
            "byte_length": manifest.bundle.byte_length,
            "sha256": manifest.bundle.sha256,
        },
        "index_objects": {
            "artifact_id": manifest.index_objects.artifact_id,
            "byte_length": manifest.index_objects.byte_length,
            "sha256": manifest.index_objects.sha256,
        },
        "operation_id": manifest.operation_id,
        "worktree": {
            "artifact_id": manifest.worktree.artifact_id,
            "byte_length": manifest.worktree.byte_length,
            "sha256": manifest.worktree.sha256,
        },
    }
    receipt_id = (
        "ingest_"
        + hashlib.sha256(b"yinshi-broker-artifact-ingest-v1\0" + canonical_json(value)).hexdigest()
    )
    return IngestReceipt(
        receipt_id=receipt_id,
        bundle=manifest.bundle,
        worktree=manifest.worktree,
        index_objects=manifest.index_objects,
    )


class BrokerArtifactStore:
    """Receive fixed-order artifact bytes into a private immutable operation directory."""

    def __init__(
        self,
        root: str | Path,
        *,
        limits: BrokerArtifactLimits,
        expected_uid: int,
        expected_gid: int,
    ) -> None:
        if type(limits) is not BrokerArtifactLimits:
            raise TypeError("broker artifact limits are invalid")
        if type(expected_uid) is not int or type(expected_gid) is not int:
            raise TypeError("broker artifact owner is invalid")
        if expected_uid != os.geteuid() or expected_gid != os.getegid():
            raise BrokerArtifactStoreRejectedError("artifact store process owner is invalid")
        self._root = Path(root)
        self._limits = limits
        self._expected_uid = expected_uid
        self._expected_gid = expected_gid
        self._reservation_lock = asyncio.Lock()
        descriptor, _root_identity = self._open_root()
        os.close(descriptor)

    def _open_root(self) -> tuple[int, str]:
        try:
            named = os.lstat(self._root)
            descriptor = os.open(
                self._root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as error:
            raise BrokerArtifactStoreRejectedError("artifact store root is unavailable") from error
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
                or opened.st_uid != self._expected_uid
                or opened.st_gid != self._expected_gid
                or stat.S_IMODE(opened.st_mode) != 0o700
            ):
                raise BrokerArtifactStoreRejectedError("artifact store root is not private")
            return descriptor, _identity(opened)
        except BaseException:
            os.close(descriptor)
            raise

    def _validate_manifest(self, manifest: object) -> ReplicaArtifactManifest:
        if type(manifest) is not ReplicaArtifactManifest:
            raise BrokerArtifactStoreRejectedError("artifact manifest is invalid")
        maxima = (
            self._limits.max_bundle_bytes,
            self._limits.max_worktree_bytes,
            self._limits.max_index_objects_bytes,
        )
        references = (manifest.bundle, manifest.worktree, manifest.index_objects)
        if any(reference.byte_length > maximum for reference, maximum in zip(references, maxima)):
            raise BrokerArtifactStoreRejectedError("artifact role limit is exceeded")
        if sum(reference.byte_length for reference in references) > self._limits.max_set_bytes:
            raise BrokerArtifactStoreRejectedError("artifact set limit is exceeded")
        return manifest

    @staticmethod
    async def _run_blocking(
        function: Callable[..., _T],
        *arguments: object,
        **keyword_arguments: object,
    ) -> _T:
        task = asyncio.create_task(asyncio.to_thread(function, *arguments, **keyword_arguments))
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = error
        result = task.result()
        if cancellation is not None:
            raise cancellation
        return result

    async def _read_chunk(self, reader: asyncio.StreamReader, count: int) -> bytes:
        parts: list[bytes] = []
        remaining = count
        while remaining:
            try:
                async with asyncio.timeout(float(self._limits.idle_timeout_seconds)):
                    content = await reader.read(remaining)
            except TimeoutError as error:
                raise BrokerArtifactStoreUnresolvedError(
                    "artifact transfer timeout is unresolved"
                ) from error
            if not content:
                raise BrokerArtifactStoreRejectedError("artifact transfer is truncated")
            parts.append(content)
            remaining -= len(content)
        return b"".join(parts)

    async def _read_trailer(self, reader: asyncio.StreamReader) -> None:
        try:
            async with asyncio.timeout(float(self._limits.idle_timeout_seconds)):
                trailing = await reader.read(1)
        except TimeoutError as error:
            raise BrokerArtifactStoreUnresolvedError(
                "artifact transfer EOF timeout is unresolved"
            ) from error
        if trailing:
            raise BrokerArtifactStoreRejectedError("artifact transfer has trailing bytes")

    @staticmethod
    def _write_all(descriptor: int, content: bytes) -> None:
        view = memoryview(content)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("artifact write made no progress")
            written += count

    async def _receive_one(
        self,
        reader: asyncio.StreamReader,
        reference: ArtifactReference,
        descriptor: int | None,
    ) -> None:
        digest = hashlib.sha256()
        remaining = reference.byte_length
        while remaining:
            count = min(remaining, self._limits.chunk_bytes)
            content = await self._read_chunk(reader, count)
            digest.update(content)
            if descriptor is not None:
                await self._run_blocking(self._write_all, descriptor, content)
            remaining -= len(content)
        if digest.hexdigest() != reference.sha256:
            raise BrokerArtifactStoreRejectedError("artifact digest differs from declaration")

    def _create_file(self, parent: int, name: str) -> int:
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent,
        )
        try:
            os.fchmod(descriptor, 0o600)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _check_written(self, descriptor: int, reference: ArtifactReference) -> _WrittenArtifact:
        value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != self._expected_uid
            or value.st_gid != self._expected_gid
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_nlink != 1
            or value.st_size != reference.byte_length
        ):
            raise BrokerArtifactStoreRejectedError("written artifact metadata is invalid")
        return _WrittenArtifact(value.st_dev, value.st_ino)

    async def _receive_existing(
        self,
        manifest: ReplicaArtifactManifest,
        reader: asyncio.StreamReader,
    ) -> IngestReceipt:
        try:
            receipt = await self.inspect_incoming_async(manifest)
        except BrokerArtifactStoreRejectedError as error:
            raise BrokerArtifactStoreCollisionError(
                "artifact operation target already contains different state"
            ) from error
        for reference in (manifest.bundle, manifest.worktree, manifest.index_objects):
            await self._receive_one(reader, reference, None)
        await self._read_trailer(reader)
        if await self.inspect_incoming_async(manifest) != receipt:
            raise BrokerArtifactStoreRejectedError("stored artifact receipt changed")
        return receipt

    def _quarantine_pending_sync(
        self,
        root_identity: str,
        pending_name: str,
        pending_identity: str,
    ) -> str:
        for _attempt in range(16):
            operation_id = pending_name.split(".", 3)[1]
            attempt_id = pending_name.rsplit(".", 1)[-1]
            abandoned_name = (
                f".{operation_id}.pending.abandoned.{attempt_id}.{os.urandom(16).hex()}"
            )
            try:
                atomic_rename_no_replace(
                    self._root / pending_name,
                    self._root / abandoned_name,
                    source_parent_identity=root_identity,
                    target_parent_identity=root_identity,
                    source_identity=pending_identity,
                )
            except WorkspacePublicationCollisionError:
                continue
            except (WorkspacePublicationError, OSError) as error:
                raise BrokerArtifactStoreUnresolvedError(
                    "artifact attempt quarantine is unresolved"
                ) from error
            return abandoned_name
        raise BrokerArtifactStoreUnresolvedError(
            "artifact attempt quarantine name allocation is unresolved"
        )

    async def _quarantine_pending(
        self,
        root_identity: str,
        pending_name: str,
        pending_identity: str,
    ) -> str:
        return await self._run_blocking(
            self._quarantine_pending_sync,
            root_identity,
            pending_name,
            pending_identity,
        )

    @asynccontextmanager
    async def _root_reservation(self, root: int) -> AsyncIterator[None]:
        async with self._reservation_lock:
            while True:
                try:
                    fcntl.flock(root, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    await asyncio.sleep(0.01)
                    continue
                break
            try:
                yield
            finally:
                fcntl.flock(root, fcntl.LOCK_UN)

    def _root_entry_count(self, root: int) -> int:
        count = 0
        with os.scandir(root) as entries:
            for _entry in entries:
                count += 1
                if count >= self._limits.max_root_entries:
                    return count
        return count

    async def receive(
        self,
        manifest: ReplicaArtifactManifest,
        reader: asyncio.StreamReader,
    ) -> IngestReceipt:
        """Receive one bounded fixed-order set within one absolute deadline."""
        try:
            async with asyncio.timeout(float(self._limits.transfer_timeout_seconds)):
                return await self._receive_with_cleanup(manifest, reader)
        except TimeoutError as error:
            raise BrokerArtifactStoreUnresolvedError(
                "artifact transfer deadline is unresolved"
            ) from error

    async def _receive_with_cleanup(
        self,
        manifest: ReplicaArtifactManifest,
        reader: asyncio.StreamReader,
    ) -> IngestReceipt:
        manifest = self._validate_manifest(manifest)
        if not isinstance(reader, asyncio.StreamReader):
            raise BrokerArtifactStoreRejectedError("artifact reader is invalid")
        try:
            require_atomic_no_replace_support()
        except WorkspacePublicationError as error:
            raise BrokerArtifactStoreRejectedError(
                "atomic artifact publication is unavailable"
            ) from error
        root, root_identity = self._open_root()
        pending_name = ""
        pending_identity: str | None = None
        attempt_state = "absent"

        async def quarantine_live_attempt() -> None:
            nonlocal attempt_state
            if attempt_state != "live" or pending_identity is None:
                return
            await self._quarantine_pending(
                root_identity,
                pending_name,
                pending_identity,
            )
            attempt_state = "quarantined"

        try:
            existing_final = False
            async with self._root_reservation(root):
                try:
                    os.stat(manifest.operation_id, dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise BrokerArtifactStoreUnresolvedError(
                        "artifact target identity is unresolved"
                    ) from error
                else:
                    existing_final = True
                if not existing_final:
                    if self._root_entry_count(root) >= self._limits.max_root_entries:
                        raise BrokerArtifactStoreRejectedError(
                            "artifact store entry capacity is exhausted"
                        )
                    for _attempt in range(16):
                        pending_name = f".{manifest.operation_id}.pending.{os.urandom(16).hex()}"
                        try:
                            os.mkdir(pending_name, mode=0o700, dir_fd=root)
                        except FileExistsError:
                            continue
                        attempt_state = "live"
                        break
                    else:
                        raise BrokerArtifactStoreUnresolvedError(
                            "artifact attempt name allocation is unresolved"
                        )
            if existing_final:
                return await self._receive_existing(manifest, reader)
            pending_named = os.stat(
                pending_name,
                dir_fd=root,
                follow_symlinks=False,
            )
            pending_identity = _identity(pending_named)
            directory = os.open(
                pending_name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root,
            )
            try:
                pending_value = os.fstat(directory)
                if (
                    not stat.S_ISDIR(pending_value.st_mode)
                    or not _matches_identity(pending_value, pending_identity)
                    or pending_value.st_uid != self._expected_uid
                    or pending_value.st_gid != self._expected_gid
                    or stat.S_IMODE(pending_value.st_mode) != 0o700
                ):
                    raise BrokerArtifactStoreRejectedError("artifact attempt directory is invalid")
                for role, reference in (
                    ("committed_bundle", manifest.bundle),
                    ("worktree", manifest.worktree),
                    ("index_objects", manifest.index_objects),
                ):
                    descriptor = self._create_file(directory, _NAMES[role])
                    try:
                        await self._receive_one(reader, reference, descriptor)
                        await self._run_blocking(os.fsync, descriptor)
                        self._check_written(descriptor, reference)
                    finally:
                        os.close(descriptor)
                await self._read_trailer(reader)
                await self._run_blocking(os.fsync, directory)
            finally:
                os.close(directory)
            await self._run_blocking(
                self._inspect_named_set_closed,
                root,
                root_identity,
                pending_name,
                manifest,
                pending_identity,
            )
            try:
                await self._run_blocking(
                    atomic_rename_no_replace,
                    self._root / pending_name,
                    self._root / manifest.operation_id,
                    source_parent_identity=root_identity,
                    target_parent_identity=root_identity,
                    source_identity=pending_identity,
                )
            except WorkspacePublicationCollisionError as error:
                raise BrokerArtifactStoreCollisionError(
                    "artifact operation target already exists"
                ) from error
            except (WorkspacePublicationError, OSError) as error:
                raise BrokerArtifactStoreUnresolvedError(
                    "artifact publication outcome is unresolved"
                ) from error
            attempt_state = "published"
            try:
                return await self.inspect_incoming_async(manifest)
            except asyncio.CancelledError as error:
                raise BrokerArtifactStoreUnresolvedError(
                    "artifact publication acknowledgment is unresolved"
                ) from error
            except Exception as error:
                raise BrokerArtifactStoreUnresolvedError(
                    "published artifact inspection is unresolved"
                ) from error
        except asyncio.CancelledError:
            await quarantine_live_attempt()
            if attempt_state == "published":
                raise BrokerArtifactStoreUnresolvedError(
                    "artifact publication cancellation is unresolved"
                )
            raise
        except BrokerArtifactStoreCollisionError:
            await quarantine_live_attempt()
            raise
        except BrokerArtifactStoreRejectedError:
            if attempt_state == "published":
                raise BrokerArtifactStoreUnresolvedError("published artifact outcome is unresolved")
            await quarantine_live_attempt()
            raise
        except BrokerArtifactStoreUnresolvedError:
            await quarantine_live_attempt()
            raise
        except OSError as error:
            await quarantine_live_attempt()
            raise BrokerArtifactStoreUnresolvedError(
                "artifact storage or transport outcome is unresolved"
            ) from error
        except Exception as error:
            await quarantine_live_attempt()
            raise BrokerArtifactStoreUnresolvedError(
                "artifact ingestion outcome is unresolved"
            ) from error
        finally:
            os.close(root)

    def _open_artifact(
        self,
        directory: int,
        name: str,
        reference: ArtifactReference,
    ) -> OpenedArtifact:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory,
            )
        except OSError as error:
            raise BrokerArtifactStoreRejectedError("stored artifact is unavailable") from error
        try:
            value = os.fstat(descriptor)
            if (
                not stat.S_ISREG(value.st_mode)
                or value.st_uid != self._expected_uid
                or value.st_gid != self._expected_gid
                or stat.S_IMODE(value.st_mode) != 0o600
                or value.st_nlink != 1
                or value.st_size != reference.byte_length
            ):
                raise BrokerArtifactStoreRejectedError("stored artifact metadata is invalid")
            named = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (value.st_dev, value.st_ino):
                raise BrokerArtifactStoreRejectedError("stored artifact identity changed")
            digest = hashlib.sha256()
            offset = 0
            while offset < reference.byte_length:
                content = os.pread(
                    descriptor,
                    min(self._limits.chunk_bytes, reference.byte_length - offset),
                    offset,
                )
                if not content:
                    raise BrokerArtifactStoreRejectedError("stored artifact is truncated")
                digest.update(content)
                offset += len(content)
            if digest.hexdigest() != reference.sha256:
                raise BrokerArtifactStoreRejectedError("stored artifact digest differs")
            return OpenedArtifact(descriptor, reference, value.st_dev, value.st_ino)
        except BaseException:
            os.close(descriptor)
            raise

    def _recheck_artifact(
        self,
        directory: int,
        name: str,
        artifact: OpenedArtifact,
    ) -> None:
        opened = os.fstat(artifact.descriptor)
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (
            (opened.st_dev, opened.st_ino) != (artifact.device, artifact.inode)
            or (named.st_dev, named.st_ino) != (artifact.device, artifact.inode)
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or opened.st_uid != self._expected_uid
            or opened.st_gid != self._expected_gid
            or named.st_uid != self._expected_uid
            or named.st_gid != self._expected_gid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or stat.S_IMODE(named.st_mode) != 0o600
            or opened.st_nlink != 1
            or named.st_nlink != 1
            or opened.st_size != artifact.reference.byte_length
            or named.st_size != artifact.reference.byte_length
        ):
            raise BrokerArtifactStoreRejectedError("stored artifact changed")
        digest = hashlib.sha256()
        offset = 0
        while offset < artifact.reference.byte_length:
            content = os.pread(
                artifact.descriptor,
                min(
                    self._limits.chunk_bytes,
                    artifact.reference.byte_length - offset,
                ),
                offset,
            )
            if not content:
                raise BrokerArtifactStoreRejectedError("stored artifact is truncated")
            digest.update(content)
            offset += len(content)
        if digest.hexdigest() != artifact.reference.sha256:
            raise BrokerArtifactStoreRejectedError("stored artifact digest differs")

    def _inspect_named_set(
        self,
        root: int,
        root_identity: str,
        name: str,
        manifest: ReplicaArtifactManifest,
        expected_identity: str | None = None,
    ) -> tuple[int, tuple[OpenedArtifact, OpenedArtifact, OpenedArtifact]]:
        try:
            directory = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root,
            )
        except OSError as error:
            raise BrokerArtifactStoreRejectedError("stored artifact set is unavailable") from error
        opened: list[OpenedArtifact] = []
        try:
            value = os.fstat(directory)
            if (
                not stat.S_ISDIR(value.st_mode)
                or value.st_uid != self._expected_uid
                or value.st_gid != self._expected_gid
                or stat.S_IMODE(value.st_mode) != 0o700
                or (
                    expected_identity is not None
                    and not _matches_identity(value, expected_identity)
                )
                or not _has_fixed_entries(directory)
            ):
                raise BrokerArtifactStoreRejectedError("stored artifact set metadata is invalid")
            for role, reference in (
                ("committed_bundle", manifest.bundle),
                ("worktree", manifest.worktree),
                ("index_objects", manifest.index_objects),
            ):
                opened.append(self._open_artifact(directory, _NAMES[role], reference))
            named = os.stat(name, dir_fd=root, follow_symlinks=False)
            current = os.fstat(directory)
            named_root = os.lstat(self._root)
            current_root = os.fstat(root)
            if (
                (named.st_dev, named.st_ino) != (current.st_dev, current.st_ino)
                or not stat.S_ISDIR(named.st_mode)
                or not stat.S_ISDIR(current.st_mode)
                or named.st_uid != self._expected_uid
                or named.st_gid != self._expected_gid
                or current.st_uid != self._expected_uid
                or current.st_gid != self._expected_gid
                or stat.S_IMODE(named.st_mode) != 0o700
                or stat.S_IMODE(current.st_mode) != 0o700
                or not _has_fixed_entries(directory)
                or not _matches_identity(named_root, root_identity)
                or not _matches_identity(current_root, root_identity)
                or not stat.S_ISDIR(named_root.st_mode)
                or not stat.S_ISDIR(current_root.st_mode)
                or named_root.st_uid != self._expected_uid
                or named_root.st_gid != self._expected_gid
                or current_root.st_uid != self._expected_uid
                or current_root.st_gid != self._expected_gid
                or stat.S_IMODE(named_root.st_mode) != 0o700
                or stat.S_IMODE(current_root.st_mode) != 0o700
            ):
                raise BrokerArtifactStoreRejectedError("stored artifact set changed")
            for role, artifact in zip(_NAMES, opened):
                self._recheck_artifact(directory, _NAMES[role], artifact)
            return directory, (opened[0], opened[1], opened[2])
        except BaseException:
            for artifact in opened:
                os.close(artifact.descriptor)
            os.close(directory)
            raise

    def _inspect_named_set_closed(
        self,
        root: int,
        root_identity: str,
        name: str,
        manifest: ReplicaArtifactManifest,
        expected_identity: str | None = None,
    ) -> None:
        directory, opened = self._inspect_named_set(
            root,
            root_identity,
            name,
            manifest,
            expected_identity,
        )
        try:
            for artifact in opened:
                os.close(artifact.descriptor)
        finally:
            os.close(directory)

    def reconcile_attempts(
        self,
        journal_manifests: Mapping[str, ReplicaArtifactManifest],
    ) -> tuple[ArtifactReconciliationResult, ...]:
        """Quarantine bounded live attempts and verify known final sets."""
        if (
            not isinstance(journal_manifests, Mapping)
            or len(journal_manifests) > self._limits.max_root_entries
            or any(
                type(operation) is not str
                or not _OPERATION_PATTERN.fullmatch(operation)
                or type(manifest) is not ReplicaArtifactManifest
                or manifest.operation_id != operation
                for operation, manifest in journal_manifests.items()
            )
        ):
            raise BrokerArtifactStoreRejectedError("journal manifest map is invalid")
        root, root_identity = self._open_root()
        try:
            names: list[str] = []
            with os.scandir(root) as entries:
                for entry in entries:
                    if len(names) >= self._limits.max_root_entries:
                        raise BrokerArtifactStoreUnresolvedError(
                            "artifact root entry limit is exceeded"
                        )
                    names.append(entry.name)
            names.sort()
            final_operations = {name for name in names if _OPERATION_PATTERN.fullmatch(name)}
            results: list[ArtifactReconciliationResult] = []
            for name in names:
                if _OPERATION_PATTERN.fullmatch(name):
                    known_manifest = journal_manifests.get(name)
                    if known_manifest is None:
                        final_state = "unjournaled_present"
                    else:
                        try:
                            self._inspect_named_set_closed(
                                root,
                                root_identity,
                                name,
                                known_manifest,
                            )
                        except BrokerArtifactStoreRejectedError:
                            final_state = "unverified_final_present"
                        except (BrokerArtifactStoreUnresolvedError, OSError):
                            final_state = "unresolved"
                        else:
                            final_state = "incoming_verified"
                    results.append(
                        ArtifactReconciliationResult(
                            entry_name=name,
                            operation_id=name,
                            state=final_state,
                        )
                    )
                    continue
                pending_match = re.fullmatch(
                    r"\.([0-9a-f]{32})\.pending(?:\.[0-9a-f]{32})?",
                    name,
                )
                if pending_match is None:
                    results.append(
                        ArtifactReconciliationResult(
                            entry_name=name,
                            operation_id=None,
                            state="retained_foreign",
                        )
                    )
                    continue
                operation_id = pending_match.group(1)
                try:
                    value = os.stat(name, dir_fd=root, follow_symlinks=False)
                    if (
                        not stat.S_ISDIR(value.st_mode)
                        or value.st_uid != self._expected_uid
                        or value.st_gid != self._expected_gid
                        or stat.S_IMODE(value.st_mode) != 0o700
                    ):
                        raise OSError("pending artifact state is not owned")
                    self._quarantine_pending_sync(
                        root_identity,
                        name,
                        _identity(value),
                    )
                except (OSError, BrokerArtifactStoreUnresolvedError):
                    results.append(
                        ArtifactReconciliationResult(
                            entry_name=name,
                            operation_id=operation_id,
                            state="unresolved",
                        )
                    )
                    continue
                results.append(
                    ArtifactReconciliationResult(
                        entry_name=name,
                        operation_id=operation_id,
                        state=(
                            "conflict_quarantined"
                            if operation_id in final_operations
                            else "pending_quarantined"
                        ),
                    )
                )
            return tuple(results)
        finally:
            os.close(root)

    @contextmanager
    def open_incoming(
        self,
        manifest: ReplicaArtifactManifest,
    ) -> Iterator[OpenedReplicaArtifacts]:
        manifest = self._validate_manifest(manifest)
        root, root_identity = self._open_root()
        directory = -1
        opened: tuple[OpenedArtifact, OpenedArtifact, OpenedArtifact] | None = None
        try:
            directory, opened = self._inspect_named_set(
                root,
                root_identity,
                manifest.operation_id,
                manifest,
            )
            yield OpenedReplicaArtifacts(
                operation_id=manifest.operation_id,
                root_descriptor=root,
                directory_descriptor=directory,
                root_identity=root_identity,
                directory_identity=_identity(os.fstat(directory)),
                bundle=opened[0],
                worktree=opened[1],
                index_objects=opened[2],
            )
        finally:
            if opened is not None:
                for artifact in opened:
                    os.close(artifact.descriptor)
            if directory >= 0:
                os.close(directory)
            os.close(root)

    @asynccontextmanager
    async def open_incoming_async(
        self,
        manifest: ReplicaArtifactManifest,
    ) -> AsyncIterator[OpenedReplicaArtifacts]:
        """Open and hash incoming files without blocking the event loop."""
        manager = self.open_incoming(manifest)
        acquisition = asyncio.create_task(asyncio.to_thread(manager.__enter__))
        try:
            opened = await asyncio.shield(acquisition)
        except asyncio.CancelledError as cancellation:
            while True:
                try:
                    await asyncio.shield(acquisition)
                    break
                except asyncio.CancelledError:
                    continue
                except Exception as error:
                    raise cancellation from error
            await self._run_blocking(manager.__exit__, None, None, None)
            raise
        try:
            yield opened
        finally:
            await self._run_blocking(manager.__exit__, None, None, None)

    def recheck_opened(self, opened: OpenedReplicaArtifacts) -> None:
        """Recheck pinned incoming files after an external verification step."""
        if type(opened) is not OpenedReplicaArtifacts:
            raise BrokerArtifactStoreRejectedError("opened artifact set is invalid")
        root = os.fstat(opened.root_descriptor)
        named_root = os.lstat(self._root)
        directory = os.fstat(opened.directory_descriptor)
        named_directory = os.stat(
            opened.operation_id,
            dir_fd=opened.root_descriptor,
            follow_symlinks=False,
        )
        if (
            not _matches_identity(root, opened.root_identity)
            or not _matches_identity(named_root, opened.root_identity)
            or not _matches_identity(directory, opened.directory_identity)
            or not _matches_identity(named_directory, opened.directory_identity)
            or not stat.S_ISDIR(root.st_mode)
            or not stat.S_ISDIR(named_root.st_mode)
            or not stat.S_ISDIR(directory.st_mode)
            or not stat.S_ISDIR(named_directory.st_mode)
            or root.st_uid != self._expected_uid
            or root.st_gid != self._expected_gid
            or named_root.st_uid != self._expected_uid
            or named_root.st_gid != self._expected_gid
            or directory.st_uid != self._expected_uid
            or directory.st_gid != self._expected_gid
            or named_directory.st_uid != self._expected_uid
            or named_directory.st_gid != self._expected_gid
            or stat.S_IMODE(root.st_mode) != 0o700
            or stat.S_IMODE(named_root.st_mode) != 0o700
            or stat.S_IMODE(directory.st_mode) != 0o700
            or stat.S_IMODE(named_directory.st_mode) != 0o700
            or not _has_fixed_entries(opened.directory_descriptor)
        ):
            raise BrokerArtifactStoreRejectedError("opened artifact set changed")
        self._recheck_artifact(
            opened.directory_descriptor,
            _NAMES["committed_bundle"],
            opened.bundle,
        )
        self._recheck_artifact(
            opened.directory_descriptor,
            _NAMES["worktree"],
            opened.worktree,
        )
        self._recheck_artifact(
            opened.directory_descriptor,
            _NAMES["index_objects"],
            opened.index_objects,
        )

    async def recheck_opened_async(self, opened: OpenedReplicaArtifacts) -> None:
        """Recheck pinned incoming files without blocking the event loop."""
        await self._run_blocking(self.recheck_opened, opened)

    def inspect_incoming(self, manifest: ReplicaArtifactManifest) -> IngestReceipt:
        """Verify exact immutable storage and return its deterministic receipt."""
        with self.open_incoming(manifest):
            return _receipt(manifest)

    async def inspect_incoming_async(
        self,
        manifest: ReplicaArtifactManifest,
    ) -> IngestReceipt:
        """Inspect immutable storage without blocking the event loop."""
        return await self._run_blocking(self.inspect_incoming, manifest)
