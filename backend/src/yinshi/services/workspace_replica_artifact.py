r"""Standalone bounded worktree artifact codec for workspace replicas.

This module is self-contained. It does not access SQLite, brokers, networks,
filesystems, or Git. It converts a validated in-memory worktree snapshot into a
deterministic bounded binary envelope and decodes that envelope.
"""

from __future__ import annotations

import enum
import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

MAGIC: Final[bytes] = b"YNSHRA1\n"
_SOURCE_DOMAIN: Final[bytes] = b"yinshi-replica-source-v1"
_VERSION: Final[int] = 1
_FLAG_COMPLETE: Final[int] = 1
_ENVELOPE_HEADER_LEN: Final[int] = len(MAGIC) + 12
_SECTION_HEADER_LEN: Final[int] = 44
_POLICY_DIGEST_LEN: Final[int] = 32
_MANIFEST_ENTRY_MIN_BYTES: Final[int] = 14
_ENTRY_FLAG_EXECUTABLE: Final[int] = 1
_ENTRY_FLAG_ALLOWED_IGNORED: Final[int] = 2
_ENTRY_FLAGS_KNOWN: Final[int] = 3
_FORMAT_CODES: Final[dict[str, int]] = {"sha1": 1, "sha256": 2}
_FORMAT_NAMES: Final[dict[int, str]] = {code: name for name, code in _FORMAT_CODES.items()}
_FORMAT_OID_SIZES: Final[dict[str, int]] = {"sha1": 20, "sha256": 32}
_INDEX_VERSIONS: Final[frozenset[int]] = frozenset({2, 3})
_INDEX_ALLOWED_EXTENSIONS: Final[frozenset[bytes]] = frozenset(
    {b"TREE", b"REUC", b"UNTR", b"FSMN", b"EOIE", b"IEOT"}
)
_INDEX_MODE_REGULAR: Final[frozenset[int]] = frozenset({0o100644, 0o100755, 0o120000})
_INDEX_FLAG_ASSUME_VALID: Final[int] = 0x8000
_INDEX_FLAG_EXTENDED: Final[int] = 0x4000
_INDEX_FLAG_STAGE_MASK: Final[int] = 0x3000
_INDEX_FLAG_NAME_MASK: Final[int] = 0x0FFF
_INDEX_EXTENDED_FLAG_SKIP_WORKTREE: Final[int] = 0x4000
_INDEX_EXTENDED_FLAG_INTENT_TO_ADD: Final[int] = 0x2000
_INDEX_EXTENDED_FLAGS_KNOWN: Final[int] = 0x6000
_HEAD_STATE_CODES: Final[dict[str, int]] = {"unborn": 0, "detached": 1, "symbolic": 2}
_HEAD_STATE_NAMES: Final[dict[int, str]] = {
    code: state for state, code in _HEAD_STATE_CODES.items()
}


class WorktreeEntryKind(enum.StrEnum):
    """Kind of one worktree manifest entry."""

    DIRECTORY = "directory"
    FILE = "file"
    SYMLINK = "symlink"


_KIND_CODES: Final[dict[WorktreeEntryKind, int]] = {
    WorktreeEntryKind.DIRECTORY: 0,
    WorktreeEntryKind.FILE: 1,
    WorktreeEntryKind.SYMLINK: 2,
}
_KINDS_BY_CODE: Final[dict[int, WorktreeEntryKind]] = {
    code: kind for kind, code in _KIND_CODES.items()
}


class WorktreeArtifactError(Exception):
    """Base error for the worktree artifact codec."""


class WorktreeArtifactEncodeError(WorktreeArtifactError):
    """Raised when an artifact snapshot cannot be encoded."""


class WorktreeArtifactDecodeError(WorktreeArtifactError):
    """Raised when artifact bytes are malformed, inconsistent, or over limits."""


@dataclass(frozen=True)
class ArtifactLimits:
    """Immutable bounds applied before allocations on encode or decode."""

    max_total_bytes: int
    max_control_bytes: int
    max_index_bytes: int
    max_manifest_bytes: int
    max_roots_bytes: int
    max_exact_index_bytes: int
    max_manifest_entries: int
    max_path_bytes: int
    max_depth: int
    max_content_bytes: int
    max_aggregate_content_bytes: int
    max_roots: int

    def __post_init__(self) -> None:
        bounds: dict[str, tuple[int, int]] = {
            "max_total_bytes": (0, 0xFFFFFFFFFFFFFFFF),
            "max_control_bytes": (0, 0xFFFFFFFFFFFFFFFF),
            "max_index_bytes": (0, 0xFFFFFFFFFFFFFFFF),
            "max_manifest_bytes": (0, 0xFFFFFFFFFFFFFFFF),
            "max_roots_bytes": (0, 0xFFFFFFFFFFFFFFFF),
            "max_exact_index_bytes": (0, 0xFFFFFFFFFFFFFFFF),
            "max_manifest_entries": (0, 0xFFFFFFFF),
            "max_path_bytes": (1, 0xFFFFFFFF),
            "max_depth": (1, 0xFFFFFFFF),
            "max_content_bytes": (0, 0xFFFFFFFF),
            "max_aggregate_content_bytes": (0, 0xFFFFFFFFFFFFFFFF),
            "max_roots": (0, 0xFFFFFFFF),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int:
                raise ValueError(f"limit {name} must be an integer")
            if not low <= value <= high:
                raise ValueError(f"limit {name} must be between {low} and {high}")
        if self.max_exact_index_bytes > self.max_index_bytes:
            raise ValueError("max_exact_index_bytes exceeds max_index_bytes")
        if self.max_content_bytes > self.max_aggregate_content_bytes:
            raise ValueError("max_content_bytes exceeds max_aggregate_content_bytes")
        if self.max_aggregate_content_bytes > self.max_manifest_bytes:
            raise ValueError("max_aggregate_content_bytes exceeds max_manifest_bytes")


DEFAULT_ARTIFACT_LIMITS: Final[ArtifactLimits] = ArtifactLimits(
    max_total_bytes=256 * 1024 * 1024,
    max_control_bytes=1024 * 1024,
    max_index_bytes=64 * 1024 * 1024,
    max_manifest_bytes=256 * 1024 * 1024,
    max_roots_bytes=16 * 1024 * 1024,
    max_exact_index_bytes=64 * 1024 * 1024,
    max_manifest_entries=1_000_000,
    max_path_bytes=4096,
    max_depth=128,
    max_content_bytes=64 * 1024 * 1024,
    max_aggregate_content_bytes=256 * 1024 * 1024,
    max_roots=1_000_000,
)


@dataclass(frozen=True)
class ManifestEntry:
    """One canonical worktree manifest entry."""

    raw_path: bytes
    kind: WorktreeEntryKind
    content: bytes = b""
    executable: bool = False
    allowed_ignored: bool = False
    nlink: int = 1
    privilege_bits: bool = False


@dataclass(frozen=True)
class ArtifactIndexEntry:
    """One fully validated Git index entry."""

    raw_path: bytes
    mode: int
    object_id: bytes
    stage: int
    intent_to_add: bool
    skip_worktree: bool
    assume_unchanged: bool


@dataclass(frozen=True)
class WorktreeArtifactInput:
    """Snapshot handed to :func:`encode_worktree_artifact`."""

    object_format: str
    head_state: str
    head_target: bytes = b""
    head_oid: bytes = b""
    index_bytes: bytes | None = None
    entries: Sequence[ManifestEntry] = ()
    root_oids: Sequence[bytes] = ()
    policy_digest: bytes | None = None


@dataclass(frozen=True)
class VerifiedWorktreeArtifact:
    """Immutable decode result with computed source-state digest."""

    object_format: str
    has_index: bool
    index_bytes: bytes | None
    index_entries: tuple[ArtifactIndexEntry, ...]
    head_state: str
    head_target: bytes
    head_oid: bytes
    policy_digest: bytes | None
    entries: tuple[ManifestEntry, ...]
    root_oids: tuple[bytes, ...]
    source_state_sha256: bytes
    total_bytes: int


__all__ = [
    "DEFAULT_ARTIFACT_LIMITS",
    "MAGIC",
    "ArtifactIndexEntry",
    "ArtifactLimits",
    "ManifestEntry",
    "VerifiedWorktreeArtifact",
    "WorktreeArtifactDecodeError",
    "WorktreeArtifactEncodeError",
    "WorktreeArtifactError",
    "WorktreeArtifactInput",
    "WorktreeEntryKind",
    "compute_source_state_sha256",
    "decode_worktree_artifact",
    "encode_worktree_artifact",
]


def _u32_bytes(value: int) -> bytes:
    return value.to_bytes(4, "big")


def _u64_bytes(value: int) -> bytes:
    return value.to_bytes(8, "big")


def _derived_parent(path: bytes) -> bytes:
    cut = path.rfind(b"/")
    return path[:cut] if cut >= 0 else b""


def _validate_raw_path(
    path: bytes,
    limits: ArtifactLimits,
    error: Callable[[str], WorktreeArtifactError],
) -> None:
    if len(path) > limits.max_path_bytes:
        raise error("entry path exceeds maximum path bytes")
    if not path or path.startswith(b"/") or b"\x00" in path:
        raise error("worktree path is unsafe")
    components = path.split(b"/")
    if len(components) > limits.max_depth:
        raise error("entry path exceeds maximum depth")
    if any(component in {b"", b".", b"..", b".git"} for component in components):
        raise error("worktree path has an unsafe component")


def _validate_symbolic_head_target(
    target: bytes,
    limits: ArtifactLimits,
    error: Callable[[str], WorktreeArtifactError],
) -> None:
    if len(target) > limits.max_path_bytes or not target.startswith(b"refs/heads/"):
        raise error("symbolic HEAD target is not a bounded branch reference")
    suffix = target[len(b"refs/heads/") :]
    components = suffix.split(b"/")
    forbidden = b" ~^:?*[\\\x7f"
    if (
        not suffix
        or b".." in suffix
        or b"@{" in suffix
        or suffix.endswith(b".")
        or any(not component for component in components)
        or any(component.startswith(b".") for component in components)
        or any(component.endswith(b".lock") for component in components)
        or any(byte < 0x20 or byte in forbidden for byte in suffix)
    ):
        raise error("symbolic HEAD target is not canonical")


def _validate_entry_shape(
    entry: ManifestEntry,
    limits: ArtifactLimits,
    error: Callable[[str], WorktreeArtifactError],
) -> None:
    if type(entry) is not ManifestEntry:
        raise error("manifest entry type is invalid")
    if type(entry.nlink) is not int or entry.nlink != 1:
        raise error("hardlinks are not representable")
    if entry.privilege_bits is not False:
        raise error("privilege bits are not representable")
    if type(entry.executable) is not bool or type(entry.allowed_ignored) is not bool:
        raise error("manifest entry flags must be Boolean")
    if not isinstance(entry.raw_path, bytes) or not isinstance(entry.content, bytes):
        raise error("manifest path and content must be raw bytes")
    if type(entry.kind) is not WorktreeEntryKind:
        raise error("manifest entry kind is unsupported")
    if entry.kind is WorktreeEntryKind.DIRECTORY and (entry.content or entry.executable):
        raise error("directory entry state is invalid")
    if entry.kind is WorktreeEntryKind.SYMLINK and (entry.executable or not entry.content):
        raise error("symlink entry state is invalid")
    if len(entry.content) > limits.max_content_bytes:
        raise error("entry content exceeds maximum content bytes")
    _validate_raw_path(entry.raw_path, limits, error)


def _validate_topology(
    path: bytes,
    present: dict[bytes, WorktreeEntryKind],
    error: Callable[[str], WorktreeArtifactError],
) -> None:
    components = path.split(b"/")
    for index in range(1, len(components)):
        ancestor = b"/".join(components[:index])
        if present.get(ancestor) is not WorktreeEntryKind.DIRECTORY:
            raise error("manifest entry lacks an explicit directory parent")


def _parse_index(
    index_bytes: bytes,
    object_format: str,
    limits: ArtifactLimits,
    error: Callable[[str], WorktreeArtifactError],
) -> tuple[ArtifactIndexEntry, ...]:
    """Validate one complete Git index without interpreting extension payloads."""
    oid_size = _FORMAT_OID_SIZES[object_format]
    checksum_start = len(index_bytes) - oid_size
    if checksum_start < 12:
        raise error("Git index is truncated")
    if index_bytes[:4] != b"DIRC":
        raise error("Git index signature is invalid")
    version = int.from_bytes(index_bytes[4:8], "big")
    if version not in _INDEX_VERSIONS:
        raise error("Git index version is unsupported")
    if (
        hashlib.new(object_format, index_bytes[:checksum_start]).digest()
        != index_bytes[checksum_start:]
    ):
        raise error("Git index checksum is invalid for object format")

    count = int.from_bytes(index_bytes[8:12], "big")
    fixed_size = 40 + oid_size + 2
    minimum_entry_size = (fixed_size + 2 + 7) & ~7
    entry_region_bytes = checksum_start - 12
    if count > entry_region_bytes // minimum_entry_size:
        raise error("Git index entry count exceeds payload bounds")
    if count > limits.max_exact_index_bytes // minimum_entry_size:
        raise error("Git index entry count exceeds configured bounds")

    entries: list[ArtifactIndexEntry] = []
    position = 12
    previous_key: tuple[bytes, int] | None = None
    for _ in range(count):
        entry_start = position
        if fixed_size > checksum_start - position:
            raise error("Git index entry fixed fields are truncated")
        mode = int.from_bytes(index_bytes[position + 24 : position + 28], "big")
        object_id_start = position + 40
        object_id = index_bytes[object_id_start : object_id_start + oid_size]
        flags_start = object_id_start + oid_size
        flags = int.from_bytes(index_bytes[flags_start : flags_start + 2], "big")
        position = flags_start + 2

        extended_flags = 0
        if flags & _INDEX_FLAG_EXTENDED:
            if version != 3:
                raise error("Git index version 2 entry has extended flags")
            if checksum_start - position < 2:
                raise error("Git index extended flags are truncated")
            extended_flags = int.from_bytes(index_bytes[position : position + 2], "big")
            position += 2
            if extended_flags & ~_INDEX_EXTENDED_FLAGS_KNOWN:
                raise error("Git index extended flags are unknown")

        path_end = index_bytes.find(b"\x00", position, checksum_start)
        if path_end < 0:
            raise error("Git index entry path is not NUL terminated")
        if path_end - position > limits.max_path_bytes:
            raise error("Git index entry path exceeds maximum path bytes")
        path = index_bytes[position:path_end]
        encoded_path_length = flags & _INDEX_FLAG_NAME_MASK
        if encoded_path_length != min(len(path), _INDEX_FLAG_NAME_MASK):
            raise error("Git index entry path length flag is not canonical")
        _validate_raw_path(path, limits, error)

        unpadded_size = path_end + 1 - entry_start
        padding_size = (-unpadded_size) % 8
        next_position = path_end + 1 + padding_size
        if next_position > checksum_start:
            raise error("Git index entry padding is truncated")
        if any(index_bytes[path_end + 1 : next_position]):
            raise error("Git index entry padding is nonzero")
        position = next_position

        if mode not in _INDEX_MODE_REGULAR:
            raise error("Git index entry mode is unsupported")
        if not any(object_id):
            raise error("Git index entry object ID cannot be zero")
        stage = (flags & _INDEX_FLAG_STAGE_MASK) >> 12
        key = (path, stage)
        if previous_key is not None:
            if key == previous_key:
                raise error("Git index entry key is duplicated")
            if key < previous_key:
                raise error("Git index entries are not canonically ordered")
            if path == previous_key[0] and (stage == 0 or previous_key[1] == 0):
                raise error("Git index stage zero cannot coexist with conflict stages")
        previous_key = key
        entries.append(
            ArtifactIndexEntry(
                raw_path=path,
                mode=mode,
                object_id=object_id,
                stage=stage,
                intent_to_add=bool(extended_flags & _INDEX_EXTENDED_FLAG_INTENT_TO_ADD),
                skip_worktree=bool(extended_flags & _INDEX_EXTENDED_FLAG_SKIP_WORKTREE),
                assume_unchanged=bool(flags & _INDEX_FLAG_ASSUME_VALID),
            )
        )

    seen_eoie = False
    while position < checksum_start:
        if seen_eoie:
            raise error("Git index EOIE extension must be final")
        if checksum_start - position < 8:
            raise error("Git index extension framing is truncated")
        signature = index_bytes[position : position + 4]
        extension_size = int.from_bytes(index_bytes[position + 4 : position + 8], "big")
        position += 8
        if extension_size > checksum_start - position:
            raise error("Git index extension payload is truncated")
        if signature not in _INDEX_ALLOWED_EXTENSIONS:
            raise error("Git index extension is unsupported")
        if signature == b"EOIE":
            seen_eoie = True
        position += extension_size

    return tuple(entries)


def _expected_roots(
    head_oid: bytes,
    index_entries: tuple[ArtifactIndexEntry, ...],
) -> tuple[bytes, ...]:
    roots = {entry.object_id for entry in index_entries}
    if head_oid:
        roots.add(head_oid)
    return tuple(sorted(roots))


def compute_source_state_sha256(
    control_digest: bytes,
    index_digest: bytes,
    manifest_digest: bytes,
    roots_digest: bytes,
) -> bytes:
    """Derive source-state digest from four raw section digests."""
    digests = (control_digest, index_digest, manifest_digest, roots_digest)
    if any(type(digest) is not bytes or len(digest) != 32 for digest in digests):
        raise ValueError("section digests must be exactly 32 raw bytes")
    hasher = hashlib.sha256(_SOURCE_DOMAIN + b"\x00")
    for digest in digests:
        hasher.update(digest)
    return hasher.digest()


def _build_control_payload(
    artifact: WorktreeArtifactInput,
    *,
    entry_count: int,
    roots_count: int,
    aggregate_content_bytes: int,
) -> bytes:
    payload = bytearray(
        (
            _FORMAT_CODES[artifact.object_format],
            1 if artifact.index_bytes is not None else 0,
            _HEAD_STATE_CODES[artifact.head_state],
            0,
        )
    )
    payload += _u32_bytes(len(artifact.head_target)) + artifact.head_target
    if artifact.head_state != "unborn":
        payload += artifact.head_oid
    payload += _u32_bytes(entry_count)
    payload += _u32_bytes(roots_count)
    payload += _u64_bytes(aggregate_content_bytes)
    if artifact.policy_digest is None:
        payload += b"\x00"
    else:
        payload += b"\x01" + artifact.policy_digest
    return bytes(payload)


def _build_manifest_payload(entries: list[ManifestEntry]) -> bytes:
    payload = bytearray()
    for entry in entries:
        parent = _derived_parent(entry.raw_path)
        flags = 0
        if entry.executable:
            flags |= _ENTRY_FLAG_EXECUTABLE
        if entry.allowed_ignored:
            flags |= _ENTRY_FLAG_ALLOWED_IGNORED
        payload += bytes((_KIND_CODES[entry.kind], flags))
        payload += _u32_bytes(len(parent)) + parent
        payload += _u32_bytes(len(entry.raw_path)) + entry.raw_path
        payload += _u32_bytes(len(entry.content)) + entry.content
    return bytes(payload)


def _frame_envelope(sections: Sequence[tuple[int, bytes]]) -> bytes:
    frame = bytearray(MAGIC)
    frame += _u32_bytes(_VERSION)
    frame += _u32_bytes(_FLAG_COMPLETE)
    frame += _u32_bytes(len(sections))
    for section_id, payload in sections:
        frame += _u32_bytes(section_id)
        frame += _u64_bytes(len(payload))
        frame += hashlib.sha256(payload).digest()
        frame += payload
    return bytes(frame)


def _validate_head(
    artifact: WorktreeArtifactInput,
    oid_size: int,
    limits: ArtifactLimits,
) -> None:
    if type(artifact.head_target) is not bytes or type(artifact.head_oid) is not bytes:
        raise WorktreeArtifactEncodeError("HEAD fields must be raw bytes")
    if len(artifact.head_target) > 0xFFFFFFFF:
        raise WorktreeArtifactEncodeError("HEAD target is too large")
    if artifact.head_state == "unborn":
        if artifact.head_oid or artifact.head_target:
            raise WorktreeArtifactEncodeError("unborn HEAD must not carry target state")
    elif artifact.head_state == "detached":
        if artifact.head_target or len(artifact.head_oid) != oid_size:
            raise WorktreeArtifactEncodeError("detached HEAD state is invalid")
    elif len(artifact.head_oid) != oid_size:
        raise WorktreeArtifactEncodeError("symbolic HEAD state is invalid")
    else:
        _validate_symbolic_head_target(
            artifact.head_target,
            limits,
            WorktreeArtifactEncodeError,
        )
    if artifact.head_oid and not any(artifact.head_oid):
        raise WorktreeArtifactEncodeError("HEAD object ID cannot be zero")


def encode_worktree_artifact(
    artifact: WorktreeArtifactInput,
    *,
    limits: ArtifactLimits = DEFAULT_ARTIFACT_LIMITS,
) -> bytes:
    """Validate snapshot fully, then return deterministic envelope bytes."""
    if type(artifact) is not WorktreeArtifactInput:
        raise WorktreeArtifactEncodeError("input must be WorktreeArtifactInput")
    if type(artifact.object_format) is not str or artifact.object_format not in _FORMAT_CODES:
        raise WorktreeArtifactEncodeError("object format is unsupported")
    if type(artifact.head_state) is not str or artifact.head_state not in _HEAD_STATE_CODES:
        raise WorktreeArtifactEncodeError("HEAD state is unsupported")
    oid_size = _FORMAT_OID_SIZES[artifact.object_format]
    _validate_head(artifact, oid_size, limits)
    if artifact.policy_digest is not None and (
        type(artifact.policy_digest) is not bytes or len(artifact.policy_digest) != 32
    ):
        raise WorktreeArtifactEncodeError("policy digest must be 32 raw bytes")
    index_payload = b"" if artifact.index_bytes is None else artifact.index_bytes
    if type(index_payload) is not bytes:
        raise WorktreeArtifactEncodeError("index bytes must be raw bytes")
    if len(index_payload) > limits.max_exact_index_bytes:
        raise WorktreeArtifactEncodeError("exact index exceeds maximum bytes")
    index_entries = (
        ()
        if artifact.index_bytes is None
        else _parse_index(
            index_payload,
            artifact.object_format,
            limits,
            WorktreeArtifactEncodeError,
        )
    )
    try:
        raw_entries = tuple(artifact.entries)
        raw_roots = tuple(artifact.root_oids)
    except TypeError as error:
        raise WorktreeArtifactEncodeError("entries and roots must be sequences") from error
    if len(raw_entries) > limits.max_manifest_entries:
        raise WorktreeArtifactEncodeError("manifest entry count exceeds maximum")
    if len(raw_roots) > limits.max_roots:
        raise WorktreeArtifactEncodeError("root count exceeds maximum")
    for entry in raw_entries:
        _validate_entry_shape(entry, limits, WorktreeArtifactEncodeError)
    entries = sorted(raw_entries, key=lambda entry: entry.raw_path)
    present: dict[bytes, WorktreeEntryKind] = {}
    for entry in entries:
        if entry.raw_path in present:
            raise WorktreeArtifactEncodeError("manifest path is duplicated")
        _validate_topology(entry.raw_path, present, WorktreeArtifactEncodeError)
        present[entry.raw_path] = entry.kind
    aggregate = sum(
        len(entry.content) for entry in entries if entry.kind is not WorktreeEntryKind.DIRECTORY
    )
    if aggregate > limits.max_aggregate_content_bytes:
        raise WorktreeArtifactEncodeError("aggregate content exceeds maximum bytes")
    if any(entry.allowed_ignored for entry in entries) and artifact.policy_digest is None:
        raise WorktreeArtifactEncodeError("ignored entries require a policy digest")
    for root in raw_roots:
        if type(root) is not bytes or len(root) != oid_size or not any(root):
            raise WorktreeArtifactEncodeError("root object ID is invalid")
    roots = _expected_roots(artifact.head_oid, index_entries)
    if len(set(raw_roots)) != len(raw_roots):
        raise WorktreeArtifactEncodeError("root object ID is duplicated")
    if set(raw_roots) != set(roots):
        raise WorktreeArtifactEncodeError("roots do not exactly match HEAD and index object IDs")
    control_length = (
        4
        + 4
        + len(artifact.head_target)
        + (0 if artifact.head_state == "unborn" else oid_size)
        + 4
        + 4
        + 8
        + 1
        + (0 if artifact.policy_digest is None else 32)
    )
    manifest_length = sum(
        14 + len(_derived_parent(entry.raw_path)) + len(entry.raw_path) + len(entry.content)
        for entry in entries
    )
    roots_length = len(roots) * oid_size
    section_lengths = (control_length, len(index_payload), manifest_length, roots_length)
    section_bounds = (
        limits.max_control_bytes,
        limits.max_index_bytes,
        limits.max_manifest_bytes,
        limits.max_roots_bytes,
    )
    if any(length > bound for length, bound in zip(section_lengths, section_bounds, strict=True)):
        raise WorktreeArtifactEncodeError("artifact section exceeds maximum bytes")
    framed_length = _ENVELOPE_HEADER_LEN + 4 * _SECTION_HEADER_LEN + sum(section_lengths)
    if framed_length > limits.max_total_bytes:
        raise WorktreeArtifactEncodeError("artifact exceeds maximum total bytes")
    control = _build_control_payload(
        artifact,
        entry_count=len(entries),
        roots_count=len(roots),
        aggregate_content_bytes=aggregate,
    )
    manifest = _build_manifest_payload(entries)
    roots_payload = b"".join(roots)
    sections = ((1, control), (2, index_payload), (3, manifest), (4, roots_payload))
    encoded = _frame_envelope(sections)
    if len(encoded) != framed_length:
        raise AssertionError("artifact framing length changed after preflight")
    return encoded


class _Reader:
    """Bounds-checked big-endian reader over a fixed payload window."""

    def __init__(self, view: memoryview[int]) -> None:
        self._view = view
        self._position = 0

    @property
    def remaining(self) -> int:
        return len(self._view) - self._position

    def take(self, size: int, label: str) -> memoryview[int]:
        if size < 0 or size > self.remaining:
            raise WorktreeArtifactDecodeError(f"truncated {label}")
        start = self._position
        self._position += size
        return self._view[start : start + size]

    def u8(self, label: str) -> int:
        return int.from_bytes(self.take(1, label), "big")

    def u32(self, label: str) -> int:
        return int.from_bytes(self.take(4, label), "big")

    def u64(self, label: str) -> int:
        return int.from_bytes(self.take(8, label), "big")


def _parse_sections(
    view: memoryview[int], limits: ArtifactLimits
) -> tuple[list[bytes], list[memoryview[int]]]:
    section_bounds = (
        limits.max_control_bytes,
        limits.max_index_bytes,
        limits.max_manifest_bytes,
        limits.max_roots_bytes,
    )
    digests: list[bytes] = []
    payloads: list[memoryview[int]] = []
    offset = _ENVELOPE_HEADER_LEN
    for position, bound in enumerate(section_bounds, start=1):
        if len(view) - offset < _SECTION_HEADER_LEN:
            raise WorktreeArtifactDecodeError("truncated section header")
        section_id = int.from_bytes(view[offset : offset + 4], "big")
        if section_id not in {1, 2, 3, 4}:
            raise WorktreeArtifactDecodeError("section ID is unknown")
        if section_id != position:
            raise WorktreeArtifactDecodeError("sections are out of order")
        declared = int.from_bytes(view[offset + 4 : offset + 12], "big")
        if declared > bound:
            raise WorktreeArtifactDecodeError("section exceeds maximum bytes")
        payload_start = offset + _SECTION_HEADER_LEN
        if declared > len(view) - payload_start:
            raise WorktreeArtifactDecodeError("truncated section payload")
        stored_digest = bytes(view[offset + 12 : offset + 44])
        payload = view[payload_start : payload_start + declared]
        if hashlib.sha256(payload).digest() != stored_digest:
            raise WorktreeArtifactDecodeError("section digest mismatch")
        digests.append(stored_digest)
        payloads.append(payload)
        offset = payload_start + declared
    if offset != len(view):
        raise WorktreeArtifactDecodeError("artifact has trailing bytes")
    return digests, payloads


def _parse_control(
    payload: memoryview[int], limits: ArtifactLimits
) -> tuple[str, bool, str, bytes, bytes, bytes | None, int, int, int]:
    reader = _Reader(payload)
    object_format = _FORMAT_NAMES.get(reader.u8("object format"))
    if object_format is None:
        raise WorktreeArtifactDecodeError("object format is unsupported")
    has_index_code = reader.u8("index presence")
    if has_index_code not in {0, 1}:
        raise WorktreeArtifactDecodeError("index-presence flag is invalid")
    head_state = _HEAD_STATE_NAMES.get(reader.u8("HEAD state"))
    if head_state is None:
        raise WorktreeArtifactDecodeError("HEAD state is unsupported")
    if reader.u8("reserved control byte") != 0:
        raise WorktreeArtifactDecodeError("reserved control byte is nonzero")
    target_length = reader.u32("HEAD target length")
    if target_length > limits.max_path_bytes:
        raise WorktreeArtifactDecodeError("HEAD target exceeds maximum bytes")
    head_target = bytes(reader.take(target_length, "HEAD target"))
    oid_size = _FORMAT_OID_SIZES[object_format]
    head_oid = b"" if head_state == "unborn" else bytes(reader.take(oid_size, "HEAD object ID"))
    entry_count = reader.u32("manifest entry count")
    root_count = reader.u32("root count")
    aggregate = reader.u64("aggregate content bytes")
    policy_code = reader.u8("policy presence")
    if policy_code not in {0, 1}:
        raise WorktreeArtifactDecodeError("policy-presence flag is invalid")
    policy_digest = bytes(reader.take(32, "policy digest")) if policy_code else None
    if reader.remaining:
        raise WorktreeArtifactDecodeError("control section has trailing bytes")
    if head_state == "unborn" and (head_target or head_oid):
        raise WorktreeArtifactDecodeError("unborn HEAD state is invalid")
    if head_state == "detached" and head_target:
        raise WorktreeArtifactDecodeError("detached HEAD state is invalid")
    if head_state == "symbolic":
        _validate_symbolic_head_target(
            head_target,
            limits,
            WorktreeArtifactDecodeError,
        )
    if head_oid and not any(head_oid):
        raise WorktreeArtifactDecodeError("HEAD object ID cannot be zero")
    if entry_count > limits.max_manifest_entries:
        raise WorktreeArtifactDecodeError("manifest entry count exceeds maximum")
    if root_count > limits.max_roots:
        raise WorktreeArtifactDecodeError("root count exceeds maximum")
    if aggregate > limits.max_aggregate_content_bytes:
        raise WorktreeArtifactDecodeError("aggregate content exceeds maximum bytes")
    return (
        object_format,
        bool(has_index_code),
        head_state,
        head_target,
        head_oid,
        policy_digest,
        entry_count,
        root_count,
        aggregate,
    )


def _parse_manifest(
    payload: memoryview[int], limits: ArtifactLimits
) -> tuple[tuple[ManifestEntry, ...], int]:
    reader = _Reader(payload)
    entries: list[ManifestEntry] = []
    present: dict[bytes, WorktreeEntryKind] = {}
    previous: bytes | None = None
    aggregate = 0
    while reader.remaining:
        if len(entries) >= limits.max_manifest_entries:
            raise WorktreeArtifactDecodeError("manifest entry count exceeds maximum")
        if reader.remaining < _MANIFEST_ENTRY_MIN_BYTES:
            raise WorktreeArtifactDecodeError("manifest has trailing bytes")
        kind = _KINDS_BY_CODE.get(reader.u8("entry kind"))
        if kind is None:
            raise WorktreeArtifactDecodeError("entry kind is unsupported")
        flags = reader.u8("entry flags")
        if flags & ~_ENTRY_FLAGS_KNOWN:
            raise WorktreeArtifactDecodeError("entry flags are unknown")
        parent_length = reader.u32("entry parent length")
        if parent_length > limits.max_path_bytes:
            raise WorktreeArtifactDecodeError("entry parent exceeds maximum bytes")
        parent = bytes(reader.take(parent_length, "entry parent"))
        path_length = reader.u32("entry path length")
        if path_length > limits.max_path_bytes:
            raise WorktreeArtifactDecodeError("entry path exceeds maximum bytes")
        path = bytes(reader.take(path_length, "entry path"))
        content_length = reader.u32("entry content length")
        if content_length > limits.max_content_bytes:
            raise WorktreeArtifactDecodeError("entry content exceeds maximum bytes")
        if (
            kind is not WorktreeEntryKind.DIRECTORY
            and aggregate + content_length > limits.max_aggregate_content_bytes
        ):
            raise WorktreeArtifactDecodeError("aggregate content exceeds maximum bytes")
        content = bytes(reader.take(content_length, "entry content"))
        _validate_raw_path(path, limits, WorktreeArtifactDecodeError)
        if parent != _derived_parent(path):
            raise WorktreeArtifactDecodeError("entry parent does not match path")
        if previous is not None and path <= previous:
            raise WorktreeArtifactDecodeError("manifest paths are not strictly increasing")
        executable = bool(flags & _ENTRY_FLAG_EXECUTABLE)
        allowed_ignored = bool(flags & _ENTRY_FLAG_ALLOWED_IGNORED)
        entry = ManifestEntry(
            raw_path=path,
            kind=kind,
            content=content,
            executable=executable,
            allowed_ignored=allowed_ignored,
        )
        _validate_entry_shape(entry, limits, WorktreeArtifactDecodeError)
        _validate_topology(path, present, WorktreeArtifactDecodeError)
        present[path] = kind
        previous = path
        entries.append(entry)
        if kind is not WorktreeEntryKind.DIRECTORY:
            aggregate += content_length
            if aggregate > limits.max_aggregate_content_bytes:
                raise WorktreeArtifactDecodeError("aggregate content exceeds maximum bytes")
    return tuple(entries), aggregate


def decode_worktree_artifact(
    data: bytes,
    *,
    limits: ArtifactLimits = DEFAULT_ARTIFACT_LIMITS,
) -> VerifiedWorktreeArtifact:
    """Decode and fully verify artifact bytes within ``limits``."""
    if type(data) is not bytes:
        raise WorktreeArtifactDecodeError("input must be bytes")
    if len(data) > limits.max_total_bytes:
        raise WorktreeArtifactDecodeError("artifact exceeds maximum total bytes")
    if len(data) < _ENVELOPE_HEADER_LEN:
        raise WorktreeArtifactDecodeError("artifact header is truncated")
    view = memoryview(data)
    if bytes(view[: len(MAGIC)]) != MAGIC:
        raise WorktreeArtifactDecodeError("artifact magic is invalid")
    if int.from_bytes(view[8:12], "big") != _VERSION:
        raise WorktreeArtifactDecodeError("artifact version is unsupported")
    envelope_flags = int.from_bytes(view[12:16], "big")
    if envelope_flags != _FLAG_COMPLETE:
        raise WorktreeArtifactDecodeError("artifact completion flags are invalid")
    if int.from_bytes(view[16:20], "big") != 4:
        raise WorktreeArtifactDecodeError("artifact section count is invalid")
    digests, payloads = _parse_sections(view, limits)
    (
        object_format,
        has_index,
        head_state,
        head_target,
        head_oid,
        policy_digest,
        entry_count,
        root_count,
        aggregate,
    ) = _parse_control(payloads[0], limits)
    index_payload = payloads[1]
    if not has_index and index_payload:
        raise WorktreeArtifactDecodeError("index presence conflicts with index section")
    if has_index and len(index_payload) > limits.max_exact_index_bytes:
        raise WorktreeArtifactDecodeError("exact index exceeds maximum bytes")
    index_bytes = bytes(index_payload) if has_index else None
    index_entries = (
        _parse_index(
            index_bytes,
            object_format,
            limits,
            WorktreeArtifactDecodeError,
        )
        if index_bytes is not None
        else ()
    )
    entries, found_aggregate = _parse_manifest(payloads[2], limits)
    if any(entry.allowed_ignored for entry in entries) and policy_digest is None:
        raise WorktreeArtifactDecodeError("ignored entries lack a policy digest")
    if len(entries) != entry_count:
        raise WorktreeArtifactDecodeError("manifest entry count does not match control")
    if found_aggregate != aggregate:
        raise WorktreeArtifactDecodeError("aggregate content does not match control")
    oid_size = _FORMAT_OID_SIZES[object_format]
    roots_payload = bytes(payloads[3])
    if len(roots_payload) != root_count * oid_size:
        raise WorktreeArtifactDecodeError("roots length does not match control")
    roots = tuple(
        roots_payload[index : index + oid_size] for index in range(0, len(roots_payload), oid_size)
    )
    if tuple(sorted(set(roots))) != roots or any(not any(root) for root in roots):
        raise WorktreeArtifactDecodeError("root object IDs are not valid, unique, and sorted")
    if roots != _expected_roots(head_oid, index_entries):
        raise WorktreeArtifactDecodeError(
            "roots do not exactly match sorted unique HEAD and index object IDs"
        )
    return VerifiedWorktreeArtifact(
        object_format=object_format,
        has_index=has_index,
        index_bytes=index_bytes,
        index_entries=index_entries,
        head_state=head_state,
        head_target=head_target,
        head_oid=head_oid,
        policy_digest=policy_digest,
        entries=entries,
        root_oids=roots,
        source_state_sha256=compute_source_state_sha256(*digests),
        total_bytes=len(data),
    )
