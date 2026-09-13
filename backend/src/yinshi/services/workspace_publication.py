"""Durable private publication for repaired tenant Git checkouts."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TypeVar

from yinshi.exceptions import GitError
from yinshi.services.git import (
    _git_askpass_env,
    _retain_directory,
    _retain_directory_argument,
    _run_git,
    _StableDirectory,
    _validate_clone_url,
    ensure_remote_url,
    restore_worktree,
    validate_local_repo,
)
from yinshi.services.workspace import (
    WorkspaceCheckoutPreparation,
    WorkspaceCheckoutState,
    _refresh_repo_remote_metadata,
    _tenant_path_is_trusted,
    _tenant_repo_path,
    _workspace_path,
    prepare_workspace_checkout_for_tenant as _prepare_trusted_checkout,
)
from yinshi.services.workspace_admission import (
    WorkspaceAdmissionLease,
    acquire_workspace_admission,
    read_workspace_identity,
    resume_workspace_admission_marker,
)
from yinshi.services.workspace_isolation import require_isolated_execution
from yinshi.tenant import TenantContext

_BACKLINK_BYTES_MAX: Final[int] = 4096
_LINUX_RENAME_NOREPLACE: Final[int] = 1
_DARWIN_RENAME_EXCL: Final[int] = 0x00000004


class WorkspacePublicationError(RuntimeError):
    """A checkout cannot be published without weakening exclusion."""


class WorkspacePublicationCollisionError(WorkspacePublicationError):
    """Another filesystem object won the final publication name."""


def _directory_open_flags() -> int:
    """Return no-follow flags for one retained publication directory."""
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_owned_directory(path: Path, *, expected_identity: str | None = None) -> int:
    """Open one real directory and require stable ownership by this process."""
    flags = _directory_open_flags()
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkspacePublicationError("Checkout publication parent is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        named = os.lstat(path)
        if not stat.S_ISDIR(opened.st_mode):
            raise WorkspacePublicationError("Checkout publication parent is not a directory")
        if not stat.S_ISDIR(named.st_mode) or stat.S_ISLNK(named.st_mode):
            raise WorkspacePublicationError("Checkout publication parent is redirected")
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise WorkspacePublicationError("Checkout publication parent changed during open")
        if opened.st_uid != os.geteuid():
            raise WorkspacePublicationError("Checkout publication parent has a different owner")
        if opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise WorkspacePublicationError("Checkout publication parent lacks exclusive ownership")
        if expected_identity is not None:
            expected = json.loads(expected_identity)
            if not isinstance(expected, dict) or (opened.st_dev, opened.st_ino) != (
                expected.get("device"),
                expected.get("inode"),
            ):
                raise WorkspacePublicationError("Checkout publication object changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_owned_descendant(
    root: Path,
    descendant: Path,
    *,
    expected_root_identity: str | None = None,
) -> int:
    """Open an owned descendant by walking every component without following links."""
    try:
        relative = descendant.relative_to(root)
    except ValueError as exc:
        raise WorkspacePublicationError("Owned publication path leaves its root") from exc
    descriptor = _open_owned_directory(root, expected_identity=expected_root_identity)
    try:
        root_device = os.fstat(descriptor).st_dev
        for component in relative.parts:
            if component in {"", ".", ".."}:
                raise WorkspacePublicationError("Owned publication path is invalid")
            next_descriptor = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            opened = os.fstat(next_descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or opened.st_dev != root_device
            ):
                os.close(next_descriptor)
                raise WorkspacePublicationError("Owned publication ancestry is untrusted")
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise WorkspacePublicationError("Owned publication ancestry is redirected") from exc
    except BaseException:
        os.close(descriptor)
        raise


def _direct_child(path: Path) -> tuple[Path, bytes]:
    """Return one parent and a non-special encoded child name."""
    absolute = Path(os.path.abspath(path))
    name = absolute.name
    if not name or name in {".", ".."}:
        raise WorkspacePublicationError("Checkout publication path is invalid")
    encoded = os.fsencode(name)
    if b"/" in encoded or b"\x00" in encoded:
        raise WorkspacePublicationError("Checkout publication path name is invalid")
    return absolute.parent, encoded


def _raise_rename_error(error_number: int) -> None:
    """Map no-replace outcomes to stable publication errors."""
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise WorkspacePublicationCollisionError("Checkout publication target already exists")
    if error_number in {errno.ENOSYS, errno.EINVAL, getattr(errno, "EOPNOTSUPP", 95)}:
        raise WorkspacePublicationError("Atomic no-replace publication is unavailable")
    raise OSError(error_number, os.strerror(error_number))


def _linux_rename_no_replace(
    source_parent: int,
    source_name: bytes,
    target_parent: int,
    target_name: bytes,
) -> None:
    """Call Linux renameat2 with RENAME_NOREPLACE."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise WorkspacePublicationError("Atomic no-replace publication is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            source_parent,
            source_name,
            target_parent,
            target_name,
            _LINUX_RENAME_NOREPLACE,
        )
        != 0
    ):
        _raise_rename_error(ctypes.get_errno())


def _darwin_rename_no_replace(
    source_parent: int,
    source_name: bytes,
    target_parent: int,
    target_name: bytes,
) -> None:
    """Call Darwin renameatx_np with RENAME_EXCL."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(libc, "renameatx_np", None)
    if renameatx_np is None:
        raise WorkspacePublicationError("Atomic no-replace publication is unavailable")
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    if (
        renameatx_np(
            source_parent,
            source_name,
            target_parent,
            target_name,
            _DARWIN_RENAME_EXCL,
        )
        != 0
    ):
        _raise_rename_error(ctypes.get_errno())


def require_atomic_no_replace_support() -> None:
    """Fail before checkout work when the host lacks an approved primitive."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        if getattr(libc, "renameat2", None) is None:
            raise WorkspacePublicationError("Atomic no-replace publication is unavailable")
        return
    if sys.platform == "darwin":
        if getattr(libc, "renameatx_np", None) is None:
            raise WorkspacePublicationError("Atomic no-replace publication is unavailable")
        return
    raise WorkspacePublicationError("Checkout publication platform is unsupported")


def atomic_rename_no_replace(
    source: Path,
    target: Path,
    *,
    source_parent_identity: str | None = None,
    target_parent_identity: str | None = None,
    source_identity: str | None = None,
    allow_regular_file: bool = False,
) -> None:
    """Publish one exact directory or approved regular file without replacement."""
    require_atomic_no_replace_support()
    source_parent_path, source_name = _direct_child(source)
    target_parent_path, target_name = _direct_child(target)
    try:
        source_parent = _open_owned_directory(
            source_parent_path,
            expected_identity=source_parent_identity,
        )
    except WorkspacePublicationError as exc:
        raise WorkspacePublicationError("Checkout publication source parent changed") from exc
    try:
        try:
            target_parent = _open_owned_directory(
                target_parent_path,
                expected_identity=target_parent_identity,
            )
        except WorkspacePublicationError as exc:
            raise WorkspacePublicationError("Checkout publication target parent changed") from exc
        try:
            source_parent_stat = os.fstat(source_parent)
            target_parent_stat = os.fstat(target_parent)
            if source_parent_stat.st_dev != target_parent_stat.st_dev:
                raise WorkspacePublicationError(
                    "Checkout publication must remain on one filesystem"
                )
            try:
                source_flags = _directory_open_flags()
                if allow_regular_file:
                    source_flags = (
                        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                    )
                source_descriptor = os.open(
                    source_name,
                    source_flags,
                    dir_fd=source_parent,
                )
            except OSError as exc:
                raise WorkspacePublicationError(
                    "Checkout publication stage is unavailable"
                ) from exc
            try:
                source_stat = os.fstat(source_descriptor)
                named_source = os.stat(
                    source_name,
                    dir_fd=source_parent,
                    follow_symlinks=False,
                )
                source_type_is_valid = (
                    stat.S_ISREG(source_stat.st_mode)
                    if allow_regular_file
                    else stat.S_ISDIR(source_stat.st_mode)
                )
                if not source_type_is_valid:
                    raise WorkspacePublicationError("Checkout publication stage type is invalid")
                if source_stat.st_uid != os.geteuid():
                    raise WorkspacePublicationError(
                        "Checkout publication stage has a different owner"
                    )
                if source_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise WorkspacePublicationError(
                        "Checkout publication stage lacks exclusive ownership"
                    )
                if source_stat.st_dev != source_parent_stat.st_dev:
                    raise WorkspacePublicationError(
                        "Checkout publication must remain on one filesystem"
                    )
                if (named_source.st_dev, named_source.st_ino) != (
                    source_stat.st_dev,
                    source_stat.st_ino,
                ):
                    raise WorkspacePublicationError("Checkout publication stage changed")
                if source_identity is not None:
                    expected_source = json.loads(source_identity)
                    if not isinstance(expected_source, dict) or (
                        source_stat.st_dev,
                        source_stat.st_ino,
                    ) != (expected_source.get("device"), expected_source.get("inode")):
                        raise WorkspacePublicationError("Checkout publication stage changed")
                if sys.platform.startswith("linux"):
                    _linux_rename_no_replace(
                        source_parent,
                        source_name,
                        target_parent,
                        target_name,
                    )
                elif sys.platform == "darwin":
                    _darwin_rename_no_replace(
                        source_parent,
                        source_name,
                        target_parent,
                        target_name,
                    )
                else:
                    raise WorkspacePublicationError("Checkout publication platform is unsupported")
                published = os.stat(
                    target_name,
                    dir_fd=target_parent,
                    follow_symlinks=False,
                )
                if (published.st_dev, published.st_ino) != (
                    source_stat.st_dev,
                    source_stat.st_ino,
                ):
                    raise WorkspacePublicationError(
                        "Checkout publication published an unexpected object"
                    )
                os.fsync(target_parent)
                if source_parent_path != target_parent_path:
                    os.fsync(source_parent)
            finally:
                os.close(source_descriptor)
        finally:
            os.close(target_parent)
    finally:
        os.close(source_parent)


@contextmanager
def _retain_owned_regular_file(
    parent: int,
    name: str,
) -> Iterator[tuple[int, bytes, os.stat_result]]:
    """Retain one exact backlink under an exclusive parent authority."""
    parent_identity = os.fstat(parent)
    if (
        not stat.S_ISDIR(parent_identity.st_mode)
        or parent_identity.st_uid != os.geteuid()
        or parent_identity.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise WorkspacePublicationError("Git backlink parent lacks exclusive ownership")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as exc:
        raise WorkspacePublicationError("Git backlink could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise WorkspacePublicationError("Git backlink is not a private regular file")
        if opened.st_uid != os.geteuid() or opened.st_dev != parent_identity.st_dev:
            raise WorkspacePublicationError("Git backlink has a different owner")
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise WorkspacePublicationError("Git backlink changed during open")
        if opened.st_size < 1 or opened.st_size > _BACKLINK_BYTES_MAX:
            raise WorkspacePublicationError("Git backlink size is invalid")
        value = os.read(descriptor, _BACKLINK_BYTES_MAX + 1)
        if len(value) != opened.st_size:
            raise WorkspacePublicationError("Git backlink changed during read")
        yield descriptor, value, opened
    finally:
        os.close(descriptor)


def _read_owned_regular_file(parent: int, name: str) -> tuple[bytes, os.stat_result]:
    """Read one bounded no-follow file whose named identity stays stable."""
    with _retain_owned_regular_file(parent, name) as (_descriptor, value, opened):
        return value, opened


def _replace_owned_regular_file(parent: int, name: str, expected: bytes, value: bytes) -> None:
    """Replace one exact backlink while retaining its parent and old identity."""
    with _retain_owned_regular_file(parent, name) as (_accepted, current, identity):
        if current != expected:
            raise WorkspacePublicationError("Git backlink does not match its staged path")
        temporary_name = f".{name}.yinshi-{os.getpid()}-{identity.st_ino:x}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent)
        try:
            written = 0
            while written < len(value):
                count = os.write(descriptor, value[written:])
                if count < 1:
                    raise WorkspacePublicationError("Git backlink write made no progress")
                written += count
            os.fsync(descriptor)
            latest = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (latest.st_dev, latest.st_ino) != (identity.st_dev, identity.st_ino):
                raise WorkspacePublicationError("Git backlink changed before replacement")
            os.replace(temporary_name, name, src_dir_fd=parent, dst_dir_fd=parent)
            replacement = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            try:
                replaced = os.fstat(replacement)
                if replaced.st_dev != identity.st_dev or replaced.st_uid != os.geteuid():
                    raise WorkspacePublicationError("Git backlink replacement changed identity")
                if os.read(replacement, len(value) + 1) != value:
                    raise WorkspacePublicationError("Git backlink replacement changed content")
                os.fsync(replacement)
            finally:
                os.close(replacement)
            os.fsync(parent)
        finally:
            os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=parent)
            except FileNotFoundError:
                pass


def rewrite_linked_worktree_backlinks(
    *,
    staging_repo_path: Path,
    staging_workspace_path: Path,
    final_repo_path: Path,
    final_workspace_path: Path,
    staging_repo_identity: str | None = None,
) -> str:
    """Rewrite both linked-worktree backlinks to their post-rename locations."""
    staging_repo = Path(os.path.abspath(staging_repo_path))
    staging_workspace = Path(os.path.abspath(staging_workspace_path))
    final_repo = Path(os.path.abspath(final_repo_path))
    final_workspace = Path(os.path.abspath(final_workspace_path))
    try:
        workspace_parent = _open_owned_descendant(
            staging_repo,
            staging_workspace,
            expected_root_identity=staging_repo_identity,
        )
    except WorkspacePublicationError as exc:
        if staging_repo_identity is not None:
            raise WorkspacePublicationError(
                "Staged repository changed before backlink rewrite"
            ) from exc
        raise
    try:
        git_file, _identity = _read_owned_regular_file(workspace_parent, ".git")
        prefix = b"gitdir: "
        if not git_file.startswith(prefix) or not git_file.endswith(b"\n"):
            raise WorkspacePublicationError("Linked worktree Git file is invalid")
        encoded_registration = git_file[len(prefix) : -1]
        if b"\n" in encoded_registration or b"\x00" in encoded_registration:
            raise WorkspacePublicationError("Linked worktree registration path is invalid")
        observed_registration = Path(os.fsdecode(encoded_registration))
        if not observed_registration.is_absolute():
            raise WorkspacePublicationError("Linked worktree registration path is not absolute")
        try:
            registration_relative = observed_registration.relative_to(staging_repo / ".git")
        except ValueError:
            try:
                registration_relative = observed_registration.relative_to(final_repo / ".git")
            except ValueError as exc:
                raise WorkspacePublicationError(
                    "Linked worktree registration leaves owned metadata"
                ) from exc
        if len(registration_relative.parts) != 2 or registration_relative.parts[0] != "worktrees":
            raise WorkspacePublicationError("Linked worktree registration path is invalid")
        staging_registration = staging_repo / ".git" / registration_relative
        final_registration = final_repo / ".git" / registration_relative
        registration_parent = _open_owned_descendant(
            staging_repo,
            staging_registration,
            expected_root_identity=staging_repo_identity,
        )
        try:
            registration_identity = _directory_identity_from_stat(
                os.fstat(registration_parent), staging_registration
            )
            worktrees_descriptor = os.open(
                "..", _directory_open_flags(), dir_fd=registration_parent
            )
            try:
                registration_parent_identity = _directory_identity_from_stat(
                    os.fstat(worktrees_descriptor), staging_registration.parent
                )
                metadata_descriptor = os.open(
                    "..", _directory_open_flags(), dir_fd=worktrees_descriptor
                )
                try:
                    metadata_identity = _directory_identity_from_stat(
                        os.fstat(metadata_descriptor), staging_registration.parent.parent
                    )
                finally:
                    os.close(metadata_descriptor)
            finally:
                os.close(worktrees_descriptor)
            staged_gitdir = f"{staging_workspace / '.git'}\n".encode()
            final_gitdir = f"{final_workspace / '.git'}\n".encode()
            observed_gitdir, _gitdir_identity = _read_owned_regular_file(
                registration_parent, "gitdir"
            )
            if observed_gitdir == staged_gitdir:
                _replace_owned_regular_file(
                    registration_parent,
                    "gitdir",
                    staged_gitdir,
                    final_gitdir,
                )
            elif observed_gitdir != final_gitdir:
                raise WorkspacePublicationError("Git backlink does not match its owned path")
        finally:
            os.close(registration_parent)
        staged_git_file = f"gitdir: {staging_registration}\n".encode()
        final_git_file = f"gitdir: {final_registration}\n".encode()
        if git_file == staged_git_file:
            _replace_owned_regular_file(
                workspace_parent,
                ".git",
                staged_git_file,
                final_git_file,
            )
        elif git_file != final_git_file:
            raise WorkspacePublicationError("Git backlink does not match its owned path")
        workspace_identity = _directory_identity_from_stat(
            os.fstat(workspace_parent), staging_workspace
        )
    finally:
        os.close(workspace_parent)
    return json.dumps(
        {
            "metadata_identity": metadata_identity,
            "registration_identity": registration_identity,
            "registration_parent_identity": registration_parent_identity,
            "registration_path_final": str(final_registration),
            "registration_path_staging": str(staging_registration),
            "workspace_git_final": str(final_workspace / ".git"),
            "workspace_git_staging": str(staging_workspace / ".git"),
            "workspace_identity": workspace_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


# Publication orchestration stays below filesystem primitives. Callers provide
# database operations so no SQLite handle survives across Git or filesystem work.
_T = TypeVar("_T")


class DatabaseOperationRunner(Protocol):
    """Run one callback with a short-lived selected database connection."""

    async def __call__(self, operation: Callable[[sqlite3.Connection], _T]) -> _T: ...


CheckoutAuthorizer = Callable[[sqlite3.Connection], WorkspaceCheckoutState]


@dataclass
class WorkspaceCheckoutPublication:
    """Prepared checkout plus a held selected marker ready for caller binding."""

    preparation: WorkspaceCheckoutPreparation
    selected_lease: WorkspaceAdmissionLease
    selected_identity: str
    operation_id: str
    owner_token: str
    final_repo_identity: str

    async def complete(
        self,
        *,
        run_database_operation: DatabaseOperationRunner,
        authorize: CheckoutAuthorizer,
        release_marker: bool,
    ) -> None:
        """Finish logical binding after caller commits every repaired database path."""
        if release_marker:

            def authorize_release(database: sqlite3.Connection) -> None:
                current = authorize(database)
                if current.repo_path != self.preparation.repo_path:
                    raise WorkspacePublicationError("Checkout repair release authority changed")

            await run_database_operation(authorize_release)
            await self.selected_lease.release()

        def bind(database: sqlite3.Connection) -> None:
            current = authorize(database)
            if current.repo_path != self.preparation.repo_path:
                raise WorkspacePublicationError("Checkout repair binding was not committed")
            changed = database.execute(
                "UPDATE workspace_checkout_repairs SET state = 'bound', "
                "updated_at = CURRENT_TIMESTAMP WHERE operation_id = ? AND owner_token = ? "
                "AND state = 'published'",
                (self.operation_id, self.owner_token),
            )
            if changed.rowcount != 1:
                existing = database.execute(
                    "SELECT state FROM workspace_checkout_repairs WHERE operation_id = ? "
                    "AND owner_token = ?",
                    (self.operation_id, self.owner_token),
                ).fetchone()
                if existing is None or existing[0] != "bound":
                    raise WorkspacePublicationError("Checkout repair binding is unavailable")
            database.commit()

        await run_database_operation(bind)


def _logical_selection_hash(state: WorkspaceCheckoutState) -> str:
    """Hash path-independent logical checkout selection for relocation recovery."""
    value = [state.workspace_id, state.repo_id, list(state.workspaces)]
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _authorize_checkout(
    database: sqlite3.Connection,
    authorize: CheckoutAuthorizer,
    expected: WorkspaceCheckoutState,
) -> WorkspaceCheckoutState:
    """Recheck current logical selection before one durable or external transition."""
    current = authorize(database)
    if _logical_selection_hash(current) != _logical_selection_hash(expected):
        raise WorkspacePublicationError("Checkout repair authority changed")
    if current.repo_path != expected.repo_path:
        raise WorkspacePublicationError("Checkout repair repository changed")
    return current


def _directory_identity_from_stat(info: os.stat_result, path: Path) -> str:
    """Encode one already-opened owned directory identity."""
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspacePublicationError("Checkout publication object is not a directory")
    if info.st_uid != os.geteuid():
        raise WorkspacePublicationError("Checkout publication object has a different owner")
    return json.dumps(
        {
            "device": info.st_dev,
            "inode": info.st_ino,
            "path": str(path),
            "user": info.st_uid,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _directory_identity_json(path: Path, *, projected_path: Path | None = None) -> str:
    """Capture one owned no-follow directory identity with an explicit locator."""
    descriptor = _open_owned_directory(path)
    try:
        return _directory_identity_from_stat(
            os.fstat(descriptor),
            projected_path if projected_path is not None else path,
        )
    finally:
        os.close(descriptor)


def _descriptor_matches_identity(descriptor: int, expected_json: str) -> bool:
    """Return whether one retained directory has its recorded device and inode."""
    try:
        expected = json.loads(expected_json)
        observed = os.fstat(descriptor)
    except (OSError, TypeError, ValueError):
        return False
    return bool(
        isinstance(expected, dict)
        and stat.S_ISDIR(observed.st_mode)
        and observed.st_uid == os.geteuid()
        and observed.st_dev == expected.get("device")
        and observed.st_ino == expected.get("inode")
    )


def _identity_matches(path: Path, expected_json: str) -> bool:
    """Return whether one named directory retains its recorded device and inode."""
    try:
        expected = json.loads(expected_json)
        observed = os.lstat(path)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return False
    return bool(
        isinstance(expected, dict)
        and stat.S_ISDIR(observed.st_mode)
        and not stat.S_ISLNK(observed.st_mode)
        and observed.st_uid == os.geteuid()
        and observed.st_dev == expected.get("device")
        and observed.st_ino == expected.get("inode")
    )


def _project_workspace_identity(
    identity_json: str,
    *,
    staging_repo: Path,
    staging_workspace: Path,
    final_repo: Path,
    final_workspace: Path,
) -> str:
    """Project stable staged inodes onto their post-rename absolute locators."""
    identity = json.loads(identity_json)
    if not isinstance(identity, dict):
        raise WorkspacePublicationError("Staged workspace identity is invalid")
    if identity.get("common") != str(staging_repo / ".git"):
        raise WorkspacePublicationError("Staged workspace common directory changed")
    if identity.get("workspace") != str(staging_workspace):
        raise WorkspacePublicationError("Staged workspace path changed")
    identity["common"] = str(final_repo / ".git")
    identity["workspace"] = str(final_workspace)
    return json.dumps(identity, sort_keys=True, separators=(",", ":"))


def _sync_private_directory(descriptor: int) -> None:
    """Synchronize one retained directory tree through descriptor-relative opens."""
    with os.scandir(descriptor) as entries:
        for entry in entries:
            entry_info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(entry_info.st_mode):
                child = os.open(
                    entry.name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (
                        entry_info.st_dev,
                        entry_info.st_ino,
                    ):
                        raise WorkspacePublicationError("Private checkout changed during sync")
                    os.fsync(child)
                finally:
                    os.close(child)
            elif stat.S_ISDIR(entry_info.st_mode) and not stat.S_ISLNK(entry_info.st_mode):
                child = os.open(
                    entry.name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (
                        entry_info.st_dev,
                        entry_info.st_ino,
                    ):
                        raise WorkspacePublicationError("Private checkout changed during sync")
                    _sync_private_directory(child)
                finally:
                    os.close(child)
    os.fsync(descriptor)


def _sync_private_tree(root: Path, *, expected_identity: str | None = None) -> None:
    """Synchronize one exact private tree without following links."""
    try:
        root_descriptor = _open_owned_directory(root, expected_identity=expected_identity)
    except WorkspacePublicationError as exc:
        if expected_identity is None:
            raise
        raise WorkspacePublicationError("Private checkout changed before sync") from exc
    try:
        _sync_private_directory(root_descriptor)
        if not _identity_matches(
            root,
            expected_identity or _directory_identity_from_stat(os.fstat(root_descriptor), root),
        ):
            raise WorkspacePublicationError("Private checkout changed during sync")
    finally:
        os.close(root_descriptor)


def _chmod_owned_directory(
    path: Path,
    mode: int,
    *,
    expected_identity: str,
) -> None:
    """Change mode only on one retained owned directory identity."""
    try:
        descriptor = _open_owned_directory(path, expected_identity=expected_identity)
    except WorkspacePublicationError as exc:
        raise WorkspacePublicationError("Private checkout changed before chmod") from exc
    try:
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def _require_git_worktree_synchronization(
    repo_path: Path,
    expected_paths: tuple[Path, ...],
    *,
    repo_identity: str,
) -> None:
    """Require Git command-level visibility from one exact published repository."""
    try:
        with _retain_directory(str(repo_path)) as repository:
            if not _descriptor_matches_identity(repository.descriptor, repo_identity):
                raise WorkspacePublicationError("Published repository changed before Git sync")
            output = await _run_git(["worktree", "list", "--porcelain"], cwd=repository)
    except GitError as exc:
        raise WorkspacePublicationError("Published repository changed before Git sync") from exc
    observed = {
        os.path.abspath(line.removeprefix("worktree "))
        for line in output.splitlines()
        if line.startswith("worktree ")
    }
    if any(os.path.abspath(path) not in observed for path in expected_paths):
        raise WorkspacePublicationError("Git worktree registration is not synchronized")


def _sync_worktree_registration(backlink_json: str) -> None:
    """Synchronize linked-worktree registration files and both owning directories."""
    payload = json.loads(backlink_json)
    registration = Path(payload["registration_path_staging"])
    registration_identity = payload.get("registration_identity")
    if not isinstance(registration_identity, str):
        raise WorkspacePublicationError("Worktree registration identity is unavailable")
    _sync_private_tree(registration, expected_identity=registration_identity)
    parent_facts = (
        (registration.parent, payload.get("registration_parent_identity")),
        (registration.parent.parent, payload.get("metadata_identity")),
    )
    for directory, expected_identity in parent_facts:
        if not isinstance(expected_identity, str):
            raise WorkspacePublicationError("Worktree registration parent identity is unavailable")
        descriptor = _open_owned_directory(directory, expected_identity=expected_identity)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _sync_publication_parents(
    staging_path: Path,
    final_path: Path,
    *,
    staging_parent_identity: str,
    final_parent_identity: str,
) -> None:
    """Synchronize exact entry-owning parents in destination-first order."""
    synchronized: set[tuple[int, int]] = set()
    parent_facts = (
        (final_path.parent, final_parent_identity),
        (staging_path.parent, staging_parent_identity),
    )
    for parent_path, expected_identity in parent_facts:
        try:
            descriptor = _open_owned_directory(
                parent_path,
                expected_identity=expected_identity,
            )
        except WorkspacePublicationError as exc:
            raise WorkspacePublicationError("Checkout publication parent changed") from exc
        try:
            identity = os.fstat(descriptor)
            key = (identity.st_dev, identity.st_ino)
            if key not in synchronized:
                os.fsync(descriptor)
                synchronized.add(key)
            if not _identity_matches(parent_path, expected_identity):
                raise WorkspacePublicationError("Checkout publication parent changed")
        finally:
            os.close(descriptor)


def _ensure_publication_parent(tenant: TenantContext, final_repo: Path) -> str:
    """Create only the tenant repository parent through an owned descriptor."""
    tenant_root = Path(os.path.abspath(tenant.data_dir))
    expected_parent = tenant_root / "repos"
    if final_repo.parent != expected_parent:
        raise WorkspacePublicationError(
            "Checkout repair target is outside tenant repository storage"
        )
    tenant_descriptor = _open_owned_directory(tenant_root)
    try:
        try:
            os.mkdir("repos", mode=0o700, dir_fd=tenant_descriptor)
        except FileExistsError:
            pass
        parent_descriptor = os.open(
            "repos",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=tenant_descriptor,
        )
        try:
            parent_identity = os.fstat(parent_descriptor)
            if not stat.S_ISDIR(parent_identity.st_mode) or parent_identity.st_uid != os.geteuid():
                raise WorkspacePublicationError("Checkout publication parent is untrusted")
            os.fchmod(parent_descriptor, 0o700)
            os.fsync(parent_descriptor)
            os.fsync(tenant_descriptor)
            return _directory_identity_from_stat(parent_identity, expected_parent)
        finally:
            os.close(parent_descriptor)
    finally:
        os.close(tenant_descriptor)


def _validate_repo_child(final_repo: Path, repo_id: str) -> None:
    """Require a direct, non-special repository name below the managed parent."""
    if final_repo.name != repo_id or repo_id in {"", ".", ".."}:
        raise WorkspacePublicationError("Checkout repair repository ID is not path-safe")
    if "/" in repo_id or "\x00" in repo_id:
        raise WorkspacePublicationError("Checkout repair repository ID is not path-safe")


def _begin_repair_locked(
    database: sqlite3.Connection,
    *,
    authorize: CheckoutAuthorizer,
    expected: WorkspaceCheckoutState,
    tenant: TenantContext,
    authority_hash: str,
    database_identity: str,
    execution_operation_id: str | None,
    operation_id: str,
    owner_token: str,
    final_repo_path: str,
    requires_publication: bool,
) -> tuple[str, str, str] | None:
    """Create or recover one exact logical repair owner before filesystem work."""
    current = _authorize_checkout(database, authorize, expected)
    deletion_owner = database.execute(
        "SELECT 1 FROM workspace_deletions WHERE repo_id = ? "
        "AND state IN ('claiming', 'claimed', 'draining') LIMIT 1",
        (expected.repo_id,),
    ).fetchone()
    deleting_workspace = database.execute(
        "SELECT 1 FROM workspaces WHERE repo_id = ? AND state = 'deleting' LIMIT 1",
        (expected.repo_id,),
    ).fetchone()
    if deletion_owner is not None or deleting_workspace is not None:
        raise WorkspacePublicationError("Checkout repair conflicts with workspace deletion")
    active_execution = database.execute(
        "SELECT e.operation_id FROM workspace_execution_receipts e "
        "JOIN workspaces w ON w.id = e.workspace_id WHERE w.repo_id = ? "
        "AND e.status IN ('reserved', 'prepared') AND e.operation_id != COALESCE(?, '') "
        "LIMIT 1",
        (expected.repo_id, execution_operation_id),
    ).fetchone()
    if active_execution is not None:
        raise WorkspacePublicationError("Checkout repair conflicts with active execution")
    active_integration = database.execute(
        "SELECT 1 FROM thread_integrations i JOIN workspaces w "
        "ON w.id = i.parent_workspace_id WHERE w.repo_id = ? "
        "AND i.status IN ('checking', 'applying', 'blocked') LIMIT 1",
        (expected.repo_id,),
    ).fetchone()
    if active_integration is not None:
        raise WorkspacePublicationError("Checkout repair conflicts with active integration")
    selection_hash = _logical_selection_hash(current)
    row = database.execute(
        "SELECT operation_id, owner_token, state, authority_hash, selection_hash, "
        "database_identity, tenant_id, source_repo_path, final_repo_path, "
        "execution_operation_id FROM workspace_checkout_repairs WHERE repo_id = ? "
        "AND state IN ('active', 'published', 'unresolved')",
        (expected.repo_id,),
    ).fetchone()
    if row is not None:
        exact = (
            row[2] != "unresolved"
            and row[3] == authority_hash
            and row[4] == selection_hash
            and row[5] == database_identity
            and row[6] == tenant.user_id
            and expected.repo_path in {row[7], row[8]}
            and row[8] == final_repo_path
            and row[9] == execution_operation_id
            and (execution_operation_id is None or row[0] == execution_operation_id)
        )
        if not exact:
            raise WorkspacePublicationError("Checkout repair has unresolved durable ownership")
        return str(row[0]), str(row[1]), str(row[2])
    if current.repo_path == final_repo_path and not requires_publication:
        return None
    database.execute(
        "INSERT INTO workspace_checkout_repairs "
        "(operation_id, workspace_id, repo_id, authority_hash, selection_hash, owner_token, "
        "execution_operation_id, database_identity, tenant_id, state, source_repo_path, "
        "final_repo_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (
            operation_id,
            expected.workspace_id,
            expected.repo_id,
            authority_hash,
            selection_hash,
            owner_token,
            execution_operation_id,
            database_identity,
            tenant.user_id,
            expected.repo_path,
            final_repo_path,
        ),
    )
    return operation_id, owner_token, "active"


def _begin_repair(
    database: sqlite3.Connection,
    *,
    authorize: CheckoutAuthorizer,
    expected: WorkspaceCheckoutState,
    tenant: TenantContext,
    authority_hash: str,
    database_identity: str,
    execution_operation_id: str | None,
    operation_id: str,
    owner_token: str,
    final_repo_path: str,
    requires_publication: bool,
) -> tuple[str, str, str] | None:
    """Serialize repair admission against prompt and integration reservations."""
    database.execute("BEGIN IMMEDIATE")
    try:
        result = _begin_repair_locked(
            database,
            authorize=authorize,
            expected=expected,
            tenant=tenant,
            authority_hash=authority_hash,
            database_identity=database_identity,
            execution_operation_id=execution_operation_id,
            operation_id=operation_id,
            owner_token=owner_token,
            final_repo_path=final_repo_path,
            requires_publication=requires_publication,
        )
        database.commit()
        return result
    except BaseException:
        database.rollback()
        raise


def _publication_row(database: sqlite3.Connection, operation_id: str) -> tuple[object, ...] | None:
    """Load one repository publication receipt in stable column order."""
    row = database.execute(
        "SELECT state, staging_path, final_path, owner_token, staging_identity_json, "
        "final_identity_json, marker_directory, marker_key, marker_authority_hash, "
        "marker_operation_id, marker_physical_identity_json, backlink_json, "
        "backlinks_validated, rename_started, rename_completed, directory_synced, "
        "cleanup_state, staging_parent_identity_json, final_parent_identity_json, object_kind "
        "FROM workspace_checkout_publications WHERE operation_id = ? "
        "AND generation = 0",
        (operation_id,),
    ).fetchone()
    return tuple(row) if row is not None else None


def _set_publication_unresolved(
    database: sqlite3.Connection,
    operation_id: str,
    owner_token: str,
) -> None:
    """Fence both durable records after ambiguous or conflicting observation."""
    database.execute("BEGIN IMMEDIATE")
    try:
        database.execute(
            "UPDATE workspace_checkout_publications SET state = 'unresolved', "
            "cleanup_state = 'retained', updated_at = CURRENT_TIMESTAMP "
            "WHERE operation_id = ? AND owner_token = ?",
            (operation_id, owner_token),
        )
        database.execute(
            "UPDATE workspace_checkout_repairs SET state = 'unresolved', "
            "updated_at = CURRENT_TIMESTAMP WHERE operation_id = ? AND owner_token = ?",
            (operation_id, owner_token),
        )
        database.commit()
    except BaseException:
        database.rollback()
        raise


async def _authorize_external(
    run_database_operation: DatabaseOperationRunner,
    authorize: CheckoutAuthorizer,
    expected: WorkspaceCheckoutState,
) -> WorkspaceCheckoutState:
    """Use a fresh database callback immediately before one external effect."""
    return await run_database_operation(
        lambda database: _authorize_checkout(database, authorize, expected)
    )


@dataclass
class _RetainedPrivateStage:
    """Owned stage plus its retained entry-owning parent authority."""

    directory: _StableDirectory
    parent_descriptor: int
    identity: str
    _closed: bool = False

    def close(self) -> None:
        """Release retained descriptors after every stage effect finishes."""
        if self._closed:
            return
        self._closed = True
        os.close(self.directory.descriptor)
        os.close(self.parent_descriptor)


def _create_private_stage(
    path: Path,
    *,
    parent_identity: str | None = None,
) -> _RetainedPrivateStage:
    """Create and retain one private stage below its exact owned parent."""
    parent_descriptor = _open_owned_directory(
        path.parent,
        expected_identity=parent_identity,
    )
    stage_descriptor: int | None = None
    name = os.fsencode(path.name)
    created = False
    try:
        parent_stat = os.fstat(parent_descriptor)
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        created = True
        stage_descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_descriptor)
        stage_identity = os.fstat(stage_descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(stage_identity.st_mode)
            or stage_identity.st_uid != os.geteuid()
            or stage_identity.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or stage_identity.st_dev != parent_stat.st_dev
            or (named.st_dev, named.st_ino) != (stage_identity.st_dev, stage_identity.st_ino)
        ):
            raise WorkspacePublicationError("Private checkout changed during creation")
        os.fsync(parent_descriptor)
        identity = _directory_identity_from_stat(stage_identity, path)
        retained = _RetainedPrivateStage(
            _StableDirectory(str(path), stage_descriptor),
            parent_descriptor,
            identity,
        )
        stage_descriptor = None
        parent_descriptor = -1
        return retained
    except BaseException:
        if stage_descriptor is not None:
            os.close(stage_descriptor)
            stage_descriptor = None
        if created:
            try:
                os.rmdir(name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            except OSError as cleanup_error:
                raise WorkspacePublicationError(
                    "Private checkout creation cleanup failed"
                ) from cleanup_error
        raise
    finally:
        if stage_descriptor is not None:
            os.close(stage_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _reopen_private_stage(
    path: Path,
    *,
    parent_identity: str,
    stage_identity: str,
) -> _RetainedPrivateStage:
    """Reopen persisted parent and stage identities for restart recovery."""
    parent_descriptor = _open_owned_directory(
        path.parent,
        expected_identity=parent_identity,
    )
    stage_descriptor: int | None = None
    try:
        parent_stat = os.fstat(parent_descriptor)
        stage_descriptor = os.open(
            os.fsencode(path.name),
            _directory_open_flags(),
            dir_fd=parent_descriptor,
        )
        stage_stat = os.fstat(stage_descriptor)
        if (
            stage_stat.st_dev != parent_stat.st_dev
            or not _descriptor_matches_identity(stage_descriptor, stage_identity)
            or stage_stat.st_uid != os.geteuid()
            or stage_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise WorkspacePublicationError("Private checkout identity is unresolved")
        retained = _RetainedPrivateStage(
            _StableDirectory(str(path), stage_descriptor),
            parent_descriptor,
            stage_identity,
        )
        stage_descriptor = None
        parent_descriptor = -1
        return retained
    except OSError as exc:
        raise WorkspacePublicationError("Private checkout identity is unresolved") from exc
    finally:
        if stage_descriptor is not None:
            os.close(stage_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _remove_cancelled_private_stage(retained: _RetainedPrivateStage | None) -> None:
    """Remove an empty retained stage before cancellation loses its identity."""
    if retained is None:
        raise WorkspacePublicationError("Private checkout identity is unresolved")
    try:
        stage_stat = os.fstat(retained.directory.descriptor)
        name = os.fsencode(Path(str(retained.directory)).name)
        named = os.stat(name, dir_fd=retained.parent_descriptor, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (stage_stat.st_dev, stage_stat.st_ino):
            raise WorkspacePublicationError("Private checkout changed during cancellation")
        if os.listdir(retained.directory.descriptor):
            raise WorkspacePublicationError("Private checkout changed during cancellation")
        os.rmdir(name, dir_fd=retained.parent_descriptor)
        os.fsync(retained.parent_descriptor)
    finally:
        retained.close()


async def _await_local_effect(
    operation: Callable[[], _T],
    *,
    cancel_result: Callable[[_T], None] | None = None,
) -> _T:
    """Drain one local mutation before propagating request cancellation."""
    attempt = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(attempt)
    except asyncio.CancelledError:
        while not attempt.done():
            try:
                await asyncio.shield(attempt)
            except asyncio.CancelledError:
                continue
            except BaseException:  # noqa: BLE001 - drain waits out the local effect
                break
        if not attempt.cancelled():
            try:
                result = attempt.result()
            except BaseException:  # noqa: BLE001, S110 - drained failure is discarded
                pass
            else:
                if cancel_result is not None:
                    cancel_result(result)
        raise


async def _persist_created_private_stage(
    retained: _RetainedPrivateStage,
    operation: Callable[[], Awaitable[_T]],
) -> _T:
    """Persist stage identity before cancellation can release retained authority."""
    attempt: asyncio.Future[_T] = asyncio.ensure_future(operation())
    cancelled = False
    result: _T
    try:
        while True:
            try:
                result = await asyncio.shield(attempt)
                break
            except asyncio.CancelledError:
                cancelled = True
                if attempt.done():
                    result = attempt.result()
                    break
    except BaseException:
        _remove_cancelled_private_stage(retained)
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _restore_exact_worktree(
    repo_path: Path | _StableDirectory,
    worktree_path: Path | _StableDirectory,
    branch: str,
    *,
    repo_identity: str,
    worktree_identity: str | None,
) -> None:
    """Restore one worktree while retaining both accepted directory objects."""
    try:
        repository_argument = (
            repo_path if isinstance(repo_path, _StableDirectory) else str(repo_path)
        )
        worktree_argument = (
            worktree_path if isinstance(worktree_path, _StableDirectory) else str(worktree_path)
        )
        with _retain_directory_argument(repository_argument) as repository:
            if not _descriptor_matches_identity(repository.descriptor, repo_identity):
                raise WorkspacePublicationError("Worktree repository changed before restoration")
            if worktree_identity is None:
                await restore_worktree(repository, worktree_argument, branch)
            else:
                with _retain_directory_argument(worktree_argument) as worktree:
                    if not _descriptor_matches_identity(worktree.descriptor, worktree_identity):
                        raise WorkspacePublicationError(
                            "Private worktree changed before restoration"
                        )
                    await restore_worktree(repository, worktree, branch)
    except GitError as exc:
        raise WorkspacePublicationError("Private worktree changed before restoration") from exc


async def _materialize_owned_repo_checkout(
    source_path: str,
    target_path: str | _StableDirectory,
    remote_url: str | None,
    *,
    staging_identity: str,
    access_token: str | None,
) -> None:
    """Materialize only inside the exact private stage recorded before Git work."""
    target = Path(target_path)
    try:
        target_argument = (
            target_path if isinstance(target_path, _StableDirectory) else str(target_path)
        )
        with _retain_directory_argument(target_argument) as target_directory:
            if not _descriptor_matches_identity(target_directory.descriptor, staging_identity):
                raise WorkspacePublicationError("Private repo stage changed before materialization")
            with os.scandir(target_directory.descriptor) as entries:
                target_has_entries = next(entries, None) is not None
            target_is_repository = await validate_local_repo(target_directory)
            if not target_is_repository and target_has_entries:
                raise WorkspacePublicationError("Private repo stage is incomplete")

            if target_is_repository:
                if remote_url:
                    await ensure_remote_url(target_directory, remote_url)
            else:
                with ExitStack() as stack:
                    try:
                        source_directory = stack.enter_context(_retain_directory(source_path))
                    except GitError:
                        source_directory = None
                    if source_directory is not None and await validate_local_repo(source_directory):
                        await _run_git(
                            ["clone", "--no-hardlinks", source_directory, "."],
                            cwd=target_directory,
                        )
                        if remote_url:
                            await ensure_remote_url(target_directory, remote_url)
                    else:
                        if not remote_url:
                            raise WorkspacePublicationError(
                                "Repository clone source is unavailable"
                            )
                        _validate_clone_url(remote_url)
                        with _git_askpass_env(access_token) as environment:
                            await _run_git(
                                ["clone", remote_url, "."],
                                cwd=target_directory,
                                env=environment,
                            )
            if not _identity_matches(target, staging_identity):
                raise WorkspacePublicationError("Private repo stage changed during materialization")
    except GitError as exc:
        raise WorkspacePublicationError(
            "Private repo stage changed before materialization"
        ) from exc


async def _acquire_or_resume_marker(
    *,
    row: tuple[object, ...] | None,
    staging_repo: Path,
    staging_workspace: Path,
    authority_hash: str,
    operation_id: str,
    marker_kind: str,
    staged_identity: str,
) -> WorkspaceAdmissionLease:
    """Acquire the selected stage marker or resume its exact durable owner."""
    if row is not None and row[6] is not None and row[7] is not None:
        return await resume_workspace_admission_marker(
            common_directory=str(row[6]),
            marker_key=str(row[7]),
            authority_hash=authority_hash,
            operation_id=operation_id,
            kind=marker_kind,
        )
    return await acquire_workspace_admission(
        repo_path=str(staging_repo),
        workspace_path=str(staging_workspace),
        authority_hash=authority_hash,
        operation_id=operation_id,
        kind=marker_kind,
        resume=True,
        physical_identity=staged_identity,
    )


def _persist_planned_publication(
    database: sqlite3.Connection,
    *,
    authorize: CheckoutAuthorizer,
    expected: WorkspaceCheckoutState,
    operation_id: str,
    owner_token: str,
    staging_path: str,
    final_path: str,
    parent_identity: str,
    object_kind: str = "repository",
    workspace_id: str | None = None,
) -> None:
    """Write no-replace intent before creating the private checkout."""
    _authorize_checkout(database, authorize, expected)
    database.execute(
        "INSERT OR IGNORE INTO workspace_checkout_publications "
        "(operation_id, generation, object_kind, workspace_id, state, staging_path, "
        "final_path, owner_token, staging_parent_identity_json, final_parent_identity_json) "
        "VALUES (?, 0, ?, ?, 'planned', ?, ?, ?, ?, ?)",
        (
            operation_id,
            object_kind,
            workspace_id,
            staging_path,
            final_path,
            owner_token,
            parent_identity,
            parent_identity,
        ),
    )
    row = database.execute(
        "SELECT staging_path, final_path, owner_token FROM workspace_checkout_publications "
        "WHERE operation_id = ? AND generation = 0",
        (operation_id,),
    ).fetchone()
    if row is None or tuple(row) != (staging_path, final_path, owner_token):
        raise WorkspacePublicationError("Checkout publication receipt does not match its owner")
    database.commit()


def _persist_materializing(
    database: sqlite3.Connection,
    *,
    authorize: CheckoutAuthorizer,
    expected: WorkspaceCheckoutState,
    operation_id: str,
    owner_token: str,
    staging_identity: str,
) -> None:
    """Record private materialization before clone starts."""
    _authorize_checkout(database, authorize, expected)
    changed = database.execute(
        "UPDATE workspace_checkout_publications SET state = 'materializing', "
        "staging_identity_json = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE operation_id = ? AND owner_token = ? AND state = 'planned'",
        (staging_identity, operation_id, owner_token),
    )
    if changed.rowcount != 1:
        raise WorkspacePublicationError("Checkout materialization transition failed")
    database.commit()


def _persist_staged(
    database: sqlite3.Connection,
    *,
    authorize: CheckoutAuthorizer,
    expected: WorkspaceCheckoutState,
    operation_id: str,
    owner_token: str,
    staging_identity: str,
    remote_url: str | None,
    installation_id: int | None,
    workspace_paths: tuple[tuple[str, str], ...],
) -> None:
    """Record exact private identity before marker publication."""
    _authorize_checkout(database, authorize, expected)
    changed = database.execute(
        "UPDATE workspace_checkout_publications SET state = 'staged', "
        "staging_identity_json = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE operation_id = ? AND owner_token = ? AND state IN ('materializing', 'staged')",
        (staging_identity, operation_id, owner_token),
    )
    if changed.rowcount != 1:
        raise WorkspacePublicationError("Staged checkout receipt is unavailable")
    database.execute(
        "UPDATE workspace_checkout_repairs SET remote_url = ?, installation_id = ?, "
        "workspace_paths_json = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE operation_id = ? AND owner_token = ? AND state = 'active'",
        (
            remote_url,
            installation_id,
            json.dumps(workspace_paths, separators=(",", ":")),
            operation_id,
            owner_token,
        ),
    )
    database.commit()


def _persist_marker(
    database: sqlite3.Connection,
    *,
    authorize: CheckoutAuthorizer,
    expected: WorkspaceCheckoutState,
    operation_id: str,
    owner_token: str,
    lease: WorkspaceAdmissionLease,
    selected_identity: str,
) -> None:
    """Record exact marker ownership before backlink mutation."""
    _authorize_checkout(database, authorize, expected)
    changed = database.execute(
        "UPDATE workspace_checkout_publications SET marker_directory = ?, marker_key = ?, "
        "marker_authority_hash = ?, marker_operation_id = ?, "
        "marker_physical_identity_json = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE operation_id = ? AND owner_token = ? AND state IN ('staged', 'publishing')",
        (
            str(lease.common),
            lease.key,
            str(lease.owner["authority"]),
            str(lease.owner["operation"]),
            selected_identity,
            operation_id,
            owner_token,
        ),
    )
    if changed.rowcount != 1:
        raise WorkspacePublicationError("Checkout marker receipt is unavailable")
    database.commit()


def _persist_publishing(
    database: sqlite3.Connection,
    *,
    authorize: CheckoutAuthorizer,
    expected: WorkspaceCheckoutState,
    operation_id: str,
    owner_token: str,
    final_identity: str,
    backlink_json: str,
) -> None:
    """Commit final identity and rename intent before public visibility."""
    _authorize_checkout(database, authorize, expected)
    changed = database.execute(
        "UPDATE workspace_checkout_publications SET state = 'publishing', "
        "final_identity_json = ?, backlink_json = ?, backlinks_validated = 1, "
        "rename_started = 1, updated_at = CURRENT_TIMESTAMP "
        "WHERE operation_id = ? AND owner_token = ? AND state IN ('staged', 'publishing')",
        (final_identity, backlink_json, operation_id, owner_token),
    )
    if changed.rowcount != 1:
        raise WorkspacePublicationError("Checkout rename intent is unavailable")
    database.commit()


def _persist_rename_completed(
    database: sqlite3.Connection,
    *,
    authorize: CheckoutAuthorizer,
    expected: WorkspaceCheckoutState,
    operation_id: str,
    owner_token: str,
) -> None:
    """Record exact final-object observation separately from synchronization."""
    _authorize_checkout(database, authorize, expected)
    changed = database.execute(
        "UPDATE workspace_checkout_publications SET rename_completed = 1, "
        "updated_at = CURRENT_TIMESTAMP WHERE operation_id = ? AND owner_token = ? "
        "AND state = 'publishing' AND rename_started = 1 AND final_identity_json IS NOT NULL",
        (operation_id, owner_token),
    )
    if changed.rowcount != 1:
        existing = database.execute(
            "SELECT state, rename_completed FROM workspace_checkout_publications "
            "WHERE operation_id = ? AND owner_token = ?",
            (operation_id, owner_token),
        ).fetchone()
        if existing is None or existing[0] != "published" or existing[1] != 1:
            database.rollback()
            raise WorkspacePublicationError("Checkout rename completion is unavailable")
    database.commit()


def _persist_directory_synced(
    database: sqlite3.Connection,
    *,
    authorize: CheckoutAuthorizer,
    expected: WorkspaceCheckoutState,
    operation_id: str,
    owner_token: str,
) -> None:
    """Record synchronization only after exact post-sync validation succeeds."""
    _authorize_checkout(database, authorize, expected)
    changed = database.execute(
        "UPDATE workspace_checkout_publications SET directory_synced = 1, "
        "updated_at = CURRENT_TIMESTAMP WHERE operation_id = ? AND owner_token = ? "
        "AND state = 'publishing' AND rename_completed = 1",
        (operation_id, owner_token),
    )
    if changed.rowcount != 1:
        existing = database.execute(
            "SELECT state, directory_synced FROM workspace_checkout_publications "
            "WHERE operation_id = ? AND owner_token = ?",
            (operation_id, owner_token),
        ).fetchone()
        if existing is None or existing[0] != "published" or existing[1] != 1:
            database.rollback()
            raise WorkspacePublicationError("Checkout synchronization receipt is unavailable")
    database.commit()


def _persist_published(
    database: sqlite3.Connection,
    *,
    authorize: CheckoutAuthorizer,
    expected: WorkspaceCheckoutState,
    operation_id: str,
    owner_token: str,
) -> None:
    """Acknowledge only a synchronized exact final object and held marker."""
    _authorize_checkout(database, authorize, expected)
    changed = database.execute(
        "UPDATE workspace_checkout_publications SET state = 'published', "
        "cleanup_state = 'cleaned', updated_at = CURRENT_TIMESTAMP "
        "WHERE operation_id = ? AND owner_token = ? AND state IN ('publishing', 'published') "
        "AND rename_completed = 1 AND directory_synced = 1",
        (operation_id, owner_token),
    )
    if changed.rowcount != 1:
        raise WorkspacePublicationError("Published checkout receipt is unavailable")
    database.execute(
        "UPDATE workspace_checkout_repairs SET state = 'published', "
        "updated_at = CURRENT_TIMESTAMP WHERE operation_id = ? AND owner_token = ? "
        "AND state IN ('active', 'published')",
        (operation_id, owner_token),
    )
    database.commit()


def _ensure_worktree_parent(
    final_repo: Path,
    final_workspace: Path,
    *,
    final_repo_identity: str | None = None,
) -> str:
    """Create and validate the managed worktree parent without following links."""
    try:
        relative_parent = final_workspace.parent.relative_to(final_repo)
    except ValueError as exc:
        raise WorkspacePublicationError("Worktree publication leaves its repository") from exc
    if not relative_parent.parts or relative_parent.parts[0] != ".worktrees":
        raise WorkspacePublicationError("Worktree publication parent is invalid")
    descriptor = _open_owned_directory(final_repo, expected_identity=final_repo_identity)
    current = final_repo
    try:
        for component in relative_parent.parts:
            if component in {"", ".", ".."}:
                raise WorkspacePublicationError("Worktree publication parent is invalid")
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
            current /= component
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid():
                raise WorkspacePublicationError("Worktree publication parent is untrusted")
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)
        return _directory_identity_from_stat(os.fstat(descriptor), current)
    finally:
        os.close(descriptor)


async def _publish_missing_selected_worktree(
    tenant: TenantContext,
    state: WorkspaceCheckoutState,
    *,
    run_database_operation: DatabaseOperationRunner,
    authorize: CheckoutAuthorizer,
    authority_hash: str,
    marker_kind: str,
    operation_id: str,
    owner_token: str,
    final_repo: Path,
) -> WorkspaceCheckoutPublication:
    """Publish one missing selected worktree below an existing managed repository."""
    branches = dict(state.workspaces)
    branch = branches.get(state.workspace_id)
    if branch is None:
        raise WorkspacePublicationError("Selected worktree branch is unavailable")
    final_workspace = Path(_workspace_path(str(final_repo), branch))
    final_repo_identity = _directory_identity_json(final_repo)
    await _authorize_external(run_database_operation, authorize, state)
    parent_identity = await _await_local_effect(
        lambda: _ensure_worktree_parent(
            final_repo,
            final_workspace,
            final_repo_identity=final_repo_identity,
        )
    )
    staging_workspace = final_workspace.parent / (
        f".{final_workspace.name}.yinshi-publication-{owner_token[:16]}"
    )
    await run_database_operation(
        lambda database: _persist_planned_publication(
            database,
            authorize=authorize,
            expected=state,
            operation_id=operation_id,
            owner_token=owner_token,
            staging_path=str(staging_workspace),
            final_path=str(final_workspace),
            parent_identity=parent_identity,
            object_kind="worktree",
            workspace_id=state.workspace_id,
        )
    )
    publication = await run_database_operation(
        lambda database: _publication_row(database, operation_id)
    )
    if publication is None:
        raise WorkspacePublicationError("Worktree publication receipt is missing")
    staging_parent_identity = str(publication[17])
    final_parent_identity = str(publication[18])
    stage_exists = os.path.lexists(staging_workspace)
    final_exists = os.path.lexists(final_workspace)
    if stage_exists and final_exists:
        await run_database_operation(
            lambda database: _set_publication_unresolved(database, operation_id, owner_token)
        )
        raise WorkspacePublicationCollisionError("Both private and final worktree paths exist")

    preparation = WorkspaceCheckoutPreparation(
        workspace_id=state.workspace_id,
        repo_id=state.repo_id,
        repo_path=str(final_repo),
        remote_url=state.remote_url,
        installation_id=state.installation_id,
        workspace_paths=((state.workspace_id, str(final_workspace)),),
        update_repo_metadata=False,
        repaired_repo=False,
    )
    lease: WorkspaceAdmissionLease | None = None
    retained_stage: _RetainedPrivateStage | None = None
    try:
        if final_exists:
            final_identity_value = publication[5]
            selected_identity_value = publication[10]
            marker_key = publication[7]
            if not isinstance(final_identity_value, str) or not _identity_matches(
                final_workspace, final_identity_value
            ):
                raise WorkspacePublicationCollisionError(
                    "Final worktree does not match its publication receipt"
                )
            if not isinstance(selected_identity_value, str) or not isinstance(marker_key, str):
                raise WorkspacePublicationError("Published worktree marker facts are missing")
            observed = await read_workspace_identity(
                repo_path=str(final_repo), workspace_path=str(final_workspace)
            )
            if observed != selected_identity_value:
                raise WorkspacePublicationError("Published worktree identity changed")
            lease = await resume_workspace_admission_marker(
                common_directory=str(final_repo / ".git"),
                marker_key=marker_key,
                authority_hash=authority_hash,
                operation_id=operation_id,
                kind=marker_kind,
            )
            backlink_value = publication[11]
            if not isinstance(backlink_value, str):
                raise WorkspacePublicationError("Published worktree backlink facts are missing")
            await run_database_operation(
                lambda database: _persist_rename_completed(
                    database,
                    authorize=authorize,
                    expected=state,
                    operation_id=operation_id,
                    owner_token=owner_token,
                )
            )
            await _authorize_external(run_database_operation, authorize, state)
            await _await_local_effect(lambda: _sync_worktree_registration(backlink_value))
            await _await_local_effect(
                lambda: _sync_private_tree(
                    final_workspace,
                    expected_identity=final_identity_value,
                )
            )
            await _await_local_effect(
                lambda: _sync_publication_parents(
                    staging_workspace,
                    final_workspace,
                    staging_parent_identity=staging_parent_identity,
                    final_parent_identity=final_parent_identity,
                )
            )
            await _authorize_external(run_database_operation, authorize, state)
            await _require_git_worktree_synchronization(
                final_repo,
                (final_workspace,),
                repo_identity=final_repo_identity,
            )
            if (
                await read_workspace_identity(
                    repo_path=str(final_repo), workspace_path=str(final_workspace)
                )
                != selected_identity_value
            ):
                raise WorkspacePublicationError("Published worktree changed during synchronization")
            await run_database_operation(
                lambda database: _persist_directory_synced(
                    database,
                    authorize=authorize,
                    expected=state,
                    operation_id=operation_id,
                    owner_token=owner_token,
                )
            )
            await run_database_operation(
                lambda database: _persist_published(
                    database,
                    authorize=authorize,
                    expected=state,
                    operation_id=operation_id,
                    owner_token=owner_token,
                )
            )
            return WorkspaceCheckoutPublication(
                preparation,
                lease,
                selected_identity_value,
                operation_id,
                owner_token,
                final_identity_value,
            )

        state_name = str(publication[0])
        staging_identity_value = publication[4]
        if not stage_exists:
            if state_name != "planned":
                raise WorkspacePublicationError("Private worktree disappeared before publication")
            await _authorize_external(run_database_operation, authorize, state)
            retained_stage = await _await_local_effect(
                lambda: _create_private_stage(
                    staging_workspace,
                    parent_identity=staging_parent_identity,
                ),
                cancel_result=_remove_cancelled_private_stage,
            )
            created_identity = retained_stage.identity
            staging_identity_value = created_identity
            await _persist_created_private_stage(
                retained_stage,
                lambda: run_database_operation(
                    lambda database: _persist_materializing(
                        database,
                        authorize=authorize,
                        expected=state,
                        operation_id=operation_id,
                        owner_token=owner_token,
                        staging_identity=created_identity,
                    )
                ),
            )
            state_name = "materializing"
        if not isinstance(staging_identity_value, str) or not _identity_matches(
            staging_workspace, staging_identity_value
        ):
            raise WorkspacePublicationError("Private worktree identity is unresolved")
        if retained_stage is None:
            retained_stage = _reopen_private_stage(
                staging_workspace,
                parent_identity=staging_parent_identity,
                stage_identity=staging_identity_value,
            )
        if state_name == "materializing":
            await _authorize_external(run_database_operation, authorize, state)
            await _restore_exact_worktree(
                final_repo,
                retained_stage.directory,
                branch,
                repo_identity=final_repo_identity,
                worktree_identity=staging_identity_value,
            )
            if not _identity_matches(staging_workspace, staging_identity_value):
                raise WorkspacePublicationError("Private worktree changed during materialization")
            await _authorize_external(run_database_operation, authorize, state)
            await _await_local_effect(
                lambda: _chmod_owned_directory(
                    staging_workspace,
                    0o700,
                    expected_identity=staging_identity_value,
                )
            )
            await run_database_operation(
                lambda database: _persist_staged(
                    database,
                    authorize=authorize,
                    expected=state,
                    operation_id=operation_id,
                    owner_token=owner_token,
                    staging_identity=staging_identity_value,
                    remote_url=state.remote_url,
                    installation_id=state.installation_id,
                    workspace_paths=((state.workspace_id, str(final_workspace)),),
                )
            )
            publication = await run_database_operation(
                lambda database: _publication_row(database, operation_id)
            )
            if publication is None:
                raise WorkspacePublicationError("Staged worktree receipt disappeared")

        projected_identity_value = publication[10]
        if publication[6] is not None and isinstance(projected_identity_value, str):
            staged_payload = json.loads(projected_identity_value)
            staged_payload["workspace"] = str(staging_workspace)
            staged_identity = json.dumps(staged_payload, sort_keys=True, separators=(",", ":"))
        else:
            staged_identity = await read_workspace_identity(
                repo_path=str(final_repo), workspace_path=str(staging_workspace)
            )
        await _authorize_external(run_database_operation, authorize, state)
        lease = await _acquire_or_resume_marker(
            row=publication,
            staging_repo=final_repo,
            staging_workspace=staging_workspace,
            authority_hash=authority_hash,
            operation_id=operation_id,
            marker_kind=marker_kind,
            staged_identity=staged_identity,
        )
        final_selected_identity = _project_workspace_identity(
            staged_identity,
            staging_repo=final_repo,
            staging_workspace=staging_workspace,
            final_repo=final_repo,
            final_workspace=final_workspace,
        )
        await run_database_operation(
            lambda database: _persist_marker(
                database,
                authorize=authorize,
                expected=state,
                operation_id=operation_id,
                owner_token=owner_token,
                lease=lease,
                selected_identity=final_selected_identity,
            )
        )
        await _authorize_external(run_database_operation, authorize, state)
        backlink = rewrite_linked_worktree_backlinks(
            staging_repo_path=final_repo,
            staging_workspace_path=staging_workspace,
            final_repo_path=final_repo,
            final_workspace_path=final_workspace,
            staging_repo_identity=final_repo_identity,
        )
        await _authorize_external(run_database_operation, authorize, state)
        await _await_local_effect(lambda: _sync_worktree_registration(backlink))
        await _authorize_external(run_database_operation, authorize, state)
        await _await_local_effect(
            lambda: _sync_private_tree(
                staging_workspace,
                expected_identity=staging_identity_value,
            )
        )
        final_identity = _directory_identity_json(staging_workspace, projected_path=final_workspace)
        await run_database_operation(
            lambda database: _persist_publishing(
                database,
                authorize=authorize,
                expected=state,
                operation_id=operation_id,
                owner_token=owner_token,
                final_identity=final_identity,
                backlink_json=backlink,
            )
        )
        await _authorize_external(run_database_operation, authorize, state)
        if not _identity_matches(final_workspace.parent, str(publication[18])):
            raise WorkspacePublicationError("Worktree publication parent identity changed")
        await _await_local_effect(
            lambda: atomic_rename_no_replace(
                staging_workspace,
                final_workspace,
                source_parent_identity=staging_parent_identity,
                target_parent_identity=final_parent_identity,
                source_identity=final_identity,
            )
        )
        observed = await read_workspace_identity(
            repo_path=str(final_repo), workspace_path=str(final_workspace)
        )
        if observed != final_selected_identity:
            raise WorkspacePublicationError("Published worktree is not the staged object")
        await run_database_operation(
            lambda database: _persist_rename_completed(
                database,
                authorize=authorize,
                expected=state,
                operation_id=operation_id,
                owner_token=owner_token,
            )
        )
        await _authorize_external(run_database_operation, authorize, state)
        await _await_local_effect(
            lambda: _sync_publication_parents(
                staging_workspace,
                final_workspace,
                staging_parent_identity=staging_parent_identity,
                final_parent_identity=final_parent_identity,
            )
        )
        await _authorize_external(run_database_operation, authorize, state)
        await _require_git_worktree_synchronization(
            final_repo,
            (final_workspace,),
            repo_identity=final_repo_identity,
        )
        if (
            await read_workspace_identity(
                repo_path=str(final_repo), workspace_path=str(final_workspace)
            )
            != final_selected_identity
        ):
            raise WorkspacePublicationError("Published worktree changed during synchronization")
        await run_database_operation(
            lambda database: _persist_directory_synced(
                database,
                authorize=authorize,
                expected=state,
                operation_id=operation_id,
                owner_token=owner_token,
            )
        )
        await run_database_operation(
            lambda database: _persist_published(
                database,
                authorize=authorize,
                expected=state,
                operation_id=operation_id,
                owner_token=owner_token,
            )
        )
        return WorkspaceCheckoutPublication(
            preparation,
            lease,
            observed,
            operation_id,
            owner_token,
            final_identity,
        )
    except BaseException as exc:
        if lease is not None:
            lease.close()
        if isinstance(exc, (WorkspacePublicationCollisionError, WorkspacePublicationError)):
            await run_database_operation(
                lambda database: _set_publication_unresolved(database, operation_id, owner_token)
            )
        raise
    finally:
        if retained_stage is not None:
            retained_stage.close()


async def publish_apply_workspace_checkout_for_tenant(
    tenant: TenantContext,
    state: WorkspaceCheckoutState,
    *,
    run_database_operation: DatabaseOperationRunner,
    authorize: CheckoutAuthorizer,
    authority_hash: str,
    database_identity: str,
    apply: Callable[[sqlite3.Connection, WorkspaceCheckoutPreparation], _T],
    bind_published: Callable[[WorkspaceCheckoutPublication], Awaitable[None]] | None = None,
    execution_operation_id: str | None = None,
    marker_kind: str = "legacy",
) -> tuple[WorkspaceCheckoutPreparation, _T]:
    """Own publication, optional execution handoff, database binding, and completion."""
    prepared = await publish_workspace_checkout_for_tenant(
        tenant,
        state,
        run_database_operation=run_database_operation,
        authorize=authorize,
        authority_hash=authority_hash,
        database_identity=database_identity,
        execution_operation_id=execution_operation_id,
        marker_kind=marker_kind,
    )
    publication: WorkspaceCheckoutPublication | None
    preparation: WorkspaceCheckoutPreparation
    if isinstance(prepared, WorkspaceCheckoutPublication):
        publication = prepared
        preparation = prepared.preparation
    else:
        publication = None
        preparation = prepared
    marker_transferred = False
    try:
        if publication is not None and bind_published is not None:
            await bind_published(publication)
            marker_transferred = True

        def apply_locked(database: sqlite3.Connection) -> _T:
            _authorize_checkout(database, authorize, state)
            return apply(database, preparation)

        result = await run_database_operation(apply_locked)
        if publication is not None:
            await publication.complete(
                run_database_operation=run_database_operation,
                authorize=authorize,
                release_marker=bind_published is None,
            )
        return preparation, result
    finally:
        if publication is not None and (bind_published is None or not marker_transferred):
            publication.selected_lease.close()


async def publish_workspace_checkout_for_tenant(
    tenant: TenantContext,
    state: WorkspaceCheckoutState,
    *,
    run_database_operation: DatabaseOperationRunner,
    authorize: CheckoutAuthorizer,
    authority_hash: str,
    database_identity: str,
    execution_operation_id: str | None = None,
    marker_kind: str = "legacy",
) -> WorkspaceCheckoutPreparation | WorkspaceCheckoutPublication:
    """Prepare one repaired checkout privately and publish it with its marker held."""
    if len(authority_hash) != 64 or any(
        character not in "0123456789abcdef" for character in authority_hash
    ):
        raise WorkspacePublicationError("Checkout repair authority is invalid")
    if not database_identity:
        raise WorkspacePublicationError("Checkout repair database identity is unavailable")
    if marker_kind not in {"prompt", "legacy"}:
        raise WorkspacePublicationError("Checkout repair marker kind is invalid")
    require_atomic_no_replace_support()

    final_repo = Path(os.path.abspath(_tenant_repo_path(tenant, state.repo_id)))
    _validate_repo_child(final_repo, state.repo_id)
    selected_recorded_path = dict(state.workspace_paths).get(state.workspace_id)
    await _authorize_external(run_database_operation, authorize, state)
    require_isolated_execution("trusted_git")
    repo_available = await validate_local_repo(state.repo_path)
    selected_worktree_missing = False
    if selected_recorded_path is not None:
        await _authorize_external(run_database_operation, authorize, state)
        selected_worktree_missing = not await validate_local_repo(selected_recorded_path)
    if selected_worktree_missing and repo_available and state.repo_path == str(final_repo):
        branch = dict(state.workspaces).get(state.workspace_id)
        expected_workspace_path = (
            None if branch is None else _workspace_path(str(final_repo), branch)
        )
        if selected_recorded_path != expected_workspace_path:
            raise WorkspacePublicationError(
                "Missing worktree path does not match its managed final location"
            )
    if (
        _tenant_path_is_trusted(tenant, state.repo_path)
        and state.repo_path != str(final_repo)
        and not selected_worktree_missing
    ):
        await _authorize_external(run_database_operation, authorize, state)
        return await _prepare_trusted_checkout(tenant, state)
    proposed_operation = execution_operation_id or secrets.token_hex(16)
    proposed_owner = secrets.token_hex(32)
    begin = await run_database_operation(
        lambda database: _begin_repair(
            database,
            authorize=authorize,
            expected=state,
            tenant=tenant,
            authority_hash=authority_hash,
            database_identity=database_identity,
            execution_operation_id=execution_operation_id,
            operation_id=proposed_operation,
            owner_token=proposed_owner,
            final_repo_path=str(final_repo),
            requires_publication=selected_worktree_missing,
        )
    )
    if begin is None:
        await _authorize_external(run_database_operation, authorize, state)
        return await _prepare_trusted_checkout(tenant, state)
    operation_id, owner_token, _repair_state = begin
    owned_publication = await run_database_operation(
        lambda database: _publication_row(database, operation_id)
    )

    if owned_publication is not None and owned_publication[19] == "worktree":
        return await _publish_missing_selected_worktree(
            tenant,
            state,
            run_database_operation=run_database_operation,
            authorize=authorize,
            authority_hash=authority_hash,
            marker_kind=marker_kind,
            operation_id=operation_id,
            owner_token=owner_token,
            final_repo=final_repo,
        )
    if selected_worktree_missing and repo_available and state.repo_path == str(final_repo):
        return await _publish_missing_selected_worktree(
            tenant,
            state,
            run_database_operation=run_database_operation,
            authorize=authorize,
            authority_hash=authority_hash,
            marker_kind=marker_kind,
            operation_id=operation_id,
            owner_token=owner_token,
            final_repo=final_repo,
        )

    if repo_available and state.repo_path == str(final_repo):
        publication = await run_database_operation(
            lambda database: _publication_row(database, operation_id)
        )
        if publication is None:
            await _authorize_external(run_database_operation, authorize, state)
            return await _prepare_trusted_checkout(tenant, state)

    await _authorize_external(run_database_operation, authorize, state)
    parent_identity = await _await_local_effect(
        lambda: _ensure_publication_parent(tenant, final_repo)
    )
    staging_repo = final_repo.parent / f".yinshi-publication-{owner_token[:24]}"
    await run_database_operation(
        lambda database: _persist_planned_publication(
            database,
            authorize=authorize,
            expected=state,
            operation_id=operation_id,
            owner_token=owner_token,
            staging_path=str(staging_repo),
            final_path=str(final_repo),
            parent_identity=parent_identity,
        )
    )
    publication = await run_database_operation(
        lambda database: _publication_row(database, operation_id)
    )
    if publication is None:
        raise WorkspacePublicationError("Checkout publication receipt is missing")
    staging_parent_identity = str(publication[17])
    final_parent_identity = str(publication[18])
    if str(publication[1]) != str(staging_repo) or str(publication[2]) != str(final_repo):
        raise WorkspacePublicationError("Checkout publication paths changed")

    stage_exists = os.path.lexists(staging_repo)
    final_exists = os.path.lexists(final_repo)
    if stage_exists and final_exists:
        await run_database_operation(
            lambda database: _set_publication_unresolved(database, operation_id, owner_token)
        )
        raise WorkspacePublicationCollisionError("Both private and final checkout paths exist")

    preparation: WorkspaceCheckoutPreparation
    selected_stage = staging_repo / ".worktrees" / dict(state.workspaces)[state.workspace_id]
    selected_final = final_repo / ".worktrees" / dict(state.workspaces)[state.workspace_id]
    selected_staged_identity: str
    lease: WorkspaceAdmissionLease | None = None
    retained_stage: _RetainedPrivateStage | None = None

    try:
        if final_exists:
            final_identity_value = publication[5]
            if not isinstance(final_identity_value, str) or not _identity_matches(
                final_repo, final_identity_value
            ):
                raise WorkspacePublicationCollisionError(
                    "Final checkout does not match the publication receipt"
                )
            selected_final_identity_value = publication[10]
            if not isinstance(selected_final_identity_value, str):
                raise WorkspacePublicationError("Published marker identity is missing")
            if (
                await read_workspace_identity(
                    repo_path=str(final_repo), workspace_path=str(selected_final)
                )
                != selected_final_identity_value
            ):
                raise WorkspacePublicationError("Published workspace identity changed")
            marker_directory = publication[6]
            marker_key = publication[7]
            if not isinstance(marker_directory, str) or not isinstance(marker_key, str):
                raise WorkspacePublicationError("Published marker ownership is missing")
            final_common = str(final_repo / ".git")
            lease = await resume_workspace_admission_marker(
                common_directory=final_common,
                marker_key=marker_key,
                authority_hash=authority_hash,
                operation_id=operation_id,
                kind=marker_kind,
            )
            workspace_paths = tuple(
                (workspace_id, _workspace_path(str(final_repo), branch))
                for workspace_id, branch in state.workspaces
            )
            preparation = WorkspaceCheckoutPreparation(
                workspace_id=state.workspace_id,
                repo_id=state.repo_id,
                repo_path=str(final_repo),
                remote_url=state.remote_url,
                installation_id=state.installation_id,
                workspace_paths=workspace_paths,
                update_repo_metadata=True,
                repaired_repo=True,
            )
            await run_database_operation(
                lambda database: _persist_rename_completed(
                    database,
                    authorize=authorize,
                    expected=state,
                    operation_id=operation_id,
                    owner_token=owner_token,
                )
            )
            await _authorize_external(run_database_operation, authorize, state)
            await _await_local_effect(
                lambda: _sync_private_tree(
                    final_repo,
                    expected_identity=final_identity_value,
                )
            )
            await _await_local_effect(
                lambda: _sync_publication_parents(
                    staging_repo,
                    final_repo,
                    staging_parent_identity=staging_parent_identity,
                    final_parent_identity=final_parent_identity,
                )
            )
            await _authorize_external(run_database_operation, authorize, state)
            await _require_git_worktree_synchronization(
                final_repo,
                tuple(Path(path) for _workspace_id, path in workspace_paths),
                repo_identity=final_identity_value,
            )
            if not _identity_matches(final_repo, final_identity_value):
                raise WorkspacePublicationError("Published checkout changed during synchronization")
            if (
                await read_workspace_identity(
                    repo_path=str(final_repo), workspace_path=str(selected_final)
                )
                != selected_final_identity_value
            ):
                raise WorkspacePublicationError(
                    "Published workspace changed during synchronization"
                )
            await run_database_operation(
                lambda database: _persist_directory_synced(
                    database,
                    authorize=authorize,
                    expected=state,
                    operation_id=operation_id,
                    owner_token=owner_token,
                )
            )
            await run_database_operation(
                lambda database: _persist_published(
                    database,
                    authorize=authorize,
                    expected=state,
                    operation_id=operation_id,
                    owner_token=owner_token,
                )
            )
            return WorkspaceCheckoutPublication(
                preparation,
                lease,
                selected_final_identity_value,
                operation_id,
                owner_token,
                final_identity_value,
            )

        state_name = str(publication[0])
        staging_identity_value = publication[4]
        if not stage_exists:
            if state_name != "planned":
                raise WorkspacePublicationError("Private checkout disappeared before publication")
            await _authorize_external(run_database_operation, authorize, state)
            retained_stage = await _await_local_effect(
                lambda: _create_private_stage(
                    staging_repo,
                    parent_identity=staging_parent_identity,
                ),
                cancel_result=_remove_cancelled_private_stage,
            )
            created_identity = retained_stage.identity
            staging_identity_value = created_identity
            await _persist_created_private_stage(
                retained_stage,
                lambda: run_database_operation(
                    lambda database: _persist_materializing(
                        database,
                        authorize=authorize,
                        expected=state,
                        operation_id=operation_id,
                        owner_token=owner_token,
                        staging_identity=created_identity,
                    )
                ),
            )
            state_name = "materializing"
        if not isinstance(staging_identity_value, str) or not _identity_matches(
            staging_repo, staging_identity_value
        ):
            raise WorkspacePublicationError("Private checkout identity is unresolved")
        if retained_stage is None:
            retained_stage = _reopen_private_stage(
                staging_repo,
                parent_identity=staging_parent_identity,
                stage_identity=staging_identity_value,
            )
        workspace_paths = tuple(
            (workspace_id, _workspace_path(str(final_repo), branch))
            for workspace_id, branch in state.workspaces
        )
        if state_name == "materializing":
            await _authorize_external(run_database_operation, authorize, state)
            remote_url, access_token, installation_id = await _refresh_repo_remote_metadata(
                tenant,
                state.repo_path,
                state.remote_url,
                state.installation_id,
            )
            await _authorize_external(run_database_operation, authorize, state)
            await _materialize_owned_repo_checkout(
                state.repo_path,
                retained_stage.directory,
                remote_url,
                access_token=access_token,
                staging_identity=staging_identity_value,
            )
            await _authorize_external(run_database_operation, authorize, state)
            await _await_local_effect(
                lambda: _chmod_owned_directory(
                    staging_repo,
                    0o700,
                    expected_identity=staging_identity_value,
                )
            )
            for workspace_id, branch in state.workspaces:
                await _authorize_external(run_database_operation, authorize, state)
                await _restore_exact_worktree(
                    retained_stage.directory,
                    Path(_workspace_path(str(staging_repo), branch)),
                    branch,
                    repo_identity=staging_identity_value,
                    worktree_identity=None,
                )
            await run_database_operation(
                lambda database: _persist_staged(
                    database,
                    authorize=authorize,
                    expected=state,
                    operation_id=operation_id,
                    owner_token=owner_token,
                    staging_identity=staging_identity_value,
                    remote_url=remote_url,
                    installation_id=installation_id,
                    workspace_paths=workspace_paths,
                )
            )
            publication = await run_database_operation(
                lambda database: _publication_row(database, operation_id)
            )
            if publication is None:
                raise WorkspacePublicationError("Staged checkout receipt disappeared")
        else:
            remote_url = state.remote_url
            installation_id = state.installation_id

        projected_selected_identity = publication[10]
        if publication[6] is not None and isinstance(projected_selected_identity, str):
            staged_payload = json.loads(projected_selected_identity)
            staged_payload["common"] = str(staging_repo / ".git")
            staged_payload["workspace"] = str(selected_stage)
            selected_staged_identity = json.dumps(
                staged_payload, sort_keys=True, separators=(",", ":")
            )
        else:
            selected_staged_identity = await read_workspace_identity(
                repo_path=str(staging_repo), workspace_path=str(selected_stage)
            )
        await _authorize_external(run_database_operation, authorize, state)
        lease = await _acquire_or_resume_marker(
            row=publication,
            staging_repo=staging_repo,
            staging_workspace=selected_stage,
            authority_hash=authority_hash,
            operation_id=operation_id,
            marker_kind=marker_kind,
            staged_identity=selected_staged_identity,
        )
        await run_database_operation(
            lambda database: _persist_marker(
                database,
                authorize=authorize,
                expected=state,
                operation_id=operation_id,
                owner_token=owner_token,
                lease=lease,
                selected_identity=_project_workspace_identity(
                    selected_staged_identity,
                    staging_repo=staging_repo,
                    staging_workspace=selected_stage,
                    final_repo=final_repo,
                    final_workspace=selected_final,
                ),
            )
        )

        backlinks: list[dict[str, object]] = []
        for workspace_id, branch in state.workspaces:
            await _authorize_external(run_database_operation, authorize, state)
            staging_workspace = Path(_workspace_path(str(staging_repo), branch))
            final_workspace = Path(_workspace_path(str(final_repo), branch))
            backlink = rewrite_linked_worktree_backlinks(
                staging_repo_path=staging_repo,
                staging_workspace_path=staging_workspace,
                final_repo_path=final_repo,
                final_workspace_path=final_workspace,
                staging_repo_identity=staging_identity_value,
            )
            backlinks.append({"workspace_id": workspace_id, "paths": json.loads(backlink)})
        await _authorize_external(run_database_operation, authorize, state)
        await _await_local_effect(
            lambda: _sync_private_tree(
                staging_repo,
                expected_identity=staging_identity_value,
            )
        )
        final_identity = _directory_identity_json(staging_repo, projected_path=final_repo)
        backlink_json = json.dumps(backlinks, sort_keys=True, separators=(",", ":"))
        await run_database_operation(
            lambda database: _persist_publishing(
                database,
                authorize=authorize,
                expected=state,
                operation_id=operation_id,
                owner_token=owner_token,
                final_identity=final_identity,
                backlink_json=backlink_json,
            )
        )
        await _authorize_external(run_database_operation, authorize, state)
        if not _identity_matches(final_repo.parent, str(publication[18])):
            raise WorkspacePublicationError("Repository publication parent identity changed")
        await _await_local_effect(
            lambda: atomic_rename_no_replace(
                staging_repo,
                final_repo,
                source_parent_identity=staging_parent_identity,
                target_parent_identity=final_parent_identity,
                source_identity=final_identity,
            )
        )
        lease.common = final_repo / ".git"
        selected_final_identity = await read_workspace_identity(
            repo_path=str(final_repo), workspace_path=str(selected_final)
        )
        expected_final_identity = _project_workspace_identity(
            selected_staged_identity,
            staging_repo=staging_repo,
            staging_workspace=selected_stage,
            final_repo=final_repo,
            final_workspace=selected_final,
        )
        if selected_final_identity != expected_final_identity:
            raise WorkspacePublicationError("Published workspace identity is not the staged object")
        await run_database_operation(
            lambda database: _persist_rename_completed(
                database,
                authorize=authorize,
                expected=state,
                operation_id=operation_id,
                owner_token=owner_token,
            )
        )
        await _authorize_external(run_database_operation, authorize, state)
        await _await_local_effect(
            lambda: _sync_publication_parents(
                staging_repo,
                final_repo,
                staging_parent_identity=staging_parent_identity,
                final_parent_identity=final_parent_identity,
            )
        )
        await _authorize_external(run_database_operation, authorize, state)
        await _require_git_worktree_synchronization(
            final_repo,
            tuple(Path(path) for _workspace_id, path in workspace_paths),
            repo_identity=final_identity,
        )
        if not _identity_matches(final_repo, final_identity):
            raise WorkspacePublicationError("Published checkout changed during synchronization")
        if (
            await read_workspace_identity(
                repo_path=str(final_repo), workspace_path=str(selected_final)
            )
            != expected_final_identity
        ):
            raise WorkspacePublicationError("Published workspace changed during synchronization")
        await run_database_operation(
            lambda database: _persist_directory_synced(
                database,
                authorize=authorize,
                expected=state,
                operation_id=operation_id,
                owner_token=owner_token,
            )
        )
        await run_database_operation(
            lambda database: _persist_published(
                database,
                authorize=authorize,
                expected=state,
                operation_id=operation_id,
                owner_token=owner_token,
            )
        )
        preparation = WorkspaceCheckoutPreparation(
            workspace_id=state.workspace_id,
            repo_id=state.repo_id,
            repo_path=str(final_repo),
            remote_url=remote_url,
            installation_id=installation_id,
            workspace_paths=workspace_paths,
            update_repo_metadata=True,
            repaired_repo=True,
        )
        return WorkspaceCheckoutPublication(
            preparation,
            lease,
            selected_final_identity,
            operation_id,
            owner_token,
            final_identity,
        )
    except BaseException as exc:
        if lease is not None:
            lease.close()
        if isinstance(exc, (WorkspacePublicationCollisionError, WorkspacePublicationError)):
            await run_database_operation(
                lambda database: _set_publication_unresolved(database, operation_id, owner_token)
            )
        raise
    finally:
        if retained_stage is not None:
            retained_stage.close()
