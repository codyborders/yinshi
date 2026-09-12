"""Create and verify bounded Git packs for index-only blob objects."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from yinshi.exceptions import GitError
from yinshi.services.git import run_git_bytes
from yinshi.services.workspace_replica_bundle import GitObject

_OID_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_GIT_ENV: Final[dict[str, str]] = {
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
}


@dataclass(frozen=True)
class IndexObjectPackLimits:
    """Resource bounds for a supplemental index-object pack."""

    max_pack_bytes: int = 512 * 1024 * 1024
    max_objects: int = 1_000_000
    max_object_bytes: int = 512 * 1024 * 1024
    max_inflated_bytes: int = 2 * 1024 * 1024 * 1024
    max_listing_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_pack_bytes",
            "max_objects",
            "max_object_bytes",
            "max_inflated_bytes",
            "max_listing_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_INDEX_OBJECT_PACK_LIMITS: Final[IndexObjectPackLimits] = IndexObjectPackLimits()


@dataclass(frozen=True)
class PackedObject:
    """One blob physically present in a verified supplemental pack."""

    object_id: str
    kind: Literal["blob"]
    byte_length: int


@dataclass(frozen=True)
class VerifiedIndexObjectPack:
    """Bounded metadata from consumer-side pack verification."""

    object_format: Literal["sha1", "sha256"]
    sha256: str
    byte_length: int
    objects: tuple[PackedObject, ...]


@dataclass(frozen=True)
class CreatedIndexObjectPack:
    """Generated pack bytes plus consumer-verified metadata."""

    object_format: Literal["sha1", "sha256"]
    pack_bytes: bytes
    sha256: str
    byte_length: int
    objects: tuple[PackedObject, ...]


def _validate_object_format(value: object) -> Literal["sha1", "sha256"]:
    if value == "sha1":
        return "sha1"
    if value == "sha256":
        return "sha256"
    raise ValueError("object_format must be sha1 or sha256")


def _validate_oids(
    values: Sequence[str],
    *,
    object_format: Literal["sha1", "sha256"],
    limits: IndexObjectPackLimits,
) -> tuple[str, ...]:
    try:
        items = tuple(values)
    except TypeError as error:
        raise TypeError("object IDs must be a finite sequence") from error
    if len(items) > limits.max_objects:
        raise GitError("index object count exceeded limit")
    expected_length = 40 if object_format == "sha1" else 64
    for item in items:
        if (
            type(item) is not str
            or len(item) != expected_length
            or not _OID_PATTERN.fullmatch(item)
            or not any(character != "0" for character in item)
        ):
            raise ValueError("index object ID is invalid")
    if len(set(items)) != len(items):
        raise ValueError("index object ID is duplicated")
    return tuple(sorted(items))


async def _repository_object_format(repository: str) -> Literal["sha1", "sha256"]:
    raw = await run_git_bytes(
        ["rev-parse", "--show-object-format"],
        cwd=repository,
        env=_GIT_ENV,
        stdout_bytes_max=8,
    )
    try:
        return _validate_object_format(raw.decode("ascii").strip())
    except UnicodeDecodeError as error:
        raise GitError("Git returned an invalid object format") from error


def _parse_inventory(
    raw: bytes,
    *,
    object_format: Literal["sha1", "sha256"],
    limits: IndexObjectPackLimits,
) -> tuple[PackedObject, ...]:
    if len(raw) > limits.max_listing_bytes:
        raise GitError("index object inventory exceeded listing limit")
    lines = raw.splitlines()
    if len(lines) > limits.max_objects:
        raise GitError("index object inventory exceeded object limit")
    expected_length = 40 if object_format == "sha1" else 64
    result: list[PackedObject] = []
    seen: set[str] = set()
    inflated_bytes = 0
    for line in lines:
        try:
            oid_raw, kind_raw, length_raw = line.split(b" ", 2)
            object_id = oid_raw.decode("ascii")
            kind = kind_raw.decode("ascii")
            length_text = length_raw.decode("ascii")
        except (ValueError, UnicodeDecodeError) as error:
            raise GitError("Git returned an invalid index object inventory") from error
        if (
            len(object_id) != expected_length
            or not _OID_PATTERN.fullmatch(object_id)
            or object_id in seen
            or kind != "blob"
            or not length_text.isdecimal()
        ):
            raise GitError("Git returned an invalid blob inventory")
        byte_length = int(length_text)
        if byte_length > limits.max_object_bytes:
            raise GitError("index blob exceeded inflated byte limit")
        inflated_bytes += byte_length
        if inflated_bytes > limits.max_inflated_bytes:
            raise GitError("index blobs exceeded aggregate inflated byte limit")
        seen.add(object_id)
        result.append(PackedObject(object_id=object_id, kind="blob", byte_length=byte_length))
    return tuple(sorted(result, key=lambda item: item.object_id))


def _check_pack_header(
    content: bytes,
    *,
    object_format: Literal["sha1", "sha256"],
    limits: IndexObjectPackLimits,
) -> int:
    digest_length = 20 if object_format == "sha1" else 32
    if len(content) > limits.max_pack_bytes:
        raise GitError("index object pack exceeded byte limit")
    if len(content) < 12 + digest_length or content[:4] != b"PACK":
        raise GitError("index object pack header is invalid")
    version = int.from_bytes(content[4:8], "big")
    if version not in (2, 3):
        raise GitError("index object pack version is unsupported")
    object_count = int.from_bytes(content[8:12], "big")
    if object_count > limits.max_objects:
        raise GitError("index object pack count exceeded limit")
    return object_count


async def verify_index_object_pack(
    content: bytes,
    *,
    object_format: Literal["sha1", "sha256"],
    expected_oids: Sequence[str],
    limits: IndexObjectPackLimits = DEFAULT_INDEX_OBJECT_PACK_LIMITS,
) -> VerifiedIndexObjectPack:
    """Verify untrusted supplemental pack bytes in an empty bare repository."""
    if type(content) is not bytes:
        raise TypeError("content must be bytes")
    validated_format = _validate_object_format(object_format)
    expected = _validate_oids(expected_oids, object_format=validated_format, limits=limits)
    object_count = _check_pack_header(content, object_format=validated_format, limits=limits)
    if object_count != len(expected):
        raise GitError("index object pack inventory count differs from expectation")
    with tempfile.TemporaryDirectory(prefix="yinshi-index-pack-") as directory:
        os.chmod(directory, 0o700)
        repository = Path(directory) / "verification.git"
        await run_git_bytes(
            ["init", "--bare", f"--object-format={validated_format}", str(repository)],
            env=_GIT_ENV,
            stdout_bytes_max=limits.max_listing_bytes,
        )
        await run_git_bytes(
            ["index-pack", "--strict", "--stdin"],
            cwd=str(repository),
            env=_GIT_ENV,
            stdin_bytes=content,
            stdout_bytes_max=128,
        )
        inventory_raw = await run_git_bytes(
            [
                "cat-file",
                "--batch-all-objects",
                "--batch-check=%(objectname) %(objecttype) %(objectsize)",
            ],
            cwd=str(repository),
            env=_GIT_ENV,
            stdout_bytes_max=limits.max_listing_bytes,
        )
        objects = _parse_inventory(
            inventory_raw,
            object_format=validated_format,
            limits=limits,
        )
    if tuple(item.object_id for item in objects) != expected:
        raise GitError("index object pack inventory differs from expectation")
    return VerifiedIndexObjectPack(
        object_format=validated_format,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
        objects=objects,
    )


async def create_index_object_pack(
    repository: str | Path,
    required_oids: Sequence[str],
    committed_objects: Sequence[GitObject],
    *,
    limits: IndexObjectPackLimits = DEFAULT_INDEX_OBJECT_PACK_LIMITS,
) -> CreatedIndexObjectPack:
    """Create and consumer-verify the exact index blobs absent from a bundle."""
    if not isinstance(repository, (str, Path)):
        raise TypeError("repository must be a path")
    repo_path = os.fspath(repository)
    object_format = await _repository_object_format(repo_path)
    required = _validate_oids(required_oids, object_format=object_format, limits=limits)
    try:
        committed = tuple(committed_objects)
    except TypeError as error:
        raise TypeError("committed_objects must be a finite sequence") from error
    committed_by_id = {item.object_id: item for item in committed if type(item) is GitObject}
    if len(committed_by_id) != len(committed):
        raise ValueError("committed object inventory is invalid or duplicated")
    _validate_oids(
        tuple(committed_by_id),
        object_format=object_format,
        limits=limits,
    )
    for object_id in required:
        bundled = committed_by_id.get(object_id)
        if bundled is not None and bundled.kind != "blob":
            raise GitError("required index object is not a blob")
    missing = tuple(object_id for object_id in required if object_id not in committed_by_id)
    input_bytes = ("\n".join(missing) + ("\n" if missing else "")).encode("ascii")
    if missing:
        preflight_raw = await run_git_bytes(
            ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
            cwd=repo_path,
            env=_GIT_ENV,
            stdin_bytes=input_bytes,
            stdout_bytes_max=limits.max_listing_bytes,
        )
        preflight = _parse_inventory(
            preflight_raw,
            object_format=object_format,
            limits=limits,
        )
        if tuple(item.object_id for item in preflight) != missing:
            raise GitError("required index blob inventory differs from source")
    pack_bytes = await run_git_bytes(
        [
            "pack-objects",
            "--stdout",
            "--no-reuse-delta",
            "--no-reuse-object",
            "--window=0",
        ],
        cwd=repo_path,
        env=_GIT_ENV,
        stdin_bytes=input_bytes,
        stdout_bytes_max=limits.max_pack_bytes,
    )
    verified = await verify_index_object_pack(
        pack_bytes,
        object_format=object_format,
        expected_oids=missing,
        limits=limits,
    )
    return CreatedIndexObjectPack(
        object_format=object_format,
        pack_bytes=pack_bytes,
        sha256=verified.sha256,
        byte_length=verified.byte_length,
        objects=verified.objects,
    )
