"""Verify and atomically publish one bounded workspace replica artifact set."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Final, Literal

from yinshi.services.broker_artifact_store import (
    BrokerArtifactStoreRejectedError,
    BrokerArtifactStoreUnresolvedError,
    OpenedArtifact,
    OpenedReplicaArtifacts,
)
from yinshi.services.replica_artifact_contract import (
    REPLICA_ARTIFACT_FILENAMES,
    REPLICA_ARTIFACT_MEDIA_TYPES,
    compute_replica_artifact_set_sha256,
    compute_replica_limits_sha256,
    validate_distinct_artifact_ids,
    validate_replica_identifier,
    validate_replica_operation_id,
)
from yinshi.services.workspace_publication import (
    WorkspacePublicationCollisionError,
    WorkspacePublicationError,
    atomic_rename_no_replace,
    require_atomic_no_replace_support,
)
from yinshi.services.workspace_replica_artifact import (
    DEFAULT_ARTIFACT_LIMITS,
    ArtifactLimits,
    VerifiedWorktreeArtifact,
    decode_worktree_artifact,
)
from yinshi.services.workspace_replica_bundle import (
    DEFAULT_BUNDLE_LIMITS,
    BundleLimits,
    VerifiedCommittedBundle,
    _run_blocking,
    verify_committed_bundle,
    verify_committed_bundle_file,
)
from yinshi.services.workspace_replica_object_pack import (
    DEFAULT_INDEX_OBJECT_PACK_LIMITS,
    IndexObjectPackLimits,
    VerifiedIndexObjectPack,
    verify_index_object_pack,
    verify_index_object_pack_file,
)

_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}\Z")
_BUNDLE_FILE = REPLICA_ARTIFACT_FILENAMES["committed_bundle"]
_WORKTREE_FILE = REPLICA_ARTIFACT_FILENAMES["worktree"]
_INDEX_OBJECTS_FILE = REPLICA_ARTIFACT_FILENAMES["index_objects"]
_EXPECTED_FILES: Final[frozenset[str]] = frozenset(
    {"artifact-set.json", *REPLICA_ARTIFACT_FILENAMES.values()}
)
_MEDIA_TYPES = REPLICA_ARTIFACT_MEDIA_TYPES


def _has_exact_entries(directory: int, expected: frozenset[str]) -> bool:
    seen: set[str] = set()
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name not in expected or entry.name in seen or len(seen) == len(expected):
                return False
            seen.add(entry.name)
    return seen == expected


class ReplicaPublicationRejectedError(Exception):
    """Publication input or confirmed storage state is invalid."""


class ReplicaPublicationCollisionError(ReplicaPublicationRejectedError):
    """A pending or final operation name already exists."""


class ReplicaPublicationUnresolvedError(Exception):
    """Publication may be visible but durable completion is unknown."""


@dataclass(frozen=True)
class ReplicaArtifactBinding:
    """Signed identity, role, digest, and length for one artifact."""

    role: Literal["committed_bundle", "worktree", "index_objects"]
    media_type: str
    artifact_id: str
    sha256: str
    byte_length: int


@dataclass(frozen=True)
class ReplicaIdentity:
    """Broker-approved physical replica authority."""

    physical_target_id: str
    replica_generation: int
    execution_owner_id: str


@dataclass(frozen=True)
class ReplicaStoreLimits:
    """Declared bounds for one complete artifact set."""

    bundle: BundleLimits = field(default_factory=lambda: DEFAULT_BUNDLE_LIMITS)
    worktree: ArtifactLimits = field(default_factory=lambda: DEFAULT_ARTIFACT_LIMITS)
    index_objects: IndexObjectPackLimits = field(
        default_factory=lambda: DEFAULT_INDEX_OBJECT_PACK_LIMITS
    )
    max_set_bytes: int = 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            type(self.bundle) is not BundleLimits
            or type(self.worktree) is not ArtifactLimits
            or type(self.index_objects) is not IndexObjectPackLimits
        ):
            raise TypeError("replica store limits contain invalid nested limits")
        if type(self.max_set_bytes) is not int or self.max_set_bytes < 1:
            raise ValueError("max_set_bytes must be a positive integer")


DEFAULT_REPLICA_STORE_LIMITS: Final[ReplicaStoreLimits] = ReplicaStoreLimits(
    bundle=DEFAULT_BUNDLE_LIMITS,
    worktree=DEFAULT_ARTIFACT_LIMITS,
    index_objects=DEFAULT_INDEX_OBJECT_PACK_LIMITS,
)


@dataclass(frozen=True)
class ReplicaArtifactSetDeclaration:
    """Authenticated declaration for one immutable three-artifact set."""

    version: Literal[2]
    operation_id: str
    repository_id: str
    workspace_id: str
    identity: ReplicaIdentity
    object_format: Literal["sha1", "sha256"]
    source_state_sha256: str
    reconciliation_fingerprint: str
    bundle: ReplicaArtifactBinding
    worktree: ReplicaArtifactBinding
    index_objects: ReplicaArtifactBinding
    limits: ReplicaStoreLimits


@dataclass(frozen=True)
class ReplicaVerificationReceipt:
    """Immutable consumer verification result without artifact bytes."""

    verification_receipt_id: str
    artifact_set_sha256: str
    object_format: Literal["sha1", "sha256"]
    source_state_sha256: str
    reconciliation_fingerprint: str
    bundle: ReplicaArtifactBinding
    worktree: ReplicaArtifactBinding
    index_objects: ReplicaArtifactBinding
    bundle_refs_sha256: str
    bundle_inventory_sha256: str
    index_inventory_sha256: str


@dataclass(frozen=True)
class ReplicaInspectionReceipt:
    """Read-only verification of current storage without publication authority."""

    inspection_receipt_id: str
    artifact_set_sha256: str
    verification: ReplicaVerificationReceipt
    final_directory_identity: str
    publication_marker_sha256: str


@dataclass(frozen=True)
class ReplicaPublicationReceipt:
    """Durable publication result bound to authority and synchronization."""

    publication_receipt_id: str
    artifact_set_sha256: str
    verification: ReplicaVerificationReceipt
    identity: ReplicaIdentity
    source_state_sha256: str
    final_directory_identity: str
    publication_marker_sha256: str
    synchronization_receipt_id: str


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _domain_digest(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + b"\x00" + _canonical_json(value)).hexdigest()


def _validate_identifier(name: str, value: object) -> str:
    try:
        return validate_replica_identifier(value, name)
    except ValueError as error:
        raise ReplicaPublicationRejectedError(str(error)) from error


def _validate_sha256(name: str, value: object) -> str:
    if type(value) is not str or not _SHA256_PATTERN.fullmatch(value):
        raise ReplicaPublicationRejectedError(f"{name} is invalid")
    return value


def _validate_binding(
    binding: object,
    *,
    role: Literal["committed_bundle", "worktree", "index_objects"],
    maximum: int,
) -> ReplicaArtifactBinding:
    if type(binding) is not ReplicaArtifactBinding:
        raise ReplicaPublicationRejectedError(f"{role} binding is invalid")
    if (
        type(binding.role) is not str
        or type(binding.media_type) is not str
        or binding.role != role
        or binding.media_type != _MEDIA_TYPES[role]
    ):
        raise ReplicaPublicationRejectedError(f"{role} binding role is invalid")
    _validate_identifier(f"{role} artifact ID", binding.artifact_id)
    _validate_sha256(f"{role} digest", binding.sha256)
    if type(binding.byte_length) is not int or not 0 < binding.byte_length <= maximum:
        raise ReplicaPublicationRejectedError(f"{role} length is invalid")
    return binding


def _validate_declaration(
    declaration: object,
    ceilings: ReplicaStoreLimits,
) -> ReplicaArtifactSetDeclaration:
    if type(declaration) is not ReplicaArtifactSetDeclaration:
        raise ReplicaPublicationRejectedError("artifact set declaration is invalid")
    if type(declaration.version) is not int or declaration.version != 2:
        raise ReplicaPublicationRejectedError("artifact set version is unsupported")
    try:
        validate_replica_operation_id(declaration.operation_id)
    except ValueError as error:
        raise ReplicaPublicationRejectedError(str(error)) from error
    _validate_identifier("repository ID", declaration.repository_id)
    _validate_identifier("workspace ID", declaration.workspace_id)
    if type(declaration.identity) is not ReplicaIdentity:
        raise ReplicaPublicationRejectedError("replica identity is invalid")
    _validate_identifier("physical target ID", declaration.identity.physical_target_id)
    _validate_identifier("execution owner ID", declaration.identity.execution_owner_id)
    if (
        type(declaration.identity.replica_generation) is not int
        or declaration.identity.replica_generation < 1
    ):
        raise ReplicaPublicationRejectedError("replica generation is invalid")
    if type(declaration.object_format) is not str or declaration.object_format not in (
        "sha1",
        "sha256",
    ):
        raise ReplicaPublicationRejectedError("object format is invalid")
    _validate_sha256("source state digest", declaration.source_state_sha256)
    _validate_sha256("reconciliation fingerprint", declaration.reconciliation_fingerprint)
    if type(declaration.limits) is not ReplicaStoreLimits:
        raise ReplicaPublicationRejectedError("declared limits are invalid")
    if declaration.limits != ceilings:
        raise ReplicaPublicationRejectedError("declared limits differ from the configured profile")
    _validate_binding(
        declaration.bundle,
        role="committed_bundle",
        maximum=declaration.limits.bundle.max_bundle_bytes,
    )
    _validate_binding(
        declaration.worktree,
        role="worktree",
        maximum=declaration.limits.worktree.max_total_bytes,
    )
    _validate_binding(
        declaration.index_objects,
        role="index_objects",
        maximum=declaration.limits.index_objects.max_pack_bytes,
    )
    try:
        validate_distinct_artifact_ids(
            (
                declaration.bundle.artifact_id,
                declaration.worktree.artifact_id,
                declaration.index_objects.artifact_id,
            )
        )
    except ValueError as error:
        raise ReplicaPublicationRejectedError(str(error)) from error
    total = (
        declaration.bundle.byte_length
        + declaration.worktree.byte_length
        + declaration.index_objects.byte_length
    )
    if total > declaration.limits.max_set_bytes:
        raise ReplicaPublicationRejectedError("artifact set exceeds aggregate byte limit")
    return declaration


def _match_content(binding: ReplicaArtifactBinding, content: object) -> bytes:
    if type(content) is not bytes:
        raise ReplicaPublicationRejectedError(f"{binding.role} content is invalid")
    if len(content) != binding.byte_length:
        raise ReplicaPublicationRejectedError(f"{binding.role} length differs from declaration")
    if hashlib.sha256(content).hexdigest() != binding.sha256:
        raise ReplicaPublicationRejectedError(f"{binding.role} digest differs from declaration")
    return content


def _decode_declared_worktree(
    declaration: ReplicaArtifactSetDeclaration,
    worktree_bytes: bytes,
) -> VerifiedWorktreeArtifact:
    return decode_worktree_artifact(
        _match_content(declaration.worktree, worktree_bytes),
        limits=declaration.limits.worktree,
    )


def _inventory_digest(items: object) -> str:
    return _domain_digest(b"yinshi-replica-inventory-v1", items)


def _missing_index_objects(
    declaration: ReplicaArtifactSetDeclaration,
    worktree: VerifiedWorktreeArtifact,
    bundle: VerifiedCommittedBundle,
) -> tuple[str, ...]:
    if worktree.object_format != declaration.object_format:
        raise ReplicaPublicationRejectedError("worktree object format differs from declaration")
    if worktree.source_state_sha256.hex() != declaration.source_state_sha256:
        raise ReplicaPublicationRejectedError("worktree source state differs from declaration")
    head_refs = tuple(item for item in bundle.refs if item.name == "HEAD")
    if not worktree.head_oid:
        if head_refs:
            raise ReplicaPublicationRejectedError("bundle HEAD differs from unborn worktree HEAD")
    elif len(head_refs) != 1 or head_refs[0].object_id != worktree.head_oid.hex():
        raise ReplicaPublicationRejectedError("bundle HEAD differs from worktree HEAD")
    bundled_by_id = {item.object_id: item for item in bundle.objects}
    index_oids = {entry.object_id.hex() for entry in worktree.index_entries}
    for object_id in index_oids:
        bundled = bundled_by_id.get(object_id)
        if bundled is not None and bundled.kind != "blob":
            raise ReplicaPublicationRejectedError("index object in bundle is not a blob")
    return tuple(sorted(index_oids - set(bundled_by_id)))


def _verification_receipt(
    declaration: ReplicaArtifactSetDeclaration,
    bundle: VerifiedCommittedBundle,
    index_pack: VerifiedIndexObjectPack,
) -> ReplicaVerificationReceipt:
    refs_value = [asdict(item) for item in bundle.refs]
    bundle_inventory = [asdict(item) for item in bundle.objects]
    index_inventory = [asdict(item) for item in index_pack.objects]
    artifact_set_sha256 = compute_replica_artifact_set_sha256(
        operation_id=declaration.operation_id,
        repository_id=declaration.repository_id,
        workspace_id=declaration.workspace_id,
        physical_target_id=declaration.identity.physical_target_id,
        replica_generation=declaration.identity.replica_generation,
        execution_owner_id=declaration.identity.execution_owner_id,
        object_format=declaration.object_format,
        source_state_sha256=declaration.source_state_sha256,
        reconciliation_fingerprint=declaration.reconciliation_fingerprint,
        bundle=asdict(declaration.bundle),
        worktree=asdict(declaration.worktree),
        index_objects=asdict(declaration.index_objects),
        limits_sha256=compute_replica_limits_sha256(asdict(declaration.limits)),
    )
    bundle_refs_sha256 = _inventory_digest(refs_value)
    bundle_inventory_sha256 = _inventory_digest(bundle_inventory)
    index_inventory_sha256 = _inventory_digest(index_inventory)
    receipt_value = {
        "artifact_set_sha256": artifact_set_sha256,
        "object_format": declaration.object_format,
        "source_state_sha256": declaration.source_state_sha256,
        "reconciliation_fingerprint": declaration.reconciliation_fingerprint,
        "bundle": asdict(declaration.bundle),
        "worktree": asdict(declaration.worktree),
        "index_objects": asdict(declaration.index_objects),
        "bundle_refs_sha256": bundle_refs_sha256,
        "bundle_inventory_sha256": bundle_inventory_sha256,
        "index_inventory_sha256": index_inventory_sha256,
    }
    return ReplicaVerificationReceipt(
        verification_receipt_id=_domain_digest(
            b"yinshi-replica-verification-receipt-v2",
            receipt_value,
        ),
        artifact_set_sha256=artifact_set_sha256,
        object_format=declaration.object_format,
        source_state_sha256=declaration.source_state_sha256,
        reconciliation_fingerprint=declaration.reconciliation_fingerprint,
        bundle=declaration.bundle,
        worktree=declaration.worktree,
        index_objects=declaration.index_objects,
        bundle_refs_sha256=bundle_refs_sha256,
        bundle_inventory_sha256=bundle_inventory_sha256,
        index_inventory_sha256=index_inventory_sha256,
    )


async def _verify_artifact_set(
    declaration: ReplicaArtifactSetDeclaration,
    bundle_bytes: bytes,
    worktree_bytes: bytes,
    index_object_bytes: bytes,
    ceilings: ReplicaStoreLimits,
) -> ReplicaVerificationReceipt:
    declaration = _validate_declaration(declaration, ceilings)
    bundle_bytes = _match_content(declaration.bundle, bundle_bytes)
    worktree_bytes = _match_content(declaration.worktree, worktree_bytes)
    index_object_bytes = _match_content(declaration.index_objects, index_object_bytes)
    try:
        worktree = decode_worktree_artifact(worktree_bytes, limits=declaration.limits.worktree)
        bundle = await verify_committed_bundle(
            bundle_bytes,
            object_format=declaration.object_format,
            limits=declaration.limits.bundle,
        )
        missing = _missing_index_objects(declaration, worktree, bundle)
        index_pack = await verify_index_object_pack(
            index_object_bytes,
            object_format=declaration.object_format,
            expected_oids=missing,
            limits=declaration.limits.index_objects,
        )
    except asyncio.CancelledError:
        raise
    except ReplicaPublicationRejectedError:
        raise
    except Exception as error:
        raise ReplicaPublicationRejectedError("artifact verification failed") from error
    return _verification_receipt(declaration, bundle, index_pack)


def _root_identity(root: Path, expected_uid: int, expected_gid: int) -> tuple[int, str]:
    try:
        named = os.lstat(root)
        descriptor = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise ReplicaPublicationRejectedError("publication root is unavailable") from error
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        or opened.st_uid != expected_uid
        or opened.st_gid != expected_gid
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise ReplicaPublicationRejectedError("publication root is not exclusively owned")
    identity = _canonical_json({"device": opened.st_dev, "inode": opened.st_ino}).decode("ascii")
    return descriptor, identity


def _write_regular(parent: int, name: str, content: bytes) -> tuple[int, int]:
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
        view = memoryview(content)
        offset = 0
        while offset < len(view):
            count = os.write(descriptor, view[offset:])
            if count <= 0:
                raise OSError("artifact write did not make progress")
            offset += count
        os.fsync(descriptor)
        value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or stat.S_IMODE(value.st_mode) != 0o600
        ):
            raise OSError("artifact file mode or link count is invalid")
        return value.st_dev, value.st_ino
    finally:
        os.close(descriptor)


def _create_descriptor_target(parent: int, name: str) -> tuple[int, tuple[int, int]]:
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
        value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or value.st_size != 0
            or stat.S_IMODE(value.st_mode) != 0o600
        ):
            raise OSError("artifact target mode or link count is invalid")
        return descriptor, (value.st_dev, value.st_ino)
    except BaseException:
        os.close(descriptor)
        raise


def _copy_descriptor_regular(
    descriptor: int,
    source: int,
    byte_length: int,
    sha256: str,
) -> None:
    digest = hashlib.sha256()
    offset = 0
    while offset < byte_length:
        content = os.pread(source, min(64 * 1024, byte_length - offset), offset)
        if not content:
            raise ReplicaPublicationRejectedError("source artifact is truncated")
        digest.update(content)
        view = memoryview(content)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("artifact write did not make progress")
            written += count
        offset += len(content)
    if os.pread(source, 1, byte_length) or digest.hexdigest() != sha256:
        raise ReplicaPublicationRejectedError("source artifact digest changed")
    os.fsync(descriptor)
    value = os.fstat(descriptor)
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
        or value.st_size != byte_length
        or stat.S_IMODE(value.st_mode) != 0o600
    ):
        raise OSError("artifact file mode, length, or link count is invalid")


def _write_descriptor_regular(
    parent: int,
    name: str,
    source: int,
    byte_length: int,
    sha256: str,
) -> tuple[int, int]:
    descriptor, identity = _create_descriptor_target(parent, name)
    try:
        _copy_descriptor_regular(descriptor, source, byte_length, sha256)
        return identity
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class _PinnedRegular:
    descriptor: int
    name: str
    device: int
    inode: int
    byte_length: int
    sha256: str


def _read_descriptor(descriptor: int, byte_length: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while offset < byte_length:
        chunk = os.pread(descriptor, min(1024 * 1024, byte_length - offset), offset)
        if not chunk:
            raise ReplicaPublicationRejectedError("published artifact is truncated")
        chunks.append(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, byte_length):
        raise ReplicaPublicationRejectedError("published artifact grew during inspection")
    return b"".join(chunks)


def _hash_descriptor(descriptor: int, byte_length: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < byte_length:
        chunk = os.pread(descriptor, min(1024 * 1024, byte_length - offset), offset)
        if not chunk:
            raise ReplicaPublicationRejectedError("published artifact is truncated")
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, byte_length):
        raise ReplicaPublicationRejectedError("published artifact grew during inspection")
    return digest.hexdigest()


def _open_pinned_regular(
    parent: int,
    name: str,
    maximum: int,
    expected_uid: int,
    expected_gid: int,
) -> _PinnedRegular:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent,
        )
    except OSError as error:
        raise ReplicaPublicationRejectedError("published artifact storage is invalid") from error
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != expected_uid
            or opened.st_gid != expected_gid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size < 0
            or opened.st_size > maximum
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise ReplicaPublicationRejectedError("published artifact storage is invalid")
        return _PinnedRegular(
            descriptor=descriptor,
            name=name,
            device=opened.st_dev,
            inode=opened.st_ino,
            byte_length=opened.st_size,
            sha256=_hash_descriptor(descriptor, opened.st_size),
        )
    except BaseException:
        os.close(descriptor)
        raise


def _recheck_pinned_regular(
    parent: int,
    pinned: _PinnedRegular,
    expected_uid: int,
    expected_gid: int,
) -> None:
    opened = os.fstat(pinned.descriptor)
    named = os.stat(pinned.name, dir_fd=parent, follow_symlinks=False)
    if (
        (opened.st_dev, opened.st_ino, opened.st_size)
        != (pinned.device, pinned.inode, pinned.byte_length)
        or (named.st_dev, named.st_ino) != (pinned.device, pinned.inode)
        or not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or stat.S_IMODE(named.st_mode) != 0o600
        or opened.st_uid != expected_uid
        or opened.st_gid != expected_gid
        or named.st_uid != expected_uid
        or named.st_gid != expected_gid
        or opened.st_nlink != 1
        or named.st_nlink != 1
        or _hash_descriptor(pinned.descriptor, pinned.byte_length) != pinned.sha256
    ):
        raise ReplicaPublicationRejectedError("published artifact changed during verification")


def _open_pinned_files(
    parent: int,
    maxima: dict[str, int],
    expected_uid: int,
    expected_gid: int,
) -> list[_PinnedRegular]:
    pins: list[_PinnedRegular] = []
    try:
        for name, maximum in maxima.items():
            pins.append(
                _open_pinned_regular(
                    parent,
                    name,
                    maximum,
                    expected_uid,
                    expected_gid,
                )
            )
        return pins
    except BaseException:
        for pinned in pins:
            os.close(pinned.descriptor)
        raise


async def _open_pinned_files_drained(
    parent: int,
    maxima: dict[str, int],
    expected_uid: int,
    expected_gid: int,
) -> list[_PinnedRegular]:
    task = asyncio.create_task(
        asyncio.to_thread(
            _open_pinned_files,
            parent,
            maxima,
            expected_uid,
            expected_gid,
        )
    )
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
    pins = task.result()
    if cancellation is not None:
        for pinned in pins:
            os.close(pinned.descriptor)
        raise cancellation
    return pins


def _recheck_pinned_files(
    parent: int,
    pins: list[_PinnedRegular],
    expected_uid: int,
    expected_gid: int,
) -> None:
    for pinned in pins:
        _recheck_pinned_regular(parent, pinned, expected_uid, expected_gid)


def _opened_binding_matches(
    binding: ReplicaArtifactBinding,
    artifact: OpenedArtifact,
) -> bool:
    reference = artifact.reference
    return (
        reference.artifact_id == binding.artifact_id
        and reference.sha256 == binding.sha256
        and reference.byte_length == binding.byte_length
    )


def _recheck_opened_descriptors(
    declaration: ReplicaArtifactSetDeclaration,
    opened: OpenedReplicaArtifacts,
    expected_uid: int,
    expected_gid: int,
) -> None:
    if (
        type(opened) is not OpenedReplicaArtifacts
        or opened.operation_id != declaration.operation_id
    ):
        raise ReplicaPublicationRejectedError("opened artifact set identity is invalid")
    entries = (
        (_BUNDLE_FILE, declaration.bundle, opened.bundle),
        (_WORKTREE_FILE, declaration.worktree, opened.worktree),
        (_INDEX_OBJECTS_FILE, declaration.index_objects, opened.index_objects),
    )
    directory = os.fstat(opened.directory_descriptor)
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != expected_uid
        or directory.st_gid != expected_gid
        or stat.S_IMODE(directory.st_mode) != 0o700
        or not _has_exact_entries(
            opened.directory_descriptor,
            frozenset(REPLICA_ARTIFACT_FILENAMES.values()),
        )
    ):
        raise ReplicaPublicationRejectedError("opened artifact directory changed")
    for name, binding, artifact in entries:
        current = os.fstat(artifact.descriptor)
        named = os.stat(name, dir_fd=opened.directory_descriptor, follow_symlinks=False)
        if (
            not _opened_binding_matches(binding, artifact)
            or not stat.S_ISREG(current.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (current.st_dev, current.st_ino) != (artifact.device, artifact.inode)
            or (named.st_dev, named.st_ino) != (artifact.device, artifact.inode)
            or current.st_uid != expected_uid
            or current.st_gid != expected_gid
            or named.st_uid != expected_uid
            or named.st_gid != expected_gid
            or stat.S_IMODE(current.st_mode) != 0o600
            or stat.S_IMODE(named.st_mode) != 0o600
            or current.st_nlink != 1
            or named.st_nlink != 1
            or current.st_size != binding.byte_length
            or named.st_size != binding.byte_length
            or _hash_descriptor(artifact.descriptor, binding.byte_length) != binding.sha256
        ):
            raise ReplicaPublicationRejectedError("opened artifact changed")


def _directory_identity(descriptor: int) -> str:
    value = os.fstat(descriptor)
    return _canonical_json({"device": value.st_dev, "inode": value.st_ino}).decode("ascii")


def _same_identity(value: os.stat_result, identity: str) -> bool:
    expected = json.loads(identity)
    return (value.st_dev, value.st_ino) == (expected.get("device"), expected.get("inode"))


class WorkspaceReplicaPublicationStore:
    """Own private staging and one-rename visibility for artifact sets."""

    def __init__(
        self,
        root: str | Path,
        *,
        ceilings: ReplicaStoreLimits,
        expected_uid: int,
        expected_gid: int,
    ) -> None:
        root_path = Path(root)
        if not root_path.is_absolute():
            raise ValueError("publication root must be absolute")
        if type(ceilings) is not ReplicaStoreLimits:
            raise TypeError("ceilings must be ReplicaStoreLimits")
        if type(expected_uid) is not int or type(expected_gid) is not int:
            raise TypeError("expected ownership must use integers")
        if expected_uid != os.geteuid() or expected_gid != os.getegid():
            raise ValueError("publication store must run as expected owner")
        descriptor, _identity = _root_identity(root_path, expected_uid, expected_gid)
        os.close(descriptor)
        self._root = root_path
        self._ceilings = ceilings
        self._expected_uid = expected_uid
        self._expected_gid = expected_gid
        self._limits_sha256 = compute_replica_limits_sha256(asdict(ceilings))

    @property
    def limits_sha256(self) -> str:
        """Return the exact immutable limit profile accepted by this store."""
        return self._limits_sha256

    @property
    def limits(self) -> ReplicaStoreLimits:
        """Return the immutable configured publication limit profile."""
        return self._ceilings

    async def _verify_pinned_semantics(
        self,
        declaration: ReplicaArtifactSetDeclaration,
        *,
        bundle_descriptor: int,
        worktree_descriptor: int,
        index_objects_descriptor: int,
    ) -> ReplicaVerificationReceipt:
        """Verify exact descriptor sources through private file-backed Git inputs."""
        with tempfile.TemporaryDirectory(prefix="yinshi-replica-pinned-") as directory:
            os.chmod(directory, 0o700)
            root = Path(directory)
            root_descriptor = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                await _run_blocking(
                    _write_descriptor_regular,
                    root_descriptor,
                    _BUNDLE_FILE,
                    bundle_descriptor,
                    declaration.bundle.byte_length,
                    declaration.bundle.sha256,
                )
                await _run_blocking(
                    _write_descriptor_regular,
                    root_descriptor,
                    _INDEX_OBJECTS_FILE,
                    index_objects_descriptor,
                    declaration.index_objects.byte_length,
                    declaration.index_objects.sha256,
                )
                worktree_bytes = await _run_blocking(
                    _read_descriptor,
                    worktree_descriptor,
                    declaration.worktree.byte_length,
                )
            finally:
                os.close(root_descriptor)
            bundle_path = root / _BUNDLE_FILE
            index_path = root / _INDEX_OBJECTS_FILE
            bundle_copy = os.open(
                bundle_path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                index_copy = os.open(
                    index_path,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    worktree = await _run_blocking(
                        _decode_declared_worktree,
                        declaration,
                        worktree_bytes,
                    )
                    bundle = await verify_committed_bundle_file(
                        bundle_path,
                        bundle_copy,
                        object_format=declaration.object_format,
                        byte_length=declaration.bundle.byte_length,
                        sha256=declaration.bundle.sha256,
                        limits=declaration.limits.bundle,
                    )
                    missing = _missing_index_objects(declaration, worktree, bundle)
                    index_pack = await verify_index_object_pack_file(
                        index_path,
                        index_copy,
                        object_format=declaration.object_format,
                        expected_oids=missing,
                        byte_length=declaration.index_objects.byte_length,
                        sha256=declaration.index_objects.sha256,
                        limits=declaration.limits.index_objects,
                    )
                finally:
                    os.close(index_copy)
            finally:
                os.close(bundle_copy)
        return _verification_receipt(declaration, bundle, index_pack)

    async def _verify_opened_semantics(
        self,
        declaration: ReplicaArtifactSetDeclaration,
        opened: OpenedReplicaArtifacts,
    ) -> ReplicaVerificationReceipt:
        return await self._verify_pinned_semantics(
            declaration,
            bundle_descriptor=opened.bundle.descriptor,
            worktree_descriptor=opened.worktree.descriptor,
            index_objects_descriptor=opened.index_objects.descriptor,
        )

    async def _recheck_opened_source(
        self,
        opened: OpenedReplicaArtifacts,
        recheck_source: Callable[[OpenedReplicaArtifacts], Awaitable[None]],
    ) -> None:
        try:
            await recheck_source(opened)
        except asyncio.CancelledError:
            raise
        except BrokerArtifactStoreRejectedError as error:
            raise ReplicaPublicationRejectedError("opened artifact source changed") from error
        except BrokerArtifactStoreUnresolvedError as error:
            raise ReplicaPublicationUnresolvedError(
                "opened artifact source is unresolved"
            ) from error
        except Exception as error:
            raise ReplicaPublicationUnresolvedError(
                "opened artifact source recheck failed"
            ) from error

    async def verify_opened_artifact_set(
        self,
        declaration: ReplicaArtifactSetDeclaration,
        opened: OpenedReplicaArtifacts,
        *,
        recheck_source: Callable[[OpenedReplicaArtifacts], Awaitable[None]],
    ) -> ReplicaVerificationReceipt:
        """Verify descriptor-pinned incoming artifacts and recheck their owner."""
        declaration = _validate_declaration(declaration, self._ceilings)
        if not callable(recheck_source):
            raise TypeError("recheck_source must be callable")
        await self._recheck_opened_source(opened, recheck_source)
        await _run_blocking(
            _recheck_opened_descriptors,
            declaration,
            opened,
            self._expected_uid,
            self._expected_gid,
        )
        try:
            receipt = await self._verify_opened_semantics(declaration, opened)
        except asyncio.CancelledError:
            raise
        except ReplicaPublicationRejectedError:
            raise
        except Exception as error:
            raise ReplicaPublicationRejectedError("artifact verification failed") from error
        await self._recheck_opened_source(opened, recheck_source)
        await _run_blocking(
            _recheck_opened_descriptors,
            declaration,
            opened,
            self._expected_uid,
            self._expected_gid,
        )
        return receipt

    async def verify_artifact_set(
        self,
        declaration: ReplicaArtifactSetDeclaration,
        bundle_bytes: bytes,
        worktree_bytes: bytes,
        index_object_bytes: bytes,
    ) -> ReplicaVerificationReceipt:
        """Verify one exact declared set without publishing it."""
        return await _verify_artifact_set(
            declaration,
            bundle_bytes,
            worktree_bytes,
            index_object_bytes,
            self._ceilings,
        )

    def _inspect_pending(
        self,
        parent: int,
        pending_name: str,
        expected: dict[str, tuple[int, str]],
    ) -> tuple[str, dict[str, tuple[int, int]]]:
        child = os.open(
            pending_name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        pins: list[_PinnedRegular] = []
        try:
            child_stat = os.fstat(child)
            if (
                child_stat.st_uid != self._expected_uid
                or child_stat.st_gid != self._expected_gid
                or stat.S_IMODE(child_stat.st_mode) != 0o700
                or not _has_exact_entries(child, _EXPECTED_FILES)
            ):
                raise ReplicaPublicationRejectedError("publication stage directory is invalid")
            for name, (byte_length, sha256) in expected.items():
                pinned = _open_pinned_regular(
                    child,
                    name,
                    byte_length,
                    self._expected_uid,
                    self._expected_gid,
                )
                pins.append(pinned)
                if pinned.byte_length != byte_length or pinned.sha256 != sha256:
                    raise ReplicaPublicationRejectedError("publication stage content changed")
            for pinned in pins:
                _recheck_pinned_regular(
                    child,
                    pinned,
                    self._expected_uid,
                    self._expected_gid,
                )
            return _directory_identity(child), {
                pinned.name: (pinned.device, pinned.inode) for pinned in pins
            }
        finally:
            for pinned in pins:
                os.close(pinned.descriptor)
            os.close(child)

    def _cleanup_pending(
        self,
        parent: int,
        root_identity: str,
        pending_name: str,
        pending_identity: str,
        file_identities: dict[str, tuple[int, int]],
    ) -> None:
        abandoned_name: str | None = None
        try:
            operation_id = pending_name.split(".", 3)[1]
            attempt_id = pending_name.rsplit(".", 1)[-1]
            for _attempt in range(16):
                candidate = f".{operation_id}.pending.abandoned.{attempt_id}.{os.urandom(16).hex()}"
                try:
                    atomic_rename_no_replace(
                        self._root / pending_name,
                        self._root / candidate,
                        source_parent_identity=root_identity,
                        target_parent_identity=root_identity,
                        source_identity=pending_identity,
                    )
                except WorkspacePublicationCollisionError:
                    continue
                abandoned_name = candidate
                break
            if abandoned_name is None:
                raise ReplicaPublicationUnresolvedError(
                    "publication quarantine name allocation is unresolved"
                )
            child = os.open(
                abandoned_name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            try:
                child_value = os.fstat(child)
                if (
                    not _same_identity(child_value, pending_identity)
                    or child_value.st_uid != self._expected_uid
                    or child_value.st_gid != self._expected_gid
                    or stat.S_IMODE(child_value.st_mode) != 0o700
                    or not _has_exact_entries(child, frozenset(file_identities))
                ):
                    raise ReplicaPublicationUnresolvedError("quarantined publication stage changed")
                for name, identity in file_identities.items():
                    value = os.stat(name, dir_fd=child, follow_symlinks=False)
                    if (
                        (value.st_dev, value.st_ino) != identity
                        or not stat.S_ISREG(value.st_mode)
                        or value.st_uid != self._expected_uid
                        or value.st_gid != self._expected_gid
                        or stat.S_IMODE(value.st_mode) != 0o600
                        or value.st_nlink != 1
                    ):
                        raise ReplicaPublicationUnresolvedError(
                            "quarantined publication file changed"
                        )
            finally:
                os.close(child)
        except ReplicaPublicationUnresolvedError:
            raise
        except (WorkspacePublicationError, OSError) as error:
            raise ReplicaPublicationUnresolvedError(
                "publication stage quarantine is unresolved"
            ) from error

    def _rename_state(
        self,
        parent: int,
        pending_name: str,
        final_name: str,
        pending_identity: str,
    ) -> Literal["not_moved", "moved", "unknown"]:
        try:
            pending = os.stat(pending_name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pending = None
        except OSError:
            return "unknown"
        try:
            final = os.stat(final_name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            final = None
        except OSError:
            return "unknown"
        pending_matches = pending is not None and _same_identity(pending, pending_identity)
        final_matches = final is not None and _same_identity(final, pending_identity)
        if pending_matches and final is None:
            return "not_moved"
        if pending is None and final_matches:
            return "moved"
        return "unknown"

    def _publication_receipt(
        self,
        declaration: ReplicaArtifactSetDeclaration,
        inspection: ReplicaInspectionReceipt,
    ) -> ReplicaPublicationReceipt:
        synchronization_receipt_id = _domain_digest(
            b"yinshi-replica-synchronization-v2",
            {
                "artifact_set_sha256": inspection.artifact_set_sha256,
                "final_directory_identity": inspection.final_directory_identity,
                "publication_marker_sha256": inspection.publication_marker_sha256,
            },
        )
        value = {
            "artifact_set_sha256": inspection.artifact_set_sha256,
            "verification": asdict(inspection.verification),
            "identity": asdict(declaration.identity),
            "source_state_sha256": declaration.source_state_sha256,
            "final_directory_identity": inspection.final_directory_identity,
            "publication_marker_sha256": inspection.publication_marker_sha256,
            "synchronization_receipt_id": synchronization_receipt_id,
        }
        return ReplicaPublicationReceipt(
            publication_receipt_id=_domain_digest(
                b"yinshi-replica-publication-receipt-v2",
                value,
            ),
            artifact_set_sha256=inspection.artifact_set_sha256,
            verification=inspection.verification,
            identity=declaration.identity,
            source_state_sha256=declaration.source_state_sha256,
            final_directory_identity=inspection.final_directory_identity,
            publication_marker_sha256=inspection.publication_marker_sha256,
            synchronization_receipt_id=synchronization_receipt_id,
        )

    async def publish_artifact_set(
        self,
        declaration: ReplicaArtifactSetDeclaration,
        bundle_bytes: bytes,
        worktree_bytes: bytes,
        index_object_bytes: bytes,
    ) -> ReplicaPublicationReceipt:
        """Verify, sync, and publish one in-memory set through one rename."""
        verification = await self.verify_artifact_set(
            declaration,
            bundle_bytes,
            worktree_bytes,
            index_object_bytes,
        )
        return await self._publish_verified_sources(
            declaration,
            verification,
            byte_sources={
                _BUNDLE_FILE: bundle_bytes,
                _WORKTREE_FILE: worktree_bytes,
                _INDEX_OBJECTS_FILE: index_object_bytes,
            },
            descriptor_sources={},
        )

    async def publish_opened_artifact_set(
        self,
        declaration: ReplicaArtifactSetDeclaration,
        opened: OpenedReplicaArtifacts,
        *,
        recheck_source: Callable[[OpenedReplicaArtifacts], Awaitable[None]],
        expected_verification: ReplicaVerificationReceipt | None = None,
    ) -> ReplicaPublicationReceipt:
        """Verify and stream one descriptor-pinned set through one rename."""
        verification = await self.verify_opened_artifact_set(
            declaration,
            opened,
            recheck_source=recheck_source,
        )
        if expected_verification is not None and verification != expected_verification:
            raise ReplicaPublicationRejectedError("source verification receipt changed")
        return await self._publish_verified_sources(
            declaration,
            verification,
            byte_sources={},
            descriptor_sources={
                _BUNDLE_FILE: (
                    opened.bundle.descriptor,
                    declaration.bundle.byte_length,
                    declaration.bundle.sha256,
                ),
                _WORKTREE_FILE: (
                    opened.worktree.descriptor,
                    declaration.worktree.byte_length,
                    declaration.worktree.sha256,
                ),
                _INDEX_OBJECTS_FILE: (
                    opened.index_objects.descriptor,
                    declaration.index_objects.byte_length,
                    declaration.index_objects.sha256,
                ),
            },
            opened=opened,
            recheck_source=recheck_source,
        )

    async def _publish_verified_sources(
        self,
        declaration: ReplicaArtifactSetDeclaration,
        verification: ReplicaVerificationReceipt,
        *,
        byte_sources: dict[str, bytes],
        descriptor_sources: dict[str, tuple[int, int, str]],
        opened: OpenedReplicaArtifacts | None = None,
        recheck_source: Callable[[OpenedReplicaArtifacts], Awaitable[None]] | None = None,
    ) -> ReplicaPublicationReceipt:
        declaration = _validate_declaration(declaration, self._ceilings)
        if bool(opened is None) != bool(recheck_source is None):
            raise TypeError("opened source and recheck callback must be supplied together")
        if set(byte_sources) | set(descriptor_sources) != {
            _BUNDLE_FILE,
            _WORKTREE_FILE,
            _INDEX_OBJECTS_FILE,
        } or set(byte_sources) & set(descriptor_sources):
            raise TypeError("publication sources must contain exactly three distinct roles")
        try:
            require_atomic_no_replace_support()
        except WorkspacePublicationError as error:
            raise ReplicaPublicationRejectedError(
                "atomic no-replace publication is unavailable"
            ) from error
        root_descriptor, root_identity = _root_identity(
            self._root,
            self._expected_uid,
            self._expected_gid,
        )
        pending_name = ""
        final_name = declaration.operation_id
        pending_identity: str | None = None
        file_identities: dict[str, tuple[int, int]] = {}
        moved = False
        try:
            for _attempt in range(16):
                pending_name = f".{declaration.operation_id}.pending.{os.urandom(16).hex()}"
                try:
                    os.mkdir(pending_name, mode=0o700, dir_fd=root_descriptor)
                except FileExistsError:
                    continue
                break
            else:
                raise ReplicaPublicationUnresolvedError(
                    "publication stage name allocation is unresolved"
                )
            marker = _canonical_json(
                {"declaration": asdict(declaration), "verification": asdict(verification)}
            )
            expected_content = {**byte_sources, "artifact-set.json": marker}
            expected = {
                name: (len(content), hashlib.sha256(content).hexdigest())
                for name, content in expected_content.items()
            }
            expected.update(
                {
                    name: (byte_length, sha256)
                    for name, (_descriptor, byte_length, sha256) in descriptor_sources.items()
                }
            )
            try:
                pending_descriptor = os.open(
                    pending_name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=root_descriptor,
                )
                try:
                    pending_identity = _directory_identity(pending_descriptor)
                    for name, content in expected_content.items():
                        file_identities[name] = _write_regular(
                            pending_descriptor,
                            name,
                            content,
                        )
                    for name, (source, byte_length, sha256) in descriptor_sources.items():
                        target, identity = _create_descriptor_target(pending_descriptor, name)
                        file_identities[name] = identity
                        try:
                            await _run_blocking(
                                _copy_descriptor_regular,
                                target,
                                source,
                                byte_length,
                                sha256,
                            )
                        finally:
                            os.close(target)
                    os.fsync(pending_descriptor)
                finally:
                    os.close(pending_descriptor)
                inspected_identity, inspected_files = await _run_blocking(
                    self._inspect_pending,
                    root_descriptor,
                    pending_name,
                    expected,
                )
                if inspected_identity != pending_identity or inspected_files != file_identities:
                    raise ReplicaPublicationRejectedError("publication stage identities changed")
                if opened is not None and recheck_source is not None:
                    await self._recheck_opened_source(opened, recheck_source)
                    await _run_blocking(
                        _recheck_opened_descriptors,
                        declaration,
                        opened,
                        self._expected_uid,
                        self._expected_gid,
                    )
            except BaseException as error:
                if pending_identity is None:
                    raise ReplicaPublicationUnresolvedError(
                        "publication stage identity is unresolved"
                    ) from error
                self._cleanup_pending(
                    root_descriptor,
                    root_identity,
                    pending_name,
                    pending_identity,
                    file_identities,
                )
                raise
            try:
                atomic_rename_no_replace(
                    self._root / pending_name,
                    self._root / final_name,
                    source_parent_identity=root_identity,
                    target_parent_identity=root_identity,
                    source_identity=pending_identity,
                )
                moved = True
            except WorkspacePublicationCollisionError as error:
                self._cleanup_pending(
                    root_descriptor,
                    root_identity,
                    pending_name,
                    pending_identity,
                    file_identities,
                )
                raise ReplicaPublicationCollisionError(
                    "publication target already exists"
                ) from error
            except (WorkspacePublicationError, OSError) as error:
                state = self._rename_state(
                    root_descriptor,
                    pending_name,
                    final_name,
                    pending_identity,
                )
                if state == "not_moved":
                    self._cleanup_pending(
                        root_descriptor,
                        root_identity,
                        pending_name,
                        pending_identity,
                        file_identities,
                    )
                    raise ReplicaPublicationRejectedError(
                        "publication rename failed before visibility"
                    ) from error
                raise ReplicaPublicationUnresolvedError(
                    "publication rename or synchronization is unresolved"
                ) from error
            try:
                inspection = await self._inspect_final(
                    declaration,
                    verification,
                    root_descriptor=root_descriptor,
                    expected_root_identity=root_identity,
                    expected_final_identity=pending_identity,
                )
            except asyncio.CancelledError as error:
                raise ReplicaPublicationUnresolvedError(
                    "publication cancellation is unresolved"
                ) from error
            except Exception as error:
                raise ReplicaPublicationUnresolvedError(
                    "published artifact verification is unresolved"
                ) from error
            return self._publication_receipt(declaration, inspection)
        except (ReplicaPublicationCollisionError, ReplicaPublicationRejectedError):
            raise
        except ReplicaPublicationUnresolvedError:
            raise
        except asyncio.CancelledError:
            if moved:
                raise ReplicaPublicationUnresolvedError("publication cancellation is unresolved")
            raise
        except Exception as error:
            if moved:
                raise ReplicaPublicationUnresolvedError(
                    "publication outcome is unresolved"
                ) from error
            raise ReplicaPublicationRejectedError("publication staging failed") from error
        finally:
            os.close(root_descriptor)

    async def _inspect_final(
        self,
        declaration: ReplicaArtifactSetDeclaration,
        expected_verification: ReplicaVerificationReceipt | None = None,
        *,
        root_descriptor: int | None = None,
        expected_root_identity: str | None = None,
        expected_final_identity: str | None = None,
    ) -> ReplicaInspectionReceipt:
        owns_root = root_descriptor is None
        if root_descriptor is None:
            root_descriptor, opened_root_identity = _root_identity(
                self._root,
                self._expected_uid,
                self._expected_gid,
            )
            expected_root_identity = opened_root_identity
        if expected_root_identity is None:
            raise ReplicaPublicationRejectedError("publication root identity is absent")
        pins: list[_PinnedRegular] = []
        try:
            try:
                final_descriptor = os.open(
                    declaration.operation_id,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=root_descriptor,
                )
            except OSError as error:
                raise ReplicaPublicationRejectedError(
                    "published artifact set is unavailable"
                ) from error
            try:
                final_identity = _directory_identity(final_descriptor)
                if (
                    expected_final_identity is not None
                    and final_identity != expected_final_identity
                ):
                    raise ReplicaPublicationRejectedError(
                        "published artifact set identity differs from staged state"
                    )
                final_stat = os.fstat(final_descriptor)
                if (
                    not stat.S_ISDIR(final_stat.st_mode)
                    or final_stat.st_uid != self._expected_uid
                    or final_stat.st_gid != self._expected_gid
                    or stat.S_IMODE(final_stat.st_mode) != 0o700
                    or not _has_exact_entries(final_descriptor, _EXPECTED_FILES)
                ):
                    raise ReplicaPublicationRejectedError(
                        "published artifact set directory is invalid"
                    )
                maxima = {
                    _BUNDLE_FILE: declaration.bundle.byte_length,
                    _WORKTREE_FILE: declaration.worktree.byte_length,
                    _INDEX_OBJECTS_FILE: declaration.index_objects.byte_length,
                    "artifact-set.json": 1024 * 1024,
                }
                pins = await _open_pinned_files_drained(
                    final_descriptor,
                    maxima,
                    self._expected_uid,
                    self._expected_gid,
                )
                pinned_by_name = {pinned.name: pinned for pinned in pins}
                marker = await _run_blocking(
                    _read_descriptor,
                    pinned_by_name["artifact-set.json"].descriptor,
                    pinned_by_name["artifact-set.json"].byte_length,
                )
                try:
                    verification = await self._verify_pinned_semantics(
                        declaration,
                        bundle_descriptor=pinned_by_name[_BUNDLE_FILE].descriptor,
                        worktree_descriptor=pinned_by_name[_WORKTREE_FILE].descriptor,
                        index_objects_descriptor=pinned_by_name[_INDEX_OBJECTS_FILE].descriptor,
                    )
                except asyncio.CancelledError:
                    raise
                except ReplicaPublicationRejectedError:
                    raise
                except Exception as error:
                    raise ReplicaPublicationRejectedError(
                        "published artifact verification failed"
                    ) from error
                if expected_verification is not None and verification != expected_verification:
                    raise ReplicaPublicationRejectedError("published verification receipt changed")
                expected_marker = _canonical_json(
                    {
                        "declaration": asdict(declaration),
                        "verification": asdict(verification),
                    }
                )
                if marker != expected_marker:
                    raise ReplicaPublicationRejectedError(
                        "publication marker differs from artifact set"
                    )
                named_final = os.stat(
                    declaration.operation_id,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
                current_final = os.fstat(final_descriptor)
                named_root = os.stat(self._root, follow_symlinks=False)
                current_root = os.fstat(root_descriptor)
                if (
                    not _same_identity(named_final, final_identity)
                    or not _same_identity(current_final, final_identity)
                    or not stat.S_ISDIR(named_final.st_mode)
                    or not stat.S_ISDIR(current_final.st_mode)
                    or named_final.st_uid != self._expected_uid
                    or named_final.st_gid != self._expected_gid
                    or current_final.st_uid != self._expected_uid
                    or current_final.st_gid != self._expected_gid
                    or stat.S_IMODE(named_final.st_mode) != 0o700
                    or stat.S_IMODE(current_final.st_mode) != 0o700
                    or not _has_exact_entries(final_descriptor, _EXPECTED_FILES)
                    or not _same_identity(named_root, expected_root_identity)
                    or not _same_identity(current_root, expected_root_identity)
                    or not stat.S_ISDIR(named_root.st_mode)
                    or not stat.S_ISDIR(current_root.st_mode)
                    or named_root.st_uid != self._expected_uid
                    or named_root.st_gid != self._expected_gid
                    or current_root.st_uid != self._expected_uid
                    or current_root.st_gid != self._expected_gid
                    or stat.S_IMODE(named_root.st_mode) != 0o700
                    or stat.S_IMODE(current_root.st_mode) != 0o700
                ):
                    raise ReplicaPublicationRejectedError(
                        "published artifact set changed during verification"
                    )
                await _run_blocking(
                    _recheck_pinned_files,
                    final_descriptor,
                    pins,
                    self._expected_uid,
                    self._expected_gid,
                )
            finally:
                for pinned in pins:
                    os.close(pinned.descriptor)
                os.close(final_descriptor)
        finally:
            if owns_root:
                os.close(root_descriptor)
        marker_sha256 = hashlib.sha256(marker).hexdigest()
        value = {
            "artifact_set_sha256": verification.artifact_set_sha256,
            "verification_receipt_id": verification.verification_receipt_id,
            "final_directory_identity": final_identity,
            "publication_marker_sha256": marker_sha256,
        }
        return ReplicaInspectionReceipt(
            inspection_receipt_id=_domain_digest(
                b"yinshi-replica-inspection-receipt-v2",
                value,
            ),
            artifact_set_sha256=verification.artifact_set_sha256,
            verification=verification,
            final_directory_identity=final_identity,
            publication_marker_sha256=marker_sha256,
        )

    async def inspect_published_artifact_set(
        self,
        declaration: ReplicaArtifactSetDeclaration,
    ) -> ReplicaInspectionReceipt:
        """Read and reverify storage without minting publication authority."""
        declaration = _validate_declaration(declaration, self._ceilings)
        try:
            return await self._inspect_final(declaration)
        except asyncio.CancelledError:
            raise
        except ReplicaPublicationRejectedError:
            raise
        except Exception as error:
            raise ReplicaPublicationRejectedError("published artifact inspection failed") from error
