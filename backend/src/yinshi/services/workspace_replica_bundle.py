"""Create and verify bounded Git bundles containing committed refs only."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TypeVar

from yinshi.exceptions import GitError
from yinshi.services.git import run_git_bytes

_OID_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REF_PATTERN: Final[re.Pattern[str]] = re.compile(r"refs/[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_NO_REPLACE_ENV: Final[dict[str, str]] = {
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
}
_OBJECT_KINDS: Final[frozenset[str]] = frozenset({"blob", "commit", "tag", "tree"})
_T = TypeVar("_T")


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


@dataclass(frozen=True)
class BundleLimits:
    """Bounds for bundle bytes, refs, names, and Git listing output."""

    max_bundle_bytes: int = 512 * 1024 * 1024
    max_refs: int = 64
    max_ref_name_bytes: int = 4096
    max_listing_bytes: int = 64 * 4096
    max_objects: int = 1_000_000
    max_object_bytes: int = 512 * 1024 * 1024
    max_inflated_bytes: int = 2 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_bundle_bytes",
            "max_refs",
            "max_ref_name_bytes",
            "max_listing_bytes",
            "max_objects",
            "max_object_bytes",
            "max_inflated_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_BUNDLE_LIMITS: Final[BundleLimits] = BundleLimits()


@dataclass(frozen=True)
class BundleRef:
    """One exact advertised ref and object ID."""

    name: str
    object_id: str


@dataclass(frozen=True)
class GitObject:
    """One object physically present in a verified transport."""

    object_id: str
    kind: Literal["blob", "commit", "tag", "tree"]
    byte_length: int


@dataclass(frozen=True)
class VerifiedCommittedBundle:
    """Bounded metadata from consumer-side committed-bundle verification."""

    object_format: Literal["sha1", "sha256"]
    byte_length: int
    sha256: str
    refs: tuple[BundleRef, ...]
    objects: tuple[GitObject, ...]


@dataclass(frozen=True)
class HeadCapture:
    """Committed HEAD state captured around bundle creation."""

    kind: Literal["symbolic", "detached"]
    target: str | None
    object_id: str


@dataclass(frozen=True)
class CommittedBundle:
    """Verified bounded bundle bytes and their exact committed names."""

    object_format: Literal["sha1", "sha256"]
    bundle_bytes: bytes
    byte_length: int
    sha256: str
    refs: tuple[BundleRef, ...]
    objects: tuple[GitObject, ...]
    head: HeadCapture | None


def _validate_ref_name(name: object, limits: BundleLimits) -> str:
    if type(name) is not str or not _REF_PATTERN.fullmatch(name):
        raise ValueError("bundle ref must be a full canonical ref name")
    encoded = name.encode("ascii", "strict")
    components = name.split("/")
    if (
        len(encoded) > limits.max_ref_name_bytes
        or ".." in name
        or "@{" in name
        or name.endswith(".")
        or any(not component for component in components)
        or any(component.startswith(".") for component in components)
        or any(component.endswith(".lock") for component in components)
    ):
        raise ValueError("bundle ref name is not canonical")
    return name


def _parse_oid(raw: bytes) -> str:
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise GitError("Git returned an invalid object ID") from error
    if not _OID_PATTERN.fullmatch(value) or not any(character != "0" for character in value):
        raise GitError("Git returned an invalid object ID")
    return value


async def _capture_object_format(repo_path: str) -> Literal["sha1", "sha256"]:
    raw = await run_git_bytes(
        ["rev-parse", "--show-object-format"],
        cwd=repo_path,
        env=_NO_REPLACE_ENV,
        stdout_bytes_max=8,
    )
    value = raw.decode("ascii", "strict").strip()
    if value == "sha1":
        return "sha1"
    if value == "sha256":
        return "sha256"
    raise GitError("Git returned an unsupported object format")


async def _require_commit(repo_path: str, revision: str) -> None:
    await run_git_bytes(
        ["rev-parse", "--verify", f"{revision}^{{commit}}"],
        cwd=repo_path,
        env=_NO_REPLACE_ENV,
        stdout_bytes_max=65,
    )


async def _capture_ref(repo_path: str, name: str, limits: BundleLimits) -> BundleRef:
    await run_git_bytes(
        ["check-ref-format", name],
        cwd=repo_path,
        env=_NO_REPLACE_ENV,
        stdout_bytes_max=limits.max_ref_name_bytes,
    )
    raw = await run_git_bytes(
        ["rev-parse", "--verify", name],
        cwd=repo_path,
        env=_NO_REPLACE_ENV,
        stdout_bytes_max=65,
    )
    await _require_commit(repo_path, name)
    return BundleRef(name=name, object_id=_parse_oid(raw))


async def _capture_head(repo_path: str, limits: BundleLimits) -> HeadCapture | None:
    symbolic_raw = await run_git_bytes(
        ["symbolic-ref", "--quiet", "--no-recurse", "HEAD"],
        cwd=repo_path,
        env=_NO_REPLACE_ENV,
        stdout_bytes_max=limits.max_ref_name_bytes + 1,
        accepted_returncodes=(0, 1),
    )
    if symbolic_raw:
        try:
            target_text = symbolic_raw.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise GitError("Git returned an invalid HEAD target") from error
        target = _validate_ref_name(target_text, limits)
        raw = await run_git_bytes(
            ["for-each-ref", "--format=%(objectname)", target],
            cwd=repo_path,
            env=_NO_REPLACE_ENV,
            stdout_bytes_max=65,
        )
        if not raw.strip():
            return None
        await _require_commit(repo_path, target)
        return HeadCapture(kind="symbolic", target=target, object_id=_parse_oid(raw))
    raw = await run_git_bytes(
        ["rev-parse", "--verify", "HEAD"],
        cwd=repo_path,
        env=_NO_REPLACE_ENV,
        stdout_bytes_max=65,
    )
    await _require_commit(repo_path, "HEAD")
    return HeadCapture(kind="detached", target=None, object_id=_parse_oid(raw))


def _write_private_bundle(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        view = memoryview(content)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise GitError("Git bundle write did not make progress")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_advertised_refs(raw: bytes, limits: BundleLimits) -> tuple[BundleRef, ...]:
    if len(raw) > limits.max_listing_bytes:
        raise GitError("Git bundle ref listing exceeded limit")
    lines = raw.splitlines()
    if len(lines) > limits.max_refs + 1:
        raise GitError("Git bundle advertised too many refs")
    refs: list[BundleRef] = []
    seen: set[str] = set()
    for line in lines:
        try:
            oid_raw, name_raw = line.split(b" ", 1)
            name = name_raw.decode("ascii")
        except (ValueError, UnicodeDecodeError) as error:
            raise GitError("Git bundle advertised an invalid ref") from error
        if name != "HEAD":
            _validate_ref_name(name, limits)
        if name in seen:
            raise GitError("Git bundle advertised a duplicate ref")
        seen.add(name)
        refs.append(BundleRef(name=name, object_id=_parse_oid(oid_raw)))
    return tuple(sorted(refs, key=lambda item: item.name))


def _parse_object_inventory(raw: bytes, limits: BundleLimits) -> tuple[GitObject, ...]:
    if len(raw) > limits.max_listing_bytes:
        raise GitError("Git object inventory exceeded listing limit")
    lines = raw.splitlines()
    if len(lines) > limits.max_objects:
        raise GitError("Git object inventory exceeded object limit")
    objects: list[GitObject] = []
    seen: set[str] = set()
    total_bytes = 0
    for line in lines:
        try:
            oid_raw, kind_raw, length_raw = line.split(b" ", 2)
            kind = kind_raw.decode("ascii")
            length_text = length_raw.decode("ascii")
        except (ValueError, UnicodeDecodeError) as error:
            raise GitError("Git returned an invalid object inventory") from error
        object_id = _parse_oid(oid_raw)
        if object_id in seen or kind not in _OBJECT_KINDS or not length_text.isdecimal():
            raise GitError("Git returned an invalid object inventory")
        byte_length = int(length_text)
        if byte_length > limits.max_object_bytes:
            raise GitError("Git object exceeded inflated byte limit")
        total_bytes += byte_length
        if total_bytes > limits.max_inflated_bytes:
            raise GitError("Git objects exceeded aggregate inflated byte limit")
        seen.add(object_id)
        objects.append(
            GitObject(
                object_id=object_id,
                kind=kind,  # type: ignore[arg-type]
                byte_length=byte_length,
            )
        )
    return tuple(sorted(objects, key=lambda item: item.object_id))


def _hash_descriptor(descriptor: int, byte_length: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < byte_length:
        content = os.pread(descriptor, min(1024 * 1024, byte_length - offset), offset)
        if not content:
            raise GitError("Git bundle is truncated")
        digest.update(content)
        offset += len(content)
    if os.pread(descriptor, 1, byte_length):
        raise GitError("Git bundle grew during verification")
    return digest.hexdigest()


def _validate_pinned_bundle(
    path: Path,
    descriptor: int,
    *,
    byte_length: int,
    sha256: str,
    limits: BundleLimits,
) -> tuple[int, int]:
    if not path.is_absolute() or type(descriptor) is not int or descriptor < 0:
        raise ValueError("bundle path and descriptor are invalid")
    if type(byte_length) is not int or not 0 < byte_length <= limits.max_bundle_bytes:
        raise GitError("Git bundle exceeded byte limit")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("bundle digest is invalid")
    opened = os.fstat(descriptor)
    named = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        or opened.st_uid != os.geteuid()
        or opened.st_gid != os.getegid()
        or named.st_uid != os.geteuid()
        or named.st_gid != os.getegid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or stat.S_IMODE(named.st_mode) != 0o600
        or opened.st_nlink != 1
        or named.st_nlink != 1
        or opened.st_size != byte_length
        or named.st_size != byte_length
        or _hash_descriptor(descriptor, byte_length) != sha256
    ):
        raise GitError("Git bundle file is not an exact private regular file")
    return opened.st_dev, opened.st_ino


def _recheck_pinned_bundle(
    path: Path,
    descriptor: int,
    *,
    identity: tuple[int, int],
    byte_length: int,
    sha256: str,
    limits: BundleLimits,
) -> None:
    if (
        _validate_pinned_bundle(
            path,
            descriptor,
            byte_length=byte_length,
            sha256=sha256,
            limits=limits,
        )
        != identity
    ):
        raise GitError("Git bundle changed during verification")


async def _verify_committed_bundle_path(
    path: Path,
    *,
    object_format: Literal["sha1", "sha256"],
    byte_length: int,
    sha256: str,
    limits: BundleLimits,
) -> VerifiedCommittedBundle:
    with tempfile.TemporaryDirectory(prefix="yinshi-bundle-") as directory:
        os.chmod(directory, 0o700)
        repository = Path(directory) / "verification.git"
        await run_git_bytes(
            ["init", "--bare", f"--object-format={object_format}", str(repository)],
            env=_NO_REPLACE_ENV,
            stdout_bytes_max=limits.max_listing_bytes,
        )
        await run_git_bytes(
            ["bundle", "verify", str(path)],
            cwd=str(repository),
            env=_NO_REPLACE_ENV,
            stdout_bytes_max=limits.max_listing_bytes,
        )
        advertised_raw = await run_git_bytes(
            ["bundle", "list-heads", str(path)],
            cwd=str(repository),
            env=_NO_REPLACE_ENV,
            stdout_bytes_max=limits.max_listing_bytes,
        )
        advertised = _parse_advertised_refs(advertised_raw, limits)
        if not advertised:
            raise GitError("Git bundle advertised no committed refs")
        if any(item.name.startswith("refs/replace/") for item in advertised):
            raise GitError("Git bundle advertised a replacement ref")
        await run_git_bytes(
            ["bundle", "unbundle", str(path)],
            cwd=str(repository),
            env=_NO_REPLACE_ENV,
            stdout_bytes_max=limits.max_listing_bytes,
        )
        for item in advertised:
            await _require_commit(str(repository), item.object_id)
        inventory_raw = await run_git_bytes(
            [
                "cat-file",
                "--batch-all-objects",
                "--batch-check=%(objectname) %(objecttype) %(objectsize)",
            ],
            cwd=str(repository),
            env=_NO_REPLACE_ENV,
            stdout_bytes_max=limits.max_listing_bytes,
        )
        objects = _parse_object_inventory(inventory_raw, limits)
        closure_raw = await run_git_bytes(
            ["rev-list", "--objects", "--no-object-names", "--stdin"],
            cwd=str(repository),
            env=_NO_REPLACE_ENV,
            stdin_bytes=("\n".join(item.object_id for item in advertised) + "\n").encode("ascii"),
            stdout_bytes_max=limits.max_listing_bytes,
        )
        closure = {_parse_oid(line) for line in closure_raw.splitlines()}
        if closure != {item.object_id for item in objects}:
            raise GitError("Git bundle contains missing or unreachable objects")
        return VerifiedCommittedBundle(
            object_format=object_format,
            byte_length=byte_length,
            sha256=sha256,
            refs=advertised,
            objects=objects,
        )


async def verify_committed_bundle_file(
    path: str | Path,
    descriptor: int,
    *,
    object_format: Literal["sha1", "sha256"],
    byte_length: int,
    sha256: str,
    limits: BundleLimits = DEFAULT_BUNDLE_LIMITS,
) -> VerifiedCommittedBundle:
    """Verify one descriptor-pinned private bundle without loading its bytes."""
    if object_format not in ("sha1", "sha256"):
        raise ValueError("object_format must be sha1 or sha256")
    bundle_path = Path(path)
    identity = await _run_blocking(
        _validate_pinned_bundle,
        bundle_path,
        descriptor,
        byte_length=byte_length,
        sha256=sha256,
        limits=limits,
    )
    verified = await _verify_committed_bundle_path(
        bundle_path,
        object_format=object_format,
        byte_length=byte_length,
        sha256=sha256,
        limits=limits,
    )
    await _run_blocking(
        _recheck_pinned_bundle,
        bundle_path,
        descriptor,
        identity=identity,
        byte_length=byte_length,
        sha256=sha256,
        limits=limits,
    )
    return verified


async def verify_committed_bundle(
    content: bytes,
    *,
    object_format: Literal["sha1", "sha256"],
    limits: BundleLimits = DEFAULT_BUNDLE_LIMITS,
) -> VerifiedCommittedBundle:
    """Verify untrusted committed-bundle bytes in an empty bare repository."""
    if type(content) is not bytes:
        raise TypeError("content must be bytes")
    if object_format not in ("sha1", "sha256"):
        raise ValueError("object_format must be sha1 or sha256")
    if not content:
        raise GitError("Git bundle is empty")
    if len(content) > limits.max_bundle_bytes:
        raise GitError("Git bundle exceeded byte limit")
    with tempfile.TemporaryDirectory(prefix="yinshi-bundle-input-") as directory:
        os.chmod(directory, 0o700)
        path = Path(directory) / "committed.bundle"
        _write_private_bundle(path, content)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            return await verify_committed_bundle_file(
                path,
                descriptor,
                object_format=object_format,
                byte_length=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                limits=limits,
            )
        finally:
            os.close(descriptor)


async def create_committed_bundle(
    repository: str | Path,
    ref_names: Sequence[str],
    *,
    include_head: bool,
    limits: BundleLimits = DEFAULT_BUNDLE_LIMITS,
) -> CommittedBundle:
    """Create a bounded bundle, verify names, then confirm source refs stayed fixed."""
    if type(include_head) is not bool:
        raise TypeError("include_head must be Boolean")
    if not isinstance(repository, (str, Path)):
        raise TypeError("repository must be a path")
    repo_path = os.fspath(repository)
    try:
        requested = tuple(ref_names)
    except TypeError as error:
        raise TypeError("ref_names must be a finite sequence") from error
    if len(requested) > limits.max_refs:
        raise ValueError("bundle ref count exceeds maximum")
    validated = tuple(_validate_ref_name(name, limits) for name in requested)
    if len(set(validated)) != len(validated):
        raise ValueError("bundle ref name is duplicated")

    object_format = await _capture_object_format(repo_path)
    before = tuple([await _capture_ref(repo_path, name, limits) for name in validated])
    head_before = await _capture_head(repo_path, limits) if include_head else None
    revisions = list(validated)
    if head_before is not None:
        revisions.append("HEAD")
    if not revisions:
        raise GitError("repository has no committed refs to bundle")
    content = await run_git_bytes(
        [
            "bundle",
            "create",
            "--version=2" if object_format == "sha1" else "--version=3",
            "-",
            *revisions,
        ],
        cwd=repo_path,
        env=_NO_REPLACE_ENV,
        stdout_bytes_max=limits.max_bundle_bytes,
    )
    if not content:
        raise GitError("Git created an empty bundle")
    verified = await verify_committed_bundle(
        content,
        object_format=object_format,
        limits=limits,
    )
    advertised = verified.refs

    expected = list(before)
    if head_before is not None:
        expected.append(BundleRef(name="HEAD", object_id=head_before.object_id))
    expected_tuple = tuple(sorted(expected, key=lambda item: item.name))
    if advertised != expected_tuple:
        raise GitError("Git bundle advertised refs differ from captured refs")

    after = tuple([await _capture_ref(repo_path, name, limits) for name in validated])
    head_after = await _capture_head(repo_path, limits) if include_head else None
    if after != before or head_after != head_before:
        raise GitError("Git refs changed during bundle creation")
    if await _capture_object_format(repo_path) != object_format:
        raise GitError("Git object format changed during bundle creation")
    return CommittedBundle(
        object_format=object_format,
        bundle_bytes=content,
        byte_length=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        refs=advertised,
        objects=verified.objects,
        head=head_before,
    )
