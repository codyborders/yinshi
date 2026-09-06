"""Bind thread Git namespaces to one physical repository and selected database.

Ownership records contain hashes, not credentials or operation queues. The
physical lifecycle lock encloses record checks, database claims, and Git work.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

from yinshi.exceptions import YinshiError
from yinshi.services.git import run_git_bytes
from yinshi.services.repository_lifecycle import repository_lifecycle

_OWNER_RECORD = ".yinshi-thread-owner-v1.json"


class ThreadGitOwnershipError(YinshiError):
    """Physical Git ownership is missing, unsafe, or belongs elsewhere."""

    def __init__(self) -> None:
        super().__init__("Thread Git storage ownership is unavailable.")


@dataclass(frozen=True)
class ThreadGitWorktree:
    """Recorded ownership for one internal path, before Git registration checks."""

    delegation_id: str
    namespace: str
    path: str = field(repr=False)


@dataclass(frozen=True)
class ThreadGitClaim:
    """Backend claim callback executed while physical Git ownership is locked."""

    database_identity: str = field(repr=False)
    claim_namespace: Callable[[str], Awaitable[None]] = field(repr=False)
    record_snapshot: Callable[[str, str, str], Awaitable[None]] | None = field(
        default=None, repr=False
    )
    owned_worktrees: Callable[[], Awaitable[tuple[ThreadGitWorktree, ...]]] | None = field(
        default=None, repr=False
    )


@dataclass(frozen=True)
class ThreadGitFinalization:
    """Validate recorded ownership before publishing an immutable result."""

    database_identity: str = field(repr=False)
    namespace: str
    validate_claim: Callable[[], Awaitable[None]] = field(repr=False)


@dataclass(frozen=True)
class ThreadGitCleanup:
    """Validate and release one durable claim under its physical Git lock."""

    database_identity: str = field(repr=False)
    namespace: str
    validate_claim: Callable[[], Awaitable[bool]] = field(repr=False)
    release_claim: Callable[[], Awaitable[None]] = field(repr=False)


def _identity_hash(path: str) -> str:
    return hashlib.sha256(os.fsencode(path)).hexdigest()


async def _common_directory(repo_path: str) -> Path:
    output = await run_git_bytes(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo_path,
    )
    raw_path = output[:-1] if output.endswith(b"\n") else output
    try:
        common = Path(os.fsdecode(raw_path)).resolve(strict=True)
        if not common.is_dir():
            raise ThreadGitOwnershipError()
        return common
    except (OSError, ValueError) as exc:
        raise ThreadGitOwnershipError() from exc


def _owner_payload(common: Path, database_identity: str) -> dict[str, object]:
    database = Path(database_identity).resolve(strict=True)
    return {
        "version": 1,
        "database_hash": _identity_hash(str(database)),
        "common_directory_hash": _identity_hash(str(common)),
    }


def _read_record(directory_fd: int, expected: dict[str, object]) -> bool:
    try:
        fd = os.open(
            _OWNER_RECORD,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ThreadGitOwnershipError()
        if info.st_size > 1024:
            raise ThreadGitOwnershipError()
        payload = json.loads(os.read(fd, 1025))
        if (
            not isinstance(payload, dict)
            or type(payload.get("version")) is not int
            or payload != expected
        ):
            raise ThreadGitOwnershipError()
        return True
    finally:
        os.close(fd)


def _check_record(common: Path, expected: dict[str, object], *, create: bool) -> bool:
    """Publish complete metadata without replacing an existing directory entry."""
    directory_fd = os.open(common, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary: str | None = None
    try:
        if _read_record(directory_fd, expected):
            return True
        if not create:
            return False
        candidate = f".yinshi-thread-owner-{uuid.uuid4().hex}.tmp"
        fd = os.open(
            candidate,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        temporary = candidate
        try:
            raw = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("ascii")
            remaining = memoryview(raw)
            while remaining:
                written = os.write(fd, remaining)
                if written == 0:
                    raise ThreadGitOwnershipError()
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(
                temporary,
                _OWNER_RECORD,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.fsync(directory_fd)
        except FileExistsError:
            if not _read_record(directory_fd, expected):
                raise ThreadGitOwnershipError() from None
        return True
    finally:
        try:
            if temporary is not None:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)


async def _record_operation(common: Path, expected: dict[str, object], *, create: bool) -> bool:
    """Drain publication before releasing its lifecycle lock on cancellation."""
    task = asyncio.create_task(asyncio.to_thread(_check_record, common, expected, create=create))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except (OSError, ValueError, ThreadGitOwnershipError):
                break
        with suppress(OSError, ValueError, ThreadGitOwnershipError):
            task.result()
        raise
    except (OSError, ValueError) as exc:
        raise ThreadGitOwnershipError() from exc


def _legacy_worktree_directory(repo_path: str) -> bool:
    try:
        with os.scandir(Path(repo_path) / ".worktrees" / "yinshi") as entries:
            for index, entry in enumerate(entries):
                if index >= 128 or entry.name.startswith("thread-"):
                    return True
        return False
    except FileNotFoundError:
        return False


def _legacy_ref_metadata(common: Path) -> bool:
    """Detect loose metadata that Git omits for broken or dangling references."""
    directories = (
        "refs",
        "refs/heads",
        "refs/heads/yinshi",
        "refs/yinshi",
        "refs/yinshi/snapshots",
        "refs/yinshi/results",
    )
    for relative in directories:
        path = common / relative
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            return True
    for relative, prefix in (
        ("refs/heads/yinshi", "thread-"),
        ("refs/yinshi/snapshots", ""),
        ("refs/yinshi/results", ""),
    ):
        try:
            with os.scandir(common / relative) as entries:
                for index, entry in enumerate(entries):
                    if index >= 128 or entry.name.startswith(prefix):
                        return True
        except FileNotFoundError:
            continue
    return False


async def _unknown_thread_artifacts(repo_path: str, common: Path) -> bool:
    refs = await run_git_bytes(
        [
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads/yinshi/thread-*",
            "refs/yinshi/snapshots/",
            "refs/yinshi/results/",
        ],
        cwd=repo_path,
    )
    if refs:
        return True
    if await asyncio.to_thread(_legacy_ref_metadata, common):
        return True
    return await asyncio.to_thread(_legacy_worktree_directory, repo_path)


def _storage_node(path: Path, *, directory: bool) -> os.stat_result | None:
    """Check a mutation target without following a symbolic link."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode):
        raise ThreadGitOwnershipError()
    return info


def _validate_storage_layout(common: Path) -> None:
    """Reject redirected reference, object, and worktree metadata storage."""
    for relative in (
        "objects",
        "objects/info",
        "objects/pack",
        "refs",
        "refs/heads",
        "refs/heads/yinshi",
        "refs/yinshi",
        "refs/yinshi/snapshots",
        "refs/yinshi/results",
        "worktrees",
        "logs",
        "logs/refs",
        "logs/refs/heads",
        "logs/refs/heads/yinshi",
        "logs/refs/yinshi",
        "logs/refs/yinshi/snapshots",
        "logs/refs/yinshi/results",
    ):
        _storage_node(common / relative, directory=True)
    for relative in (
        "logs/refs/heads/yinshi",
        "logs/refs/yinshi/snapshots",
        "logs/refs/yinshi/results",
    ):
        try:
            with os.scandir(common / relative) as entries:
                for entry in entries:
                    _storage_node(Path(entry.path), directory=False)
        except FileNotFoundError:
            pass
    _storage_node(common / "packed-refs", directory=False)
    for prefix in range(256):
        _storage_node(common / "objects" / f"{prefix:02x}", directory=True)
    alternates = _storage_node(common / "objects/info/alternates", directory=False)
    if alternates is not None and alternates.st_size:
        raise ThreadGitOwnershipError()
    try:
        with os.scandir(common / "worktrees") as entries:
            for entry in entries:
                metadata = Path(entry.path)
                _storage_node(metadata, directory=True)
                _storage_node(metadata / "logs", directory=True)
                for name in ("HEAD", "index", "commondir", "gitdir", "logs/HEAD"):
                    _storage_node(metadata / name, directory=False)
    except FileNotFoundError:
        pass


def _check_workspace_binding(
    workspace_path: str, common: Path, git_directory: bytes, top_level: bytes
) -> None:
    """Require a real checkout or a linked checkout with the exact backlink."""
    workspace = Path(workspace_path).resolve(strict=True)
    git_dir = Path(os.fsdecode(git_directory.removesuffix(b"\n"))).resolve(strict=True)
    actual_root = Path(os.fsdecode(top_level.removesuffix(b"\n"))).resolve(strict=True)
    if actual_root != workspace:
        raise ThreadGitOwnershipError()
    descriptor = workspace / ".git"
    info = os.lstat(descriptor)
    if stat.S_ISDIR(info.st_mode):
        if descriptor.resolve(strict=True) != common or git_dir != common:
            raise ThreadGitOwnershipError()
        return
    if not stat.S_ISREG(info.st_mode) or git_dir.parent != common / "worktrees":
        raise ThreadGitOwnershipError()
    backlink = git_dir / "gitdir"
    recorded = _storage_node(backlink, directory=False)
    if recorded is None or recorded.st_size > 4096:
        raise ThreadGitOwnershipError()
    descriptor_fd = os.open(backlink, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        value = os.read(descriptor_fd, 4097)
    finally:
        os.close(descriptor_fd)
    if not value or len(value) > 4096:
        raise ThreadGitOwnershipError()
    linked_path = Path(os.fsdecode(value.removesuffix(b"\n")))
    if not linked_path.is_absolute():
        linked_path = git_dir / linked_path
    if linked_path.resolve(strict=True) != descriptor:
        raise ThreadGitOwnershipError()


async def _validate_workspace_binding(workspace_path: str, common: Path) -> None:
    git_directory = await run_git_bytes(["rev-parse", "--absolute-git-dir"], cwd=workspace_path)
    top_level = await run_git_bytes(
        ["rev-parse", "--path-format=absolute", "--show-toplevel"], cwd=workspace_path
    )
    await asyncio.to_thread(
        _check_workspace_binding, workspace_path, common, git_directory, top_level
    )


async def verify_thread_git_workspace(workspace_path: str, branch: str, namespace: str) -> None:
    """Validate a checkout under an already held physical namespace lock."""
    try:
        common = await _common_directory(workspace_path)
        if thread_git_claim_namespace(common, branch) != namespace:
            raise ThreadGitOwnershipError()
        await _validate_workspace_binding(workspace_path, common)
    except (OSError, ValueError) as exc:
        raise ThreadGitOwnershipError() from exc


def thread_git_claim_namespace(common: Path, branch: str) -> str:
    """Identify a branch inside an already canonical physical common directory."""
    return hashlib.sha256(os.fsencode(str(common)) + b"\0" + branch.encode("utf-8")).hexdigest()


@asynccontextmanager
async def thread_git_namespace(
    repo_path: str,
    branch: str,
    database_identity: str,
    *,
    create_owner: bool,
) -> AsyncIterator[str]:
    """Lock physical Git identity before checking persistent database ownership.

    Callers hold the existing logical repository lock first. No caller may
    acquire a logical repository lock while holding this physical lock.
    """
    common = await _common_directory(repo_path)
    try:
        expected = await asyncio.to_thread(_owner_payload, common, database_identity)
        async with repository_lifecycle("yinshi-thread-git", common):
            if await _common_directory(repo_path) != common:
                raise ThreadGitOwnershipError()
            await asyncio.to_thread(_validate_storage_layout, common)
            await _validate_workspace_binding(repo_path, common)
            exists = await _record_operation(common, expected, create=False)
            if not exists:
                if not create_owner or await _unknown_thread_artifacts(repo_path, common):
                    raise ThreadGitOwnershipError()
                await _record_operation(common, expected, create=True)
            yield thread_git_claim_namespace(common, branch)
    except (OSError, ValueError) as exc:
        raise ThreadGitOwnershipError() from exc
