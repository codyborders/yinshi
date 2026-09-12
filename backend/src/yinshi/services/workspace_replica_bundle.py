"""Create and verify bounded Git bundles containing committed refs only."""

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

_OID_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REF_PATTERN: Final[re.Pattern[str]] = re.compile(r"refs/[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_NO_REPLACE_ENV: Final[dict[str, str]] = {"GIT_NO_REPLACE_OBJECTS": "1"}


@dataclass(frozen=True)
class BundleLimits:
    """Bounds for bundle bytes, refs, names, and Git listing output."""

    max_bundle_bytes: int = 512 * 1024 * 1024
    max_refs: int = 64
    max_ref_name_bytes: int = 4096
    max_listing_bytes: int = 64 * 4096

    def __post_init__(self) -> None:
        for name in (
            "max_bundle_bytes",
            "max_refs",
            "max_ref_name_bytes",
            "max_listing_bytes",
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


async def _verify_bundle(
    content: bytes,
    limits: BundleLimits,
    object_format: Literal["sha1", "sha256"],
) -> tuple[BundleRef, ...]:
    with tempfile.TemporaryDirectory(prefix="yinshi-bundle-") as directory:
        os.chmod(directory, 0o700)
        root = Path(directory)
        repository = root / "verification.git"
        await run_git_bytes(
            ["init", "--bare", f"--object-format={object_format}", str(repository)],
            env=_NO_REPLACE_ENV,
            stdout_bytes_max=limits.max_listing_bytes,
        )
        path = root / "committed.bundle"
        _write_private_bundle(path, content)
        await run_git_bytes(
            ["bundle", "verify", str(path)],
            cwd=str(repository),
            env=_NO_REPLACE_ENV,
            stdout_bytes_max=limits.max_listing_bytes,
        )
        advertised = await run_git_bytes(
            ["bundle", "list-heads", str(path)],
            cwd=str(repository),
            env=_NO_REPLACE_ENV,
            stdout_bytes_max=limits.max_listing_bytes,
        )
        return _parse_advertised_refs(advertised, limits)


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
    advertised = await _verify_bundle(content, limits, object_format)

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
        head=head_before,
    )
