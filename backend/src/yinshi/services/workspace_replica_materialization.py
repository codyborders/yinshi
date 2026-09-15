"""Durably materialize one authoritative replica into launcher staging."""

from __future__ import annotations

import asyncio
import base64
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, NoReturn, TypeVar

from yinshi.exceptions import GitError
from yinshi.services.filesystem_identity import descriptor_mount_id as _mount_id
from yinshi.services.git import _StableDirectory, run_git_bytes
from yinshi.services.replica_artifact_contract import (
    compute_replica_limits_sha256,
    validate_replica_operation_id,
)
from yinshi.services.workspace_publication import (
    WorkspacePublicationError,
    atomic_rename_no_replace,
    require_atomic_no_replace_support,
)
from yinshi.services.workspace_replica_artifact import (
    ManifestEntry,
    VerifiedWorktreeArtifact,
    WorktreeEntryKind,
)
from yinshi.services.workspace_replica_bundle import VerifiedCommittedBundle
from yinshi.services.workspace_replica_object_pack import VerifiedIndexObjectPack
from yinshi.services.workspace_replica_publication import (
    OpenedPublishedReplicaArtifacts,
    ReplicaArtifactSetDeclaration,
    ReplicaPublicationCollisionError,
    ReplicaPublicationReceipt,
    ReplicaPublicationRejectedError,
    ReplicaPublicationUnresolvedError,
    ReplicaStoreLimits,
    WorkspaceReplicaPublicationStore,
)

_RECEIPT_DOMAIN = b"yinshi-replica-materialization-receipt-v1"
_TREE_DOMAIN = b"yinshi-replica-materialization-tree-v1"
_REQUEST_DOMAIN = b"yinshi-replica-materialization-request-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PENDING = re.compile(r"\.[0-9a-f]{32}\.pending\.[0-9a-f]{32}\Z")
_RECORD_NAMES = ("intent.json", "stage.json", "prepared.json", "receipt.json")
_ALLOWED_GIT = frozenset(
    {
        "cat-file",
        "bundle",
        "for-each-ref",
        "index-pack",
        "init",
        "ls-files",
        "rev-parse",
        "symbolic-ref",
        "update-ref",
    }
)
_GIT_ENV = {
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "Never",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_COUNT": "6",
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": os.devnull,
    "GIT_CONFIG_KEY_1": "protocol.allow",
    "GIT_CONFIG_VALUE_1": "never",
    "GIT_CONFIG_KEY_2": "submodule.recurse",
    "GIT_CONFIG_VALUE_2": "false",
    "GIT_CONFIG_KEY_3": "core.fsmonitor",
    "GIT_CONFIG_VALUE_3": "false",
    "GIT_CONFIG_KEY_4": "core.logAllRefUpdates",
    "GIT_CONFIG_VALUE_4": "false",
    "GIT_CONFIG_KEY_5": "pack.writeReverseIndex",
    "GIT_CONFIG_VALUE_5": "false",
}


_T = TypeVar("_T")


class ReplicaMaterializationRejectedError(RuntimeError):
    """Materialization input or durable state violates the exact contract."""


class ReplicaMaterializationCollisionError(ReplicaMaterializationRejectedError):
    """A foreign destination occupies an operation-owned name."""


class ReplicaMaterializationUnresolvedError(RuntimeError):
    """Materialization completion is unknown after an operational failure."""


@dataclass(frozen=True, slots=True)
class ReplicaMaterializationRequest:
    """Immutable authority for one exact materialization."""

    version: int
    declaration: ReplicaArtifactSetDeclaration
    publication: ReplicaPublicationReceipt


@dataclass(frozen=True, slots=True)
class ReplicaMaterializationReceipt:
    """Durable result without filesystem paths or executor credentials."""

    version: int
    materialization_receipt_id: str
    operation_id: str
    artifact_set_sha256: str
    publication_receipt_id: str
    verification_receipt_id: str
    source_state_sha256: str
    limits_sha256: str
    staging_tree_sha256: str
    operation_directory_identity: str
    repo_directory_identity: str
    home_directory_identity: str


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _domain_digest(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + b"\x00" + _canonical_json(value)).hexdigest()


def _identity(metadata: os.stat_result) -> str:
    return _canonical_json({"device": metadata.st_dev, "inode": metadata.st_ino}).decode("ascii")


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _file_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _raise_root_error(message: str, error: BaseException | None = None) -> NoReturn:
    if error is None:
        raise ReplicaMaterializationRejectedError(message)
    raise ReplicaMaterializationUnresolvedError(message) from error


def _open_private_root(path: Path, uid: int, gid: int) -> tuple[int, os.stat_result, int]:
    try:
        named = os.lstat(path)
    except OSError as error:
        _raise_root_error("materialization root lookup is unresolved", error)
    if (
        not stat.S_ISDIR(named.st_mode)
        or named.st_uid != uid
        or named.st_gid != gid
        or stat.S_IMODE(named.st_mode) != 0o700
    ):
        _raise_root_error("materialization root is not exclusively owned")
    try:
        descriptor = os.open(path, _directory_flags())
    except OSError as error:
        _raise_root_error("materialization root open is unresolved", error)
    opened = os.fstat(descriptor)
    try:
        mount_id = _mount_id(descriptor)
    except RuntimeError as error:
        os.close(descriptor)
        _raise_root_error("materialization root mount identity is unresolved", error)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        or opened.st_uid != uid
        or opened.st_gid != gid
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        os.close(descriptor)
        _raise_root_error("materialization root changed during open")
    return descriptor, opened, mount_id


def _check_named_directory(
    parent: int,
    name: str,
    descriptor: int,
    *,
    uid: int,
    gid: int,
    mount_id: int,
    mode: int = 0o700,
) -> os.stat_result:
    opened = os.fstat(descriptor)
    try:
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as error:
        raise ReplicaMaterializationUnresolvedError(
            "owned directory lookup is unresolved"
        ) from error
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        or opened.st_uid != uid
        or opened.st_gid != gid
        or stat.S_IMODE(opened.st_mode) != mode
        or _mount_id(descriptor) != mount_id
    ):
        raise ReplicaMaterializationRejectedError("owned directory identity is invalid")
    return opened


def _request_binding(request: ReplicaMaterializationRequest) -> dict[str, object]:
    declaration = request.declaration
    publication = request.publication
    return {
        "version": request.version,
        "operation_id": declaration.operation_id,
        "repository_id": declaration.repository_id,
        "workspace_id": declaration.workspace_id,
        "replica_identity": asdict(declaration.identity),
        "object_format": declaration.object_format,
        "artifact_set_sha256": publication.artifact_set_sha256,
        "publication_receipt_id": publication.publication_receipt_id,
        "verification_receipt_id": publication.verification.verification_receipt_id,
        "source_state_sha256": declaration.source_state_sha256,
        "reconciliation_fingerprint": declaration.reconciliation_fingerprint,
        "limits_sha256": compute_replica_limits_sha256(asdict(declaration.limits)),
    }


def _intent_value(request: ReplicaMaterializationRequest, pending_name: str) -> dict[str, object]:
    binding = _request_binding(request)
    return {
        "record_version": 1,
        "request": binding,
        "request_sha256": _domain_digest(_REQUEST_DOMAIN, binding),
        "pending_name": pending_name,
    }


def _validate_request_shape(request: object) -> ReplicaMaterializationRequest:
    if type(request) is not ReplicaMaterializationRequest:
        raise ReplicaMaterializationRejectedError("materialization request is invalid")
    if type(request.version) is not int or request.version != 1:
        raise ReplicaMaterializationRejectedError("materialization request version is unsupported")
    if type(request.declaration) is not ReplicaArtifactSetDeclaration:
        raise ReplicaMaterializationRejectedError("materialization declaration is invalid")
    if type(request.publication) is not ReplicaPublicationReceipt:
        raise ReplicaMaterializationRejectedError("materialization publication is invalid")
    try:
        validate_replica_operation_id(request.declaration.operation_id)
    except ValueError as error:
        raise ReplicaMaterializationRejectedError(str(error)) from error
    return request


def _validate_source(opened: OpenedPublishedReplicaArtifacts) -> None:
    declaration = opened.declaration
    bundle = opened.verified_bundle
    worktree = opened.verified_worktree
    objects = opened.verified_index_objects
    if not (
        declaration.object_format
        == bundle.object_format
        == worktree.object_format
        == objects.object_format
    ):
        raise ReplicaMaterializationRejectedError("published object formats differ")
    for item in bundle.refs:
        if item.name.startswith("refs/replace/"):
            raise ReplicaMaterializationRejectedError("replacement refs are forbidden")
        if item.name != "HEAD" and not item.name.startswith("refs/"):
            raise ReplicaMaterializationRejectedError("bundle contains an unknown pseudo-ref")
    symbolic_target: str | None = None
    if worktree.head_state in {"symbolic", "unborn"}:
        try:
            symbolic_target = worktree.head_target.decode("ascii")
        except UnicodeDecodeError as error:
            raise ReplicaMaterializationRejectedError("HEAD target is invalid") from error
        if symbolic_target.startswith("refs/replace/"):
            raise ReplicaMaterializationRejectedError("replacement refs are forbidden")
    if (
        worktree.head_state == "unborn"
        and symbolic_target is not None
        and any(item.name == symbolic_target for item in bundle.refs)
    ):
        raise ReplicaMaterializationRejectedError("unborn HEAD target exists as a bundled ref")
    if any(item.kind not in {"blob", "commit", "tag", "tree"} for item in bundle.objects):
        raise ReplicaMaterializationRejectedError("bundle object type is unsupported")
    if any(item.kind != "blob" for item in objects.objects):
        raise ReplicaMaterializationRejectedError("supplemental object type is unsupported")


@contextmanager
def _exclusive_flock(descriptor: int) -> Iterator[None]:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN):
            raise ReplicaMaterializationUnresolvedError(
                "another materialization process owns the journal"
            ) from error
        raise ReplicaMaterializationUnresolvedError("journal lock is unresolved") from error
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass


def _read_record(
    directory: int,
    name: str,
    *,
    uid: int,
    gid: int,
    device: int,
    mount_id: int,
) -> tuple[dict[str, Any], os.stat_result] | None:
    try:
        named_before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ReplicaMaterializationUnresolvedError(
            "journal record lookup is unresolved"
        ) from error
    if stat.S_ISLNK(named_before.st_mode) or not stat.S_ISREG(named_before.st_mode):
        raise ReplicaMaterializationRejectedError("journal record type is invalid")
    try:
        descriptor = os.open(name, _file_flags(), dir_fd=directory)
    except OSError as error:
        raise ReplicaMaterializationUnresolvedError("journal record open is unresolved") from error
    try:
        before = os.fstat(descriptor)
        try:
            named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except OSError as error:
            raise ReplicaMaterializationUnresolvedError(
                "journal record lookup is unresolved"
            ) from error
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != uid
            or before.st_gid != gid
            or before.st_dev != device
            or _mount_id(descriptor) != mount_id
            or stat.S_IMODE(before.st_mode) != 0o600
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            or before.st_size < 3
            or before.st_size > 256 * 1024
        ):
            raise ReplicaMaterializationRejectedError("journal record identity is invalid")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                raise ReplicaMaterializationRejectedError("journal record is incomplete")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ReplicaMaterializationRejectedError("journal record changed during read")
        content = b"".join(chunks)
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReplicaMaterializationRejectedError("journal record is malformed") from error
        if type(value) is not dict or content != _canonical_json(value) + b"\n":
            raise ReplicaMaterializationRejectedError("journal record is not canonical")
        return value, before
    finally:
        os.close(descriptor)


def _publish_record(
    operation_path: Path,
    operation: int,
    name: str,
    value: dict[str, object],
    *,
    operation_identity: str,
) -> None:
    content = _canonical_json(value) + b"\n"
    temporary = f".{name}.tmp.{secrets.token_hex(16)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=operation,
        )
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "short journal write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        atomic_rename_no_replace(
            operation_path / temporary,
            operation_path / name,
            source_parent_identity=operation_identity,
            target_parent_identity=operation_identity,
            allow_regular_file=True,
        )
        os.fsync(operation)
    except FileExistsError as error:
        raise ReplicaMaterializationRejectedError("journal record already exists") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=operation)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _open_journal_operation(
    root: int,
    root_path: Path,
    operation_id: str,
    *,
    uid: int,
    gid: int,
    mount_id: int,
    create: bool,
) -> tuple[int, os.stat_result, bool] | None:
    created = False
    try:
        named_before = os.stat(operation_id, dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        named_before = None
    except OSError as error:
        raise ReplicaMaterializationUnresolvedError(
            "journal operation lookup is unresolved"
        ) from error
    if named_before is not None and (
        stat.S_ISLNK(named_before.st_mode) or not stat.S_ISDIR(named_before.st_mode)
    ):
        raise ReplicaMaterializationRejectedError("journal operation type is invalid")
    try:
        descriptor = os.open(operation_id, _directory_flags(), dir_fd=root)
    except FileNotFoundError:
        if named_before is not None:
            raise ReplicaMaterializationUnresolvedError("journal operation changed during open")
        if not create:
            return None
        created_descriptor: int | None = None
        try:
            os.mkdir(operation_id, 0o700, dir_fd=root)
            created = True
            created_descriptor = os.open(operation_id, _directory_flags(), dir_fd=root)
            os.fsync(root)
            descriptor = created_descriptor
            created_descriptor = None
        except FileExistsError as error:
            raise ReplicaMaterializationCollisionError(
                "journal operation appeared concurrently"
            ) from error
        except OSError as error:
            raise ReplicaMaterializationUnresolvedError(
                "journal operation creation is unresolved"
            ) from error
        finally:
            if created_descriptor is not None:
                os.close(created_descriptor)
    except OSError as error:
        raise ReplicaMaterializationUnresolvedError(
            "journal operation open is unresolved"
        ) from error
    try:
        metadata = _check_named_directory(
            root,
            operation_id,
            descriptor,
            uid=uid,
            gid=gid,
            mount_id=mount_id,
        )
    except BaseException:
        os.close(descriptor)
        raise
    del root_path
    return descriptor, metadata, created


def _journal_records(
    operation: int,
    *,
    uid: int,
    gid: int,
    device: int,
    mount_id: int,
) -> dict[str, dict[str, Any]]:
    try:
        names = set(os.listdir(operation))
    except OSError as error:
        raise ReplicaMaterializationUnresolvedError("journal listing is unresolved") from error
    unknown = names - set(_RECORD_NAMES)
    if unknown:
        raise ReplicaMaterializationRejectedError("journal operation contains unknown entries")
    result: dict[str, dict[str, Any]] = {}
    seen_missing = False
    for name in _RECORD_NAMES:
        record = _read_record(
            operation,
            name,
            uid=uid,
            gid=gid,
            device=device,
            mount_id=mount_id,
        )
        if record is None:
            seen_missing = True
            continue
        if seen_missing:
            raise ReplicaMaterializationRejectedError("journal record order is invalid")
        result[name] = record[0]
    return result


def _stage_value(
    descriptor: int,
    metadata: os.stat_result,
    pending_name: str,
) -> dict[str, object]:
    return {
        "record_version": 1,
        "pending_name": pending_name,
        "operation_directory_identity": _identity(metadata),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mount_identity": _mount_id(descriptor),
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
    }


def _validate_intent(value: dict[str, Any], request: ReplicaMaterializationRequest) -> str:
    if set(value) != {"record_version", "request", "request_sha256", "pending_name"}:
        raise ReplicaMaterializationRejectedError("intent record fields are invalid")
    pending = value.get("pending_name")
    if type(pending) is not str or not _PENDING.fullmatch(pending):
        raise ReplicaMaterializationRejectedError("intent pending name is invalid")
    expected = _intent_value(request, pending)
    if value != expected:
        raise ReplicaMaterializationRejectedError("materialization intent conflicts with request")
    return pending


def _validate_stage(
    value: dict[str, Any],
    pending_name: str,
    descriptor: int | None = None,
    metadata: os.stat_result | None = None,
) -> None:
    keys = {
        "record_version",
        "pending_name",
        "operation_directory_identity",
        "device",
        "inode",
        "mount_identity",
        "mode",
        "uid",
        "gid",
    }
    if set(value) != keys or value.get("pending_name") != pending_name:
        raise ReplicaMaterializationRejectedError("stage record fields are invalid")
    if (
        descriptor is not None
        and metadata is not None
        and value != _stage_value(descriptor, metadata, pending_name)
    ):
        raise ReplicaMaterializationRejectedError("staged operation identity changed")


def _open_owned_operation(
    root: int,
    name: str,
    *,
    uid: int,
    gid: int,
    mount_id: int,
    foreign_is_collision: bool,
) -> tuple[int, os.stat_result] | None:
    try:
        named_before = os.stat(name, dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ReplicaMaterializationUnresolvedError(
            "staged operation lookup is unresolved"
        ) from error
    if stat.S_ISLNK(named_before.st_mode) or not stat.S_ISDIR(named_before.st_mode):
        if foreign_is_collision:
            raise ReplicaMaterializationCollisionError(
                "foreign final operation occupies destination"
            )
        raise ReplicaMaterializationRejectedError("staged operation type is invalid")
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=root)
    except OSError as error:
        raise ReplicaMaterializationUnresolvedError(
            "staged operation open is unresolved"
        ) from error
    try:
        metadata = _check_named_directory(
            root,
            name,
            descriptor,
            uid=uid,
            gid=gid,
            mount_id=mount_id,
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, metadata


def _remove_contents(directory: int, *, uid: int, device: int, mount_id: int) -> None:
    try:
        names = sorted(os.listdir(directory), key=os.fsencode)
    except OSError as error:
        raise ReplicaMaterializationUnresolvedError("pending tree listing is unresolved") from error
    for name in names:
        raw_name = os.fsencode(name)
        try:
            metadata = os.stat(raw_name, dir_fd=directory, follow_symlinks=False)
        except OSError as error:
            raise ReplicaMaterializationUnresolvedError(
                "pending entry lookup is unresolved"
            ) from error
        if metadata.st_uid != uid or metadata.st_dev != device:
            raise ReplicaMaterializationRejectedError("pending entry identity is invalid")
        if stat.S_ISDIR(metadata.st_mode):
            try:
                child = os.open(raw_name, _directory_flags(), dir_fd=directory)
            except OSError as error:
                raise ReplicaMaterializationUnresolvedError(
                    "pending directory open is unresolved"
                ) from error
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ) or _mount_id(child) != mount_id:
                    raise ReplicaMaterializationRejectedError(
                        "pending directory mount identity changed"
                    )
                _remove_contents(child, uid=uid, device=device, mount_id=mount_id)
                os.fsync(child)
            finally:
                os.close(child)
            os.rmdir(raw_name, dir_fd=directory)
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise ReplicaMaterializationRejectedError("pending entry has multiple links")
            descriptor = os.open(raw_name, _file_flags(), dir_fd=directory)
            try:
                if _mount_id(descriptor) != mount_id:
                    raise ReplicaMaterializationRejectedError("pending file mount identity changed")
            finally:
                os.close(descriptor)
            os.unlink(raw_name, dir_fd=directory)
        elif stat.S_ISLNK(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise ReplicaMaterializationRejectedError("pending entry has multiple links")
            os.unlink(raw_name, dir_fd=directory)
        else:
            raise ReplicaMaterializationRejectedError("pending entry type is unsupported")
    os.fsync(directory)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short file write")
        offset += written


def _mkdir_open(
    parent: int,
    name: bytes,
    *,
    uid: int,
    device: int,
    mount_id: int,
) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    descriptor = os.open(name, _directory_flags(), dir_fd=parent)
    try:
        metadata = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino)
            or metadata.st_uid != uid
            or metadata.st_dev != device
            or _mount_id(descriptor) != mount_id
        ):
            raise ReplicaMaterializationRejectedError("worktree directory identity is invalid")
        os.fchmod(descriptor, 0o700)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _create_manifest_entry(
    repository: int,
    entry: ManifestEntry,
    *,
    uid: int,
    device: int,
    mount_id: int,
) -> None:
    parts = entry.raw_path.split(b"/")
    parent = os.dup(repository)
    try:
        for component in parts[:-1]:
            child = _mkdir_open(
                parent,
                component,
                uid=uid,
                device=device,
                mount_id=mount_id,
            )
            os.close(parent)
            parent = child
        leaf = parts[-1]
        if entry.kind == WorktreeEntryKind.FILE:
            mode = 0o700 if entry.executable else 0o600
            descriptor = os.open(
                leaf,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                mode,
                dir_fd=parent,
            )
            try:
                _write_all(descriptor, entry.content)
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        elif entry.kind == WorktreeEntryKind.SYMLINK:
            os.symlink(entry.content, leaf, dir_fd=parent)
        elif entry.kind == WorktreeEntryKind.DIRECTORY:
            child = _mkdir_open(
                parent,
                leaf,
                uid=uid,
                device=device,
                mount_id=mount_id,
            )
            try:
                os.fchmod(child, 0o700)
                os.fsync(child)
            finally:
                os.close(child)
        else:
            raise ReplicaMaterializationRejectedError("worktree entry kind is unsupported")
        os.fsync(parent)
    finally:
        os.close(parent)


async def _git(
    arguments: list[str],
    *,
    cwd: Path | _StableDirectory,
    stdin_descriptor: int | None = None,
    stdin_bytes: bytes | None = None,
    stdout_max: int = 1024 * 1024,
) -> bytes:
    if not arguments or arguments[0] not in _ALLOWED_GIT:
        raise ReplicaMaterializationRejectedError(
            "Git command is outside materialization allowlist"
        )
    return await run_git_bytes(
        arguments,
        cwd=cwd if isinstance(cwd, _StableDirectory) else str(cwd),
        env=dict(_GIT_ENV),
        stdin_descriptor=stdin_descriptor,
        stdin_bytes=stdin_bytes,
        stdout_bytes_max=stdout_max,
        stderr_bytes_max=1024 * 1024,
    )


def _stdin_path() -> str:
    return "/proc/self/fd/0" if Path("/proc/self/fd/0").exists() else "/dev/fd/0"


async def _import_objects(
    repository: _StableDirectory,
    opened: OpenedPublishedReplicaArtifacts,
) -> None:
    os.lseek(opened.bundle.descriptor, 0, os.SEEK_SET)
    await _git(
        ["bundle", "unbundle", _stdin_path()],
        cwd=repository,
        stdin_descriptor=opened.bundle.descriptor,
        stdout_max=opened.declaration.limits.bundle.max_listing_bytes,
    )
    os.lseek(opened.index_objects.descriptor, 0, os.SEEK_SET)
    pack_result = await _git(
        ["index-pack", "--stdin", "--strict", "--fix-thin"],
        cwd=repository,
        stdin_descriptor=opened.index_objects.descriptor,
        stdout_max=129,
    )
    oid_bytes = 20 if opened.declaration.object_format == "sha1" else 32
    expected_pack = os.pread(
        opened.index_objects.descriptor,
        oid_bytes,
        opened.index_objects.byte_length - oid_bytes,
    ).hex()
    try:
        returned_pack = pack_result.decode("ascii").strip().split()[-1]
    except (UnicodeDecodeError, IndexError) as error:
        raise ReplicaMaterializationUnresolvedError(
            "supplemental pack identity is invalid"
        ) from error
    if returned_pack != expected_pack:
        raise ReplicaMaterializationUnresolvedError("supplemental pack identity differs")


async def _verify_objects(
    repository: _StableDirectory,
    bundle: VerifiedCommittedBundle,
    supplemental: VerifiedIndexObjectPack,
) -> None:
    expected = {item.object_id: (item.kind, item.byte_length) for item in bundle.objects}
    expected.update(
        {item.object_id: (item.kind, item.byte_length) for item in supplemental.objects}
    )
    if not expected:
        return
    input_bytes = b"".join(object_id.encode("ascii") + b"\n" for object_id in sorted(expected))
    raw = await _git(
        ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        cwd=repository,
        stdin_bytes=input_bytes,
        stdout_max=max(1024, len(expected) * 160),
    )
    actual: dict[str, tuple[str, int]] = {}
    for line in raw.splitlines():
        try:
            object_raw, kind_raw, size_raw = line.split(b" ", 2)
            actual[object_raw.decode("ascii")] = (
                kind_raw.decode("ascii"),
                int(size_raw.decode("ascii")),
            )
        except (ValueError, UnicodeDecodeError) as error:
            raise ReplicaMaterializationUnresolvedError(
                "Git object inventory is invalid"
            ) from error
    if actual != expected:
        raise ReplicaMaterializationUnresolvedError("imported object inventory differs")


def _expected_refs(
    bundle: VerifiedCommittedBundle,
    worktree: VerifiedWorktreeArtifact,
) -> dict[str, str]:
    refs = {item.name: item.object_id for item in bundle.refs if item.name != "HEAD"}
    if worktree.head_state == "symbolic":
        target = worktree.head_target.decode("ascii")
        head_oid = worktree.head_oid.hex()
        current = refs.get(target)
        if current is not None and current != head_oid:
            raise ReplicaMaterializationRejectedError("symbolic HEAD conflicts with bundled ref")
        refs[target] = head_oid
    return refs


async def _restore_refs_and_head(
    repository: _StableDirectory,
    bundle: VerifiedCommittedBundle,
    worktree: VerifiedWorktreeArtifact,
) -> None:
    refs = _expected_refs(bundle, worktree)
    if refs:
        commands = bytearray(b"start\0")
        for name, object_id in sorted(refs.items()):
            commands += b"update " + name.encode("ascii") + b"\0"
            commands += object_id.encode("ascii") + b"\0\0"
        commands += b"prepare\0commit\0"
        await _git(
            ["update-ref", "--stdin", "-z", "--no-deref"],
            cwd=repository,
            stdin_bytes=bytes(commands),
            stdout_max=1024,
        )
    if worktree.head_state == "symbolic":
        await _git(
            ["symbolic-ref", "HEAD", worktree.head_target.decode("ascii")],
            cwd=repository,
            stdout_max=128,
        )
    elif worktree.head_state == "detached":
        await _git(
            ["update-ref", "--no-deref", "HEAD", worktree.head_oid.hex()],
            cwd=repository,
            stdout_max=128,
        )
    else:
        await _git(
            ["symbolic-ref", "HEAD", worktree.head_target.decode("ascii")],
            cwd=repository,
            stdout_max=128,
        )


def _write_index(repository: int, content: bytes | None) -> None:
    if content is None:
        return
    git_directory = os.open(b".git", _directory_flags(), dir_fd=repository)
    try:
        descriptor = os.open(
            b"index",
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=git_directory,
        )
        try:
            _write_all(descriptor, content)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(git_directory)
    finally:
        os.close(git_directory)


async def _verify_refs_and_head(
    repository: _StableDirectory,
    bundle: VerifiedCommittedBundle,
    worktree: VerifiedWorktreeArtifact,
) -> None:
    raw = await _git(
        ["for-each-ref", "--format=%(refname) %(objectname)"],
        cwd=repository,
        stdout_max=1024 * 1024,
    )
    actual: dict[str, str] = {}
    for line in raw.splitlines():
        try:
            name, object_id = line.decode("ascii").split(" ", 1)
        except (UnicodeDecodeError, ValueError) as error:
            raise ReplicaMaterializationUnresolvedError("Git ref inventory is invalid") from error
        actual[name] = object_id
    if actual != _expected_refs(bundle, worktree):
        raise ReplicaMaterializationUnresolvedError("materialized refs differ")
    symbolic = (
        await _git(
            ["symbolic-ref", "--quiet", "--no-recurse", "HEAD"],
            cwd=repository,
            stdout_max=1024,
        )
        if worktree.head_state != "detached"
        else b""
    )
    if worktree.head_state == "symbolic":
        if symbolic.strip() != worktree.head_target:
            raise ReplicaMaterializationUnresolvedError("symbolic HEAD differs")
        resolved = await _git(
            ["rev-parse", "--verify", "HEAD^{commit}"], cwd=repository, stdout_max=129
        )
        if resolved.decode("ascii").strip() != worktree.head_oid.hex():
            raise ReplicaMaterializationUnresolvedError("symbolic HEAD object differs")
    elif worktree.head_state == "detached":
        resolved = await _git(
            ["rev-parse", "--verify", "HEAD^{commit}"], cwd=repository, stdout_max=129
        )
        if resolved.decode("ascii").strip() != worktree.head_oid.hex():
            raise ReplicaMaterializationUnresolvedError("detached HEAD object differs")
    elif symbolic.strip() != worktree.head_target:
        raise ReplicaMaterializationUnresolvedError("unborn HEAD differs")


async def _verify_index_semantics(
    repository: _StableDirectory,
    worktree: VerifiedWorktreeArtifact,
    *,
    max_path_bytes: int,
) -> None:
    """Compare bounded raw Git index output with the verified entries."""
    if worktree.index_bytes is None:
        return
    stdout_max = 1024 + len(worktree.index_entries) * (96 + max_path_bytes)
    raw = await _git(
        ["ls-files", "--stage", "-z"],
        cwd=repository,
        stdout_max=stdout_max,
    )
    if raw and not raw.endswith(b"\x00"):
        raise ReplicaMaterializationUnresolvedError("Git index listing is truncated")
    records = raw.split(b"\x00")[:-1] if raw else []
    parsed: list[tuple[bytes, int, int, bytes]] = []
    for record in records:
        header, separator, path = record.partition(b"\t")
        fields = header.split(b" ")
        if not separator or len(fields) != 3:
            raise ReplicaMaterializationUnresolvedError("Git index listing record is invalid")
        try:
            parsed.append(
                (
                    path,
                    int(fields[2], 10),
                    int(fields[0], 8),
                    bytes.fromhex(fields[1].decode("ascii")),
                )
            )
        except (ValueError, UnicodeDecodeError) as error:
            raise ReplicaMaterializationUnresolvedError(
                "Git index listing record is invalid"
            ) from error
    expected = [
        (entry.raw_path, entry.stage, entry.mode, entry.object_id)
        for entry in worktree.index_entries
    ]
    if parsed != expected:
        raise ReplicaMaterializationUnresolvedError("materialized index semantics differ")


def _normalize_git_tree(
    directory: int,
    *,
    uid: int,
    device: int,
    mount_id: int,
) -> None:
    for name in sorted(os.listdir(directory), key=os.fsencode):
        raw = os.fsencode(name)
        metadata = os.stat(raw, dir_fd=directory, follow_symlinks=False)
        if metadata.st_uid != uid or metadata.st_dev != device:
            raise ReplicaMaterializationRejectedError("Git entry identity is invalid")
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(raw, _directory_flags(), dir_fd=directory)
            try:
                if _mount_id(child) != mount_id:
                    raise ReplicaMaterializationRejectedError(
                        "Git directory mount identity changed"
                    )
                _normalize_git_tree(
                    child,
                    uid=uid,
                    device=device,
                    mount_id=mount_id,
                )
                os.fchmod(child, 0o700)
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise ReplicaMaterializationRejectedError("Git file has multiple links")
            descriptor = os.open(raw, _file_flags(), dir_fd=directory)
            try:
                if _mount_id(descriptor) != mount_id:
                    raise ReplicaMaterializationRejectedError("Git file mount identity changed")
                os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode) & 0o700 or 0o400)
            finally:
                os.close(descriptor)
        elif stat.S_ISLNK(metadata.st_mode):
            raise ReplicaMaterializationRejectedError("Git metadata symbolic link is forbidden")
        else:
            raise ReplicaMaterializationRejectedError("Git entry type is unsupported")


def _git_tree_inventory(
    directory: int,
    *,
    uid: int,
    device: int,
    mount_id: int,
    prefix: bytes = b"",
) -> tuple[set[bytes], set[bytes]]:
    files: set[bytes] = set()
    directories: set[bytes] = set()
    for name in sorted(os.listdir(directory), key=os.fsencode):
        raw = os.fsencode(name)
        path = raw if not prefix else prefix + b"/" + raw
        metadata = os.stat(raw, dir_fd=directory, follow_symlinks=False)
        if metadata.st_uid != uid or metadata.st_dev != device:
            raise ReplicaMaterializationRejectedError("Git entry identity is invalid")
        if raw.endswith(b".lock"):
            raise ReplicaMaterializationRejectedError("Git lock file is forbidden")
        if stat.S_ISDIR(metadata.st_mode):
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                raise ReplicaMaterializationRejectedError("Git directory mode is unsafe")
            child = os.open(raw, _directory_flags(), dir_fd=directory)
            try:
                if _mount_id(child) != mount_id:
                    raise ReplicaMaterializationRejectedError(
                        "Git directory mount identity changed"
                    )
                directories.add(path)
                child_files, child_directories = _git_tree_inventory(
                    child,
                    uid=uid,
                    device=device,
                    mount_id=mount_id,
                    prefix=path,
                )
                files.update(child_files)
                directories.update(child_directories)
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) not in {
                0o400,
                0o500,
                0o600,
                0o700,
            }:
                raise ReplicaMaterializationRejectedError("Git file mode is unsafe")
            descriptor = os.open(raw, _file_flags(), dir_fd=directory)
            try:
                if _mount_id(descriptor) != mount_id:
                    raise ReplicaMaterializationRejectedError("Git file mount identity changed")
            finally:
                os.close(descriptor)
            files.add(path)
        elif stat.S_ISLNK(metadata.st_mode):
            raise ReplicaMaterializationRejectedError("Git metadata symbolic link is forbidden")
        else:
            raise ReplicaMaterializationRejectedError("Git entry type is unsupported")
    return files, directories


def _validate_git_topology(
    git_directory: int,
    bundle: VerifiedCommittedBundle,
    worktree: VerifiedWorktreeArtifact,
    *,
    uid: int,
    device: int,
    mount_id: int,
) -> None:
    files, directories = _git_tree_inventory(
        git_directory,
        uid=uid,
        device=device,
        mount_id=mount_id,
    )
    required_top = {b"HEAD", b"config"}
    if worktree.index_bytes is not None:
        required_top.add(b"index")
    top_files = {path for path in files if b"/" not in path}
    if top_files != required_top:
        raise ReplicaMaterializationRejectedError("Git top-level files are invalid")
    if any(path.split(b"/", 1)[0] not in {b"objects", b"refs"} for path in files - top_files):
        raise ReplicaMaterializationRejectedError("Git metadata file is outside policy")

    expected_refs = set(_expected_refs(bundle, worktree))
    actual_refs = {path.decode("ascii") for path in files if path.startswith(b"refs/")}
    if actual_refs != expected_refs:
        raise ReplicaMaterializationRejectedError("Git loose ref topology is invalid")

    object_hex_length = 40 if worktree.object_format == "sha1" else 64
    pack_parts: dict[bytes, set[bytes]] = {}
    object_files = {path for path in files if path.startswith(b"objects/")}
    for path in object_files:
        if path.startswith(b"objects/pack/"):
            leaf = path.removeprefix(b"objects/pack/")
            match = re.fullmatch(
                rb"(pack-[0-9a-f]{"
                + str(object_hex_length).encode("ascii")
                + rb"})(\.(?:pack|idx))",
                leaf,
            )
            if match is None:
                raise ReplicaMaterializationRejectedError("Git pack file name is invalid")
            pack_parts.setdefault(match.group(1), set()).add(match.group(2))
            continue
        match = re.fullmatch(
            rb"objects/([0-9a-f]{2})/([0-9a-f]{"
            + str(object_hex_length - 2).encode("ascii")
            + rb"})",
            path,
        )
        if match is None:
            raise ReplicaMaterializationRejectedError("Git object path is invalid")
    if any(parts != {b".pack", b".idx"} for parts in pack_parts.values()):
        raise ReplicaMaterializationRejectedError("Git pack pair is incomplete")

    allowed_directories = {
        b"objects",
        b"objects/info",
        b"objects/pack",
        b"refs",
        b"refs/heads",
        b"refs/tags",
    }
    for path in files:
        parts = path.split(b"/")
        for index in range(1, len(parts)):
            allowed_directories.add(b"/".join(parts[:index]))
    if not directories.issubset(allowed_directories):
        raise ReplicaMaterializationRejectedError("Git directory topology is invalid")


def _scan_worktree(
    directory: int,
    *,
    uid: int,
    device: int,
    mount_id: int,
    prefix: bytes = b"",
) -> tuple[dict[bytes, tuple[str, bool, bytes]], set[bytes]]:
    leaves: dict[bytes, tuple[str, bool, bytes]] = {}
    directories: set[bytes] = set()
    for name in sorted(os.listdir(directory), key=os.fsencode):
        raw = os.fsencode(name)
        if not prefix and raw == b".git":
            continue
        path = raw if not prefix else prefix + b"/" + raw
        metadata = os.stat(raw, dir_fd=directory, follow_symlinks=False)
        if metadata.st_uid != uid or metadata.st_dev != device:
            raise ReplicaMaterializationRejectedError("worktree entry ownership is invalid")
        if stat.S_ISDIR(metadata.st_mode):
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                raise ReplicaMaterializationRejectedError("worktree directory mode is unsafe")
            child = os.open(raw, _directory_flags(), dir_fd=directory)
            try:
                directories.add(path)
                if _mount_id(child) != mount_id:
                    raise ReplicaMaterializationRejectedError(
                        "worktree directory mount identity changed"
                    )
                child_leaves, child_directories = _scan_worktree(
                    child,
                    uid=uid,
                    device=device,
                    mount_id=mount_id,
                    prefix=path,
                )
                leaves.update(child_leaves)
                directories.update(child_directories)
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) not in {
                0o600,
                0o700,
            }:
                raise ReplicaMaterializationRejectedError("worktree file mode is unsafe")
            descriptor = os.open(raw, _file_flags(), dir_fd=directory)
            try:
                if _mount_id(descriptor) != mount_id:
                    raise ReplicaMaterializationRejectedError(
                        "worktree file mount identity changed"
                    )
                content = bytearray()
                while True:
                    chunk = os.read(descriptor, 65536)
                    if not chunk:
                        break
                    content.extend(chunk)
            finally:
                os.close(descriptor)
            leaves[path] = (
                "file",
                bool(stat.S_IMODE(metadata.st_mode) & 0o111),
                bytes(content),
            )
        elif stat.S_ISLNK(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise ReplicaMaterializationRejectedError(
                    "worktree symbolic link has multiple links"
                )
            target = os.readlink(raw, dir_fd=directory)
            leaves[path] = ("symlink", False, os.fsencode(target))
        else:
            raise ReplicaMaterializationRejectedError("worktree entry type is unsupported")
    return leaves, directories


def _verify_worktree(
    repository: int,
    worktree: VerifiedWorktreeArtifact,
    *,
    uid: int,
    device: int,
    mount_id: int,
) -> None:
    expected: dict[bytes, tuple[str, bool, bytes]] = {}
    expected_directories: set[bytes] = set()
    for entry in worktree.entries:
        if entry.kind == WorktreeEntryKind.FILE:
            expected[entry.raw_path] = ("file", entry.executable, entry.content)
        elif entry.kind == WorktreeEntryKind.SYMLINK:
            expected[entry.raw_path] = ("symlink", False, entry.content)
        else:
            expected_directories.add(entry.raw_path)
        parts = entry.raw_path.split(b"/")
        for index in range(1, len(parts)):
            expected_directories.add(b"/".join(parts[:index]))
    actual, actual_directories = _scan_worktree(
        repository,
        uid=uid,
        device=device,
        mount_id=mount_id,
    )
    if actual != expected or actual_directories != expected_directories:
        raise ReplicaMaterializationUnresolvedError("materialized worktree differs")
    git_directory = os.open(b".git", _directory_flags(), dir_fd=repository)
    try:
        if _mount_id(git_directory) != mount_id:
            raise ReplicaMaterializationRejectedError("Git directory mount identity changed")
        record = None
        try:
            descriptor = os.open(b"index", _file_flags(), dir_fd=git_directory)
        except FileNotFoundError:
            descriptor = None
        if descriptor is not None:
            try:
                if _mount_id(descriptor) != mount_id:
                    raise ReplicaMaterializationRejectedError("Git index mount identity changed")
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                record = b"".join(chunks)
            finally:
                os.close(descriptor)
        if record != worktree.index_bytes:
            raise ReplicaMaterializationUnresolvedError("materialized index bytes differ")
    finally:
        os.close(git_directory)


async def _build_pending(
    pending_path: Path,
    pending: int,
    opened: OpenedPublishedReplicaArtifacts,
    *,
    uid: int,
    mount_id: int,
) -> None:
    metadata = os.fstat(pending)
    if _mount_id(pending) != mount_id:
        raise ReplicaMaterializationRejectedError("pending operation mount identity changed")
    _remove_contents(
        pending,
        uid=uid,
        device=metadata.st_dev,
        mount_id=mount_id,
    )
    os.mkdir("repo", 0o700, dir_fd=pending)
    os.mkdir("home", 0o700, dir_fd=pending)
    template_name = f".template.{secrets.token_hex(16)}"
    os.mkdir(template_name, 0o700, dir_fd=pending)
    os.fsync(pending)
    repository_path = pending_path / "repo"
    pending_stable = _StableDirectory(str(pending_path), pending)
    try:
        await _git(
            [
                "init",
                "--quiet",
                f"--object-format={opened.declaration.object_format}",
                f"--template={pending_path / template_name}",
                "repo",
            ],
            cwd=pending_stable,
            stdout_max=1024,
        )
    finally:
        try:
            os.rmdir(template_name, dir_fd=pending)
            os.fsync(pending)
        except FileNotFoundError:
            pass
    repository = os.open("repo", _directory_flags(), dir_fd=pending)
    try:
        if _mount_id(repository) != mount_id:
            raise ReplicaMaterializationRejectedError(
                "materialized repository mount identity changed"
            )
        repository_stable = _StableDirectory(str(repository_path), repository)
        await _import_objects(repository_stable, opened)
        await _verify_objects(
            repository_stable,
            opened.verified_bundle,
            opened.verified_index_objects,
        )
        await _restore_refs_and_head(
            repository_stable,
            opened.verified_bundle,
            opened.verified_worktree,
        )
        _write_index(repository, opened.verified_worktree.index_bytes)
        for entry in opened.verified_worktree.entries:
            _create_manifest_entry(
                repository,
                entry,
                uid=uid,
                device=metadata.st_dev,
                mount_id=mount_id,
            )
        await _verify_refs_and_head(
            repository_stable,
            opened.verified_bundle,
            opened.verified_worktree,
        )
        await _verify_objects(
            repository_stable,
            opened.verified_bundle,
            opened.verified_index_objects,
        )
        await _verify_index_semantics(
            repository_stable,
            opened.verified_worktree,
            max_path_bytes=opened.declaration.limits.worktree.max_path_bytes,
        )
        _verify_worktree(
            repository,
            opened.verified_worktree,
            uid=uid,
            device=metadata.st_dev,
            mount_id=mount_id,
        )
        git_directory = os.open(".git", _directory_flags(), dir_fd=repository)
        try:
            if _mount_id(git_directory) != mount_id:
                raise ReplicaMaterializationRejectedError("Git directory mount identity changed")
            _normalize_git_tree(
                git_directory,
                uid=uid,
                device=metadata.st_dev,
                mount_id=mount_id,
            )
            _validate_git_topology(
                git_directory,
                opened.verified_bundle,
                opened.verified_worktree,
                uid=uid,
                device=metadata.st_dev,
                mount_id=mount_id,
            )
            os.fchmod(git_directory, 0o700)
        finally:
            os.close(git_directory)
        os.fchmod(repository, 0o700)
    finally:
        os.close(repository)
    home = os.open("home", _directory_flags(), dir_fd=pending)
    try:
        if _mount_id(home) != mount_id:
            raise ReplicaMaterializationRejectedError("materialized home mount identity changed")
        if os.listdir(home):
            raise ReplicaMaterializationRejectedError("materialized home is not empty")
        os.fchmod(home, 0o700)
        os.fsync(home)
    finally:
        os.close(home)
    if set(os.listdir(pending)) != {"repo", "home"}:
        raise ReplicaMaterializationRejectedError("materialized operation entries are invalid")
    os.fchmod(pending, 0o700)


def _sync_tree_digest(
    directory: int,
    *,
    uid: int,
    gid: int,
    device: int,
    mount_id: int,
    allowed_symlinks: dict[bytes, bytes],
    prefix: bytes = b"",
) -> str:
    if _mount_id(directory) != mount_id:
        raise ReplicaMaterializationRejectedError("materialized directory mount identity changed")
    records: list[dict[str, object]] = []

    def visit(current: int, current_prefix: bytes) -> None:
        for name in sorted(os.listdir(current), key=os.fsencode):
            raw = os.fsencode(name)
            path = raw if not current_prefix else current_prefix + b"/" + raw
            before = os.stat(raw, dir_fd=current, follow_symlinks=False)
            if before.st_uid != uid or before.st_gid != gid or before.st_dev != device:
                raise ReplicaMaterializationRejectedError("materialized entry ownership is invalid")
            mode = stat.S_IMODE(before.st_mode)
            if not stat.S_ISLNK(before.st_mode) and mode not in {
                0o400,
                0o500,
                0o600,
                0o700,
            }:
                raise ReplicaMaterializationRejectedError("materialized entry mode is unsafe")
            common: dict[str, object] = {
                "path": base64.b64encode(path).decode("ascii"),
                "mode": stat.S_IMODE(before.st_mode),
                "uid": before.st_uid,
                "gid": before.st_gid,
                "links": before.st_nlink,
            }
            if stat.S_ISDIR(before.st_mode):
                descriptor = os.open(raw, _directory_flags(), dir_fd=current)
                try:
                    opened = os.fstat(descriptor)
                    if (opened.st_dev, opened.st_ino) != (
                        before.st_dev,
                        before.st_ino,
                    ) or _mount_id(descriptor) != mount_id:
                        raise ReplicaMaterializationRejectedError(
                            "materialized directory mount identity changed"
                        )
                    if stat.S_IMODE(opened.st_mode) != 0o700:
                        raise ReplicaMaterializationRejectedError(
                            "materialized directory mode is unsafe"
                        )
                    visit(descriptor, path)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                common["kind"] = "directory"
                records.append(common)
            elif stat.S_ISREG(before.st_mode):
                if before.st_nlink != 1:
                    raise ReplicaMaterializationRejectedError(
                        "materialized file link count is unsafe"
                    )
                descriptor = os.open(raw, _file_flags(), dir_fd=current)
                try:
                    if _mount_id(descriptor) != mount_id:
                        raise ReplicaMaterializationRejectedError(
                            "materialized file mount identity changed"
                        )
                    digest = hashlib.sha256()
                    length = 0
                    while True:
                        chunk = os.read(descriptor, 65536)
                        if not chunk:
                            break
                        digest.update(chunk)
                        length += len(chunk)
                    os.fsync(descriptor)
                    after = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                if length != before.st_size or (
                    before.st_dev,
                    before.st_ino,
                    before.st_mtime_ns,
                ) != (after.st_dev, after.st_ino, after.st_mtime_ns):
                    raise ReplicaMaterializationRejectedError(
                        "materialized file changed during sync"
                    )
                common.update(kind="file", length=length, sha256=digest.hexdigest())
                records.append(common)
            elif stat.S_ISLNK(before.st_mode):
                if before.st_nlink != 1:
                    raise ReplicaMaterializationRejectedError(
                        "materialized symbolic link has multiple links"
                    )
                target = os.fsencode(os.readlink(raw, dir_fd=current))
                if path not in allowed_symlinks or allowed_symlinks[path] != target:
                    raise ReplicaMaterializationRejectedError(
                        "undeclared materialized symbolic link is forbidden"
                    )
                common.update(
                    kind="symlink",
                    length=len(target),
                    target=base64.b64encode(target).decode("ascii"),
                )
                records.append(common)
            else:
                raise ReplicaMaterializationRejectedError("materialized entry type is unsupported")

    visit(directory, prefix)
    os.fsync(directory)
    records.sort(key=lambda value: str(value["path"]))
    return _domain_digest(_TREE_DOMAIN, records)


def _declared_destination_symlinks(
    worktree: VerifiedWorktreeArtifact,
) -> dict[bytes, bytes]:
    return {
        b"repo/" + entry.raw_path: entry.content
        for entry in worktree.entries
        if entry.kind == WorktreeEntryKind.SYMLINK
    }


async def _complete_despite_cancellation(
    effect: Coroutine[Any, Any, _T],
) -> _T:
    """Drain one effect and let its failure take priority over cancellation."""
    task = asyncio.create_task(effect)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if cancelled is None:
                cancelled = error
    result = task.result()
    if cancelled is not None:
        raise cancelled
    return result


def _reverify_named_destination(
    *,
    staging: int,
    staging_root: Path,
    staging_root_identity: str,
    final_descriptor: int,
    final_metadata: os.stat_result,
    operation_id: str,
    repository: int,
    repository_metadata: os.stat_result,
    home: int,
    home_metadata: os.stat_result,
    uid: int,
    gid: int,
    mount_id: int,
    allowed_symlinks: dict[bytes, bytes],
    expected_tree_sha256: str,
) -> None:
    root_metadata = os.fstat(staging)
    try:
        named_root = os.lstat(staging_root)
        named_final = os.stat(operation_id, dir_fd=staging, follow_symlinks=False)
    except OSError as error:
        raise ReplicaMaterializationUnresolvedError(
            "materialized destination recheck is unresolved"
        ) from error
    if (
        _identity(root_metadata) != staging_root_identity
        or (named_root.st_dev, named_root.st_ino) != (root_metadata.st_dev, root_metadata.st_ino)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != uid
        or root_metadata.st_gid != gid
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
        or _mount_id(staging) != mount_id
    ):
        raise ReplicaMaterializationRejectedError("staging root identity changed")
    current_final = os.fstat(final_descriptor)
    if (
        (named_final.st_dev, named_final.st_ino) != (final_metadata.st_dev, final_metadata.st_ino)
        or (current_final.st_dev, current_final.st_ino) != (named_final.st_dev, named_final.st_ino)
        or not stat.S_ISDIR(named_final.st_mode)
        or named_final.st_uid != uid
        or named_final.st_gid != gid
        or named_final.st_dev != root_metadata.st_dev
        or stat.S_IMODE(named_final.st_mode) != 0o700
        or _mount_id(final_descriptor) != mount_id
    ):
        raise ReplicaMaterializationRejectedError("materialized operation was replaced")
    if set(os.listdir(final_descriptor)) != {"repo", "home"}:
        raise ReplicaMaterializationRejectedError("final operation entries are invalid")
    for name, descriptor, held in (
        ("repo", repository, repository_metadata),
        ("home", home, home_metadata),
    ):
        try:
            named = os.stat(name, dir_fd=final_descriptor, follow_symlinks=False)
        except OSError as error:
            raise ReplicaMaterializationUnresolvedError(
                f"materialized {name} recheck is unresolved"
            ) from error
        current = os.fstat(descriptor)
        if (
            (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino)
            or (current.st_dev, current.st_ino) != (named.st_dev, named.st_ino)
            or not stat.S_ISDIR(named.st_mode)
            or stat.S_IMODE(named.st_mode) != 0o700
            or named.st_uid != uid
            or named.st_gid != gid
            or named.st_dev != current_final.st_dev
            or _mount_id(descriptor) != mount_id
        ):
            raise ReplicaMaterializationRejectedError(f"materialized {name} identity is invalid")
    if os.listdir(home):
        raise ReplicaMaterializationRejectedError("final home is not empty")
    tree_sha256 = _sync_tree_digest(
        final_descriptor,
        uid=uid,
        gid=gid,
        device=current_final.st_dev,
        mount_id=mount_id,
        allowed_symlinks=allowed_symlinks,
    )
    if tree_sha256 != expected_tree_sha256:
        raise ReplicaMaterializationRejectedError("final staging digest differs after recheck")


def _prepared_value(
    request: ReplicaMaterializationRequest,
    stage: dict[str, Any],
    tree_sha256: str,
) -> dict[str, object]:
    return {
        "record_version": 1,
        "request_sha256": _domain_digest(_REQUEST_DOMAIN, _request_binding(request)),
        "publication_receipt_id": request.publication.publication_receipt_id,
        "verification_receipt_id": request.publication.verification.verification_receipt_id,
        "source_state_sha256": request.declaration.source_state_sha256,
        "operation_directory_identity": stage["operation_directory_identity"],
        "staging_tree_sha256": tree_sha256,
    }


def _make_receipt(
    request: ReplicaMaterializationRequest,
    tree_sha256: str,
    operation: os.stat_result,
    repository: os.stat_result,
    home: os.stat_result,
) -> ReplicaMaterializationReceipt:
    values = {
        "version": 1,
        "operation_id": request.declaration.operation_id,
        "artifact_set_sha256": request.publication.artifact_set_sha256,
        "publication_receipt_id": request.publication.publication_receipt_id,
        "verification_receipt_id": request.publication.verification.verification_receipt_id,
        "source_state_sha256": request.declaration.source_state_sha256,
        "limits_sha256": _request_binding(request)["limits_sha256"],
        "staging_tree_sha256": tree_sha256,
        "operation_directory_identity": _identity(operation),
        "repo_directory_identity": _identity(repository),
        "home_directory_identity": _identity(home),
    }
    return ReplicaMaterializationReceipt(
        materialization_receipt_id=_domain_digest(_RECEIPT_DOMAIN, values),
        **values,  # type: ignore[arg-type]
    )


def _parse_receipt(value: dict[str, Any]) -> ReplicaMaterializationReceipt:
    fields = {
        "version",
        "materialization_receipt_id",
        "operation_id",
        "artifact_set_sha256",
        "publication_receipt_id",
        "verification_receipt_id",
        "source_state_sha256",
        "limits_sha256",
        "staging_tree_sha256",
        "operation_directory_identity",
        "repo_directory_identity",
        "home_directory_identity",
    }
    if set(value) != fields:
        raise ReplicaMaterializationRejectedError("receipt record fields are invalid")
    try:
        receipt = ReplicaMaterializationReceipt(**value)
    except TypeError as error:
        raise ReplicaMaterializationRejectedError("receipt record is invalid") from error
    if type(receipt.version) is not int or receipt.version != 1:
        raise ReplicaMaterializationRejectedError("receipt version is invalid")
    for digest in (
        receipt.materialization_receipt_id,
        receipt.artifact_set_sha256,
        receipt.publication_receipt_id,
        receipt.verification_receipt_id,
        receipt.source_state_sha256,
        receipt.limits_sha256,
        receipt.staging_tree_sha256,
    ):
        if type(digest) is not str or not _SHA256.fullmatch(digest):
            raise ReplicaMaterializationRejectedError("receipt digest is invalid")
    values = asdict(receipt)
    values.pop("materialization_receipt_id")
    if receipt.materialization_receipt_id != _domain_digest(_RECEIPT_DOMAIN, values):
        raise ReplicaMaterializationRejectedError("receipt identifier is invalid")
    return receipt


class ReplicaMaterializationStore:
    """Own durable journal state and exact launcher staging materialization."""

    def __init__(
        self,
        *,
        publication: WorkspaceReplicaPublicationStore,
        staging_root: Path,
        journal_root: Path,
        limits: ReplicaStoreLimits,
        expected_uid: int,
        expected_gid: int,
    ) -> None:
        if type(publication) is not WorkspaceReplicaPublicationStore:
            raise TypeError("publication must be WorkspaceReplicaPublicationStore")
        if not isinstance(staging_root, Path) or not isinstance(journal_root, Path):
            raise TypeError("materialization roots must be Path values")
        if not staging_root.is_absolute() or not journal_root.is_absolute():
            raise ValueError("materialization roots must be absolute")
        if type(limits) is not ReplicaStoreLimits:
            raise TypeError("limits must be ReplicaStoreLimits")
        if type(expected_uid) is not int or type(expected_gid) is not int:
            raise TypeError("expected ownership must use integers")
        if expected_uid != os.geteuid() or expected_gid != os.getegid():
            raise ValueError("materialization store must run as expected owner")
        if publication.limits != limits:
            raise ValueError("materialization limits differ from publication limits")
        descriptors: list[int] = []
        try:
            staging_descriptor, staging_metadata, staging_mount_id = _open_private_root(
                staging_root, expected_uid, expected_gid
            )
            descriptors.append(staging_descriptor)
            journal_descriptor, journal_metadata, journal_mount_id = _open_private_root(
                journal_root, expected_uid, expected_gid
            )
            descriptors.append(journal_descriptor)
            publication_root = getattr(publication, "_root", None)
            if not isinstance(publication_root, Path):
                raise TypeError("publication root authority is unavailable")
            publication_descriptor, publication_metadata, _ = _open_private_root(
                publication_root, expected_uid, expected_gid
            )
            descriptors.append(publication_descriptor)
            identities = {
                (staging_metadata.st_dev, staging_metadata.st_ino),
                (journal_metadata.st_dev, journal_metadata.st_ino),
                (publication_metadata.st_dev, publication_metadata.st_ino),
            }
            if len(identities) != 3:
                raise ValueError("publication, staging, and journal roots must be distinct")
            require_atomic_no_replace_support()
        except WorkspacePublicationError as error:
            raise ReplicaMaterializationRejectedError(
                "atomic no-replace materialization is unsupported"
            ) from error
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        self._publication = publication
        self._staging_root = staging_root
        self._journal_root = journal_root
        self._staging_root_identity = _identity(staging_metadata)
        self._journal_root_identity = _identity(journal_metadata)
        self._staging_mount_id = staging_mount_id
        self._journal_mount_id = journal_mount_id
        self._limits = limits
        self._expected_uid = expected_uid
        self._expected_gid = expected_gid
        self._lock = asyncio.Lock()

    async def materialize(
        self, request: ReplicaMaterializationRequest
    ) -> ReplicaMaterializationReceipt:
        """Establish durable intent, build exact trees, then publish one receipt."""
        request = _validate_request_shape(request)
        receipt = await self._run(request, allow_create=True)
        if receipt is None:
            raise ReplicaMaterializationUnresolvedError("materialization lost durable state")
        return receipt

    async def reconcile(
        self, request: ReplicaMaterializationRequest
    ) -> ReplicaMaterializationReceipt | None:
        """Continue only an existing exact durable intent."""
        request = _validate_request_shape(request)
        return await self._run(request, allow_create=False)

    async def _run(
        self,
        request: ReplicaMaterializationRequest,
        *,
        allow_create: bool,
    ) -> ReplicaMaterializationReceipt | None:
        durable_intent = False
        try:
            async with self._lock:
                journal, journal_metadata, journal_mount_id = _open_private_root(
                    self._journal_root, self._expected_uid, self._expected_gid
                )
                try:
                    staging, staging_metadata, staging_mount_id = _open_private_root(
                        self._staging_root, self._expected_uid, self._expected_gid
                    )
                    try:
                        if (
                            _identity(journal_metadata) != self._journal_root_identity
                            or _identity(staging_metadata) != self._staging_root_identity
                            or journal_mount_id != self._journal_mount_id
                            or staging_mount_id != self._staging_mount_id
                        ):
                            raise ReplicaMaterializationRejectedError(
                                "materialization root identity changed"
                            )
                        with _exclusive_flock(journal):
                            result, durable_intent = await self._run_locked(
                                request,
                                allow_create=allow_create,
                                journal=journal,
                                staging=staging,
                                staging_metadata=staging_metadata,
                            )
                            return result
                    finally:
                        os.close(staging)
                finally:
                    os.close(journal)
        except asyncio.CancelledError as error:
            if durable_intent or self._intent_exists(request.declaration.operation_id):
                raise ReplicaMaterializationUnresolvedError(
                    "materialization was cancelled after durable intent"
                ) from error
            raise
        except (
            ReplicaMaterializationRejectedError,
            ReplicaMaterializationUnresolvedError,
        ):
            raise
        except ReplicaPublicationCollisionError as error:
            raise ReplicaMaterializationCollisionError("source publication collided") from error
        except ReplicaPublicationRejectedError as error:
            raise ReplicaMaterializationRejectedError("source publication was rejected") from error
        except ReplicaPublicationUnresolvedError as error:
            raise ReplicaMaterializationUnresolvedError(
                "source publication is unresolved"
            ) from error
        except (GitError, WorkspacePublicationError, OSError) as error:
            raise ReplicaMaterializationUnresolvedError(
                "materialization operation is unresolved"
            ) from error
        except Exception as error:
            raise ReplicaMaterializationUnresolvedError(
                "materialization operation is unresolved"
            ) from error

    def _intent_exists(self, operation_id: str) -> bool:
        try:
            root, _metadata, mount_id = _open_private_root(
                self._journal_root, self._expected_uid, self._expected_gid
            )
            try:
                operation = _open_journal_operation(
                    root,
                    self._journal_root,
                    operation_id,
                    uid=self._expected_uid,
                    gid=self._expected_gid,
                    mount_id=mount_id,
                    create=False,
                )
                if operation is None:
                    return False
                descriptor = operation[0]
                try:
                    return (
                        _read_record(
                            descriptor,
                            "intent.json",
                            uid=self._expected_uid,
                            gid=self._expected_gid,
                            device=operation[1].st_dev,
                            mount_id=mount_id,
                        )
                        is not None
                    )
                finally:
                    os.close(descriptor)
            finally:
                os.close(root)
        except Exception:  # noqa: BLE001 - uncertainty after cancellation fails closed
            return True

    async def _run_locked(
        self,
        request: ReplicaMaterializationRequest,
        *,
        allow_create: bool,
        journal: int,
        staging: int,
        staging_metadata: os.stat_result,
    ) -> tuple[ReplicaMaterializationReceipt | None, bool]:
        operation_id = request.declaration.operation_id
        operation_state = _open_journal_operation(
            journal,
            self._journal_root,
            operation_id,
            uid=self._expected_uid,
            gid=self._expected_gid,
            mount_id=self._journal_mount_id,
            create=False,
        )
        final_before = _open_owned_operation(
            staging,
            operation_id,
            uid=self._expected_uid,
            gid=self._expected_gid,
            mount_id=self._staging_mount_id,
            foreign_is_collision=True,
        )
        if final_before is not None:
            os.close(final_before[0])
        if operation_state is None:
            if final_before is not None:
                raise ReplicaMaterializationCollisionError(
                    "unjournaled final operation already exists"
                )
            if not allow_create:
                return None, False
            async with self._publication.open_published_artifact_set(
                request.declaration, request.publication
            ) as opened:
                _validate_source(opened)
                operation_state = _open_journal_operation(
                    journal,
                    self._journal_root,
                    operation_id,
                    uid=self._expected_uid,
                    gid=self._expected_gid,
                    mount_id=self._journal_mount_id,
                    create=True,
                )
                if operation_state is None:
                    raise ReplicaMaterializationUnresolvedError(
                        "journal operation creation was lost"
                    )
                return await self._continue(
                    request,
                    opened,
                    journal=journal,
                    staging=staging,
                    staging_metadata=staging_metadata,
                    operation_state=operation_state,
                    create_intent=True,
                )
        existing_records = _journal_records(
            operation_state[0],
            uid=self._expected_uid,
            gid=self._expected_gid,
            device=operation_state[1].st_dev,
            mount_id=self._journal_mount_id,
        )
        if not existing_records and not allow_create:
            os.close(operation_state[0])
            return None, False
        async with self._publication.open_published_artifact_set(
            request.declaration, request.publication
        ) as opened:
            _validate_source(opened)
            return await self._continue(
                request,
                opened,
                journal=journal,
                staging=staging,
                staging_metadata=staging_metadata,
                operation_state=operation_state,
                create_intent=not existing_records,
            )

    async def _continue(
        self,
        request: ReplicaMaterializationRequest,
        opened: OpenedPublishedReplicaArtifacts,
        *,
        journal: int,
        staging: int,
        staging_metadata: os.stat_result,
        operation_state: tuple[int, os.stat_result, bool],
        create_intent: bool,
    ) -> tuple[ReplicaMaterializationReceipt, bool]:
        operation, operation_metadata, _created = operation_state
        operation_path = self._journal_root / request.declaration.operation_id
        operation_identity = _identity(operation_metadata)
        durable_intent = False
        declared_symlinks = _declared_destination_symlinks(opened.verified_worktree)
        try:
            records = _journal_records(
                operation,
                uid=self._expected_uid,
                gid=self._expected_gid,
                device=operation_metadata.st_dev,
                mount_id=self._journal_mount_id,
            )
            if "intent.json" not in records:
                if records:
                    raise ReplicaMaterializationRejectedError(
                        "journal has state without materialization intent"
                    )
                if not create_intent:
                    raise ReplicaMaterializationRejectedError(
                        "journal operation lacks durable intent"
                    )
                pending_name = (
                    f".{request.declaration.operation_id}.pending.{secrets.token_hex(16)}"
                )
                intent = _intent_value(request, pending_name)
                _publish_record(
                    operation_path,
                    operation,
                    "intent.json",
                    intent,
                    operation_identity=operation_identity,
                )
                records["intent.json"] = intent
                durable_intent = True
            else:
                pending_name = _validate_intent(records["intent.json"], request)
                durable_intent = True
            pending_name = _validate_intent(records["intent.json"], request)
            stage = records.get("stage.json")
            pending = _open_owned_operation(
                staging,
                pending_name,
                uid=self._expected_uid,
                gid=self._expected_gid,
                mount_id=self._staging_mount_id,
                foreign_is_collision=False,
            )
            final = _open_owned_operation(
                staging,
                request.declaration.operation_id,
                uid=self._expected_uid,
                gid=self._expected_gid,
                mount_id=self._staging_mount_id,
                foreign_is_collision=True,
            )
            if pending is not None and final is not None:
                os.close(pending[0])
                os.close(final[0])
                raise ReplicaMaterializationRejectedError("pending and final operation both exist")
            if stage is None:
                if final is not None:
                    os.close(final[0])
                    raise ReplicaMaterializationCollisionError(
                        "final operation lacks staged identity"
                    )
                if pending is not None:
                    os.close(pending[0])
                    raise ReplicaMaterializationUnresolvedError(
                        "pending operation lacks durable staged identity"
                    )
                os.mkdir(pending_name, 0o700, dir_fd=staging)
                pending = _open_owned_operation(
                    staging,
                    pending_name,
                    uid=self._expected_uid,
                    gid=self._expected_gid,
                    mount_id=self._staging_mount_id,
                    foreign_is_collision=False,
                )
                if pending is None:
                    raise ReplicaMaterializationUnresolvedError(
                        "pending operation creation was lost"
                    )
                os.fsync(staging)
                stage = _stage_value(pending[0], pending[1], pending_name)
                _publish_record(
                    operation_path,
                    operation,
                    "stage.json",
                    stage,
                    operation_identity=operation_identity,
                )
                records["stage.json"] = stage
            else:
                _validate_stage(stage, pending_name)
            prepared = records.get("prepared.json")
            receipt_value = records.get("receipt.json")
            if final is not None:
                if pending is not None:
                    os.close(pending[0])
                return await self._finish_final(
                    request,
                    opened,
                    staging=staging,
                    staging_metadata=staging_metadata,
                    operation=operation,
                    operation_identity=operation_identity,
                    operation_path=operation_path,
                    stage=stage,
                    prepared=prepared,
                    receipt_value=receipt_value,
                    final=final,
                    durable_intent=durable_intent,
                )
            if pending is None:
                raise ReplicaMaterializationRejectedError("recorded staged operation is missing")
            pending_descriptor, pending_metadata = pending
            try:
                _validate_stage(
                    stage,
                    pending_name,
                    pending_descriptor,
                    pending_metadata,
                )
                if receipt_value is not None:
                    raise ReplicaMaterializationRejectedError(
                        "receipt exists without final operation"
                    )
                if prepared is None:
                    await _build_pending(
                        self._staging_root / pending_name,
                        pending_descriptor,
                        opened,
                        uid=self._expected_uid,
                        mount_id=self._staging_mount_id,
                    )
                    tree_sha256 = _sync_tree_digest(
                        pending_descriptor,
                        uid=self._expected_uid,
                        gid=self._expected_gid,
                        device=pending_metadata.st_dev,
                        mount_id=self._staging_mount_id,
                        allowed_symlinks=declared_symlinks,
                    )
                    await _complete_despite_cancellation(
                        self._publication.recheck_opened_published_artifact_set(opened)
                    )
                    prepared = _prepared_value(request, stage, tree_sha256)
                    _publish_record(
                        operation_path,
                        operation,
                        "prepared.json",
                        prepared,
                        operation_identity=operation_identity,
                    )
                else:
                    tree_sha256 = _sync_tree_digest(
                        pending_descriptor,
                        uid=self._expected_uid,
                        gid=self._expected_gid,
                        device=pending_metadata.st_dev,
                        mount_id=self._staging_mount_id,
                        allowed_symlinks=declared_symlinks,
                    )
                    if prepared != _prepared_value(request, stage, tree_sha256):
                        raise ReplicaMaterializationRejectedError("prepared staging digest differs")
                await _complete_despite_cancellation(
                    self._publication.recheck_opened_published_artifact_set(opened)
                )
                source_identity = stage.get("operation_directory_identity")
                if type(source_identity) is not str:
                    raise ReplicaMaterializationRejectedError("stage operation identity is invalid")
                try:
                    atomic_rename_no_replace(
                        self._staging_root / pending_name,
                        self._staging_root / request.declaration.operation_id,
                        source_parent_identity=_identity(staging_metadata),
                        target_parent_identity=_identity(staging_metadata),
                        source_identity=source_identity,
                    )
                except Exception as error:
                    raise ReplicaMaterializationUnresolvedError(
                        "materialization rename outcome is unresolved"
                    ) from error
            finally:
                os.close(pending_descriptor)
            final = _open_owned_operation(
                staging,
                request.declaration.operation_id,
                uid=self._expected_uid,
                gid=self._expected_gid,
                mount_id=self._staging_mount_id,
                foreign_is_collision=True,
            )
            if final is None:
                raise ReplicaMaterializationUnresolvedError("published operation is not observable")
            return await self._finish_final(
                request,
                opened,
                staging=staging,
                staging_metadata=staging_metadata,
                operation=operation,
                operation_identity=operation_identity,
                operation_path=operation_path,
                stage=stage,
                prepared=prepared,
                receipt_value=None,
                final=final,
                durable_intent=durable_intent,
            )
        finally:
            os.close(operation)

    async def _finish_final(
        self,
        request: ReplicaMaterializationRequest,
        opened: OpenedPublishedReplicaArtifacts,
        *,
        staging: int,
        staging_metadata: os.stat_result,
        operation: int,
        operation_identity: str,
        operation_path: Path,
        stage: dict[str, Any],
        prepared: dict[str, Any] | None,
        receipt_value: dict[str, Any] | None,
        final: tuple[int, os.stat_result],
        durable_intent: bool,
    ) -> tuple[ReplicaMaterializationReceipt, bool]:
        final_descriptor, final_metadata = final
        declared_symlinks = _declared_destination_symlinks(opened.verified_worktree)
        repository: int | None = None
        home: int | None = None
        try:
            _validate_stage(
                stage,
                str(stage["pending_name"]),
                final_descriptor,
                final_metadata,
            )
            if prepared is None:
                raise ReplicaMaterializationRejectedError(
                    "final operation lacks prepared authority"
                )
            tree_sha256 = _sync_tree_digest(
                final_descriptor,
                uid=self._expected_uid,
                gid=self._expected_gid,
                device=final_metadata.st_dev,
                mount_id=self._staging_mount_id,
                allowed_symlinks=declared_symlinks,
            )
            if prepared != _prepared_value(request, stage, tree_sha256):
                raise ReplicaMaterializationRejectedError("final staging digest differs")
            if set(os.listdir(final_descriptor)) != {"repo", "home"}:
                raise ReplicaMaterializationRejectedError("final operation entries are invalid")
            repository = os.open("repo", _directory_flags(), dir_fd=final_descriptor)
            home = os.open("home", _directory_flags(), dir_fd=final_descriptor)
            repository_metadata = os.fstat(repository)
            home_metadata = os.fstat(home)
            if (
                _mount_id(repository) != self._staging_mount_id
                or _mount_id(home) != self._staging_mount_id
            ):
                raise ReplicaMaterializationRejectedError("final child mount identity changed")
            if os.listdir(home):
                raise ReplicaMaterializationRejectedError("final home is not empty")
            _verify_worktree(
                repository,
                opened.verified_worktree,
                uid=self._expected_uid,
                device=final_metadata.st_dev,
                mount_id=self._staging_mount_id,
            )
            git_directory = os.open(".git", _directory_flags(), dir_fd=repository)
            try:
                _validate_git_topology(
                    git_directory,
                    opened.verified_bundle,
                    opened.verified_worktree,
                    uid=self._expected_uid,
                    device=final_metadata.st_dev,
                    mount_id=self._staging_mount_id,
                )
            finally:
                os.close(git_directory)
            repository_stable = _StableDirectory(
                str(self._staging_root / request.declaration.operation_id / "repo"),
                repository,
            )
            await _verify_refs_and_head(
                repository_stable,
                opened.verified_bundle,
                opened.verified_worktree,
            )
            await _verify_objects(
                repository_stable,
                opened.verified_bundle,
                opened.verified_index_objects,
            )
            await _verify_index_semantics(
                repository_stable,
                opened.verified_worktree,
                max_path_bytes=opened.declaration.limits.worktree.max_path_bytes,
            )
            os.fsync(repository)
            os.fsync(home)
            os.fsync(final_descriptor)
            os.fsync(staging)
            await _complete_despite_cancellation(
                self._publication.recheck_opened_published_artifact_set(opened)
            )
            # No await is permitted between this complete identity check and
            # durable receipt publication.
            _reverify_named_destination(
                staging=staging,
                staging_root=self._staging_root,
                staging_root_identity=self._staging_root_identity,
                final_descriptor=final_descriptor,
                final_metadata=final_metadata,
                operation_id=request.declaration.operation_id,
                repository=repository,
                repository_metadata=repository_metadata,
                home=home,
                home_metadata=home_metadata,
                uid=self._expected_uid,
                gid=self._expected_gid,
                mount_id=self._staging_mount_id,
                allowed_symlinks=declared_symlinks,
                expected_tree_sha256=tree_sha256,
            )
            receipt = _make_receipt(
                request,
                tree_sha256,
                final_metadata,
                repository_metadata,
                home_metadata,
            )
            if receipt_value is not None:
                persisted = _parse_receipt(receipt_value)
                if persisted != receipt:
                    raise ReplicaMaterializationRejectedError(
                        "persisted receipt differs from final operation"
                    )
                return persisted, durable_intent
            _publish_record(
                operation_path,
                operation,
                "receipt.json",
                asdict(receipt),
                operation_identity=operation_identity,
            )
            return receipt, durable_intent
        finally:
            if home is not None:
                os.close(home)
            if repository is not None:
                os.close(repository)
            os.close(final_descriptor)
