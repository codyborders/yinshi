"""Isolated child-thread workspace provisioning and Git result finalization.

Phase 2 of the thread orchestration plan. This service captures an immutable
parent Git base, creates delegated child worktrees from exact commits, and
finalizes child filesystem state into one synthetic result commit. Parent
branches, HEAD, the real Git index, and parent working files are never
modified. Hidden refs under ``refs/yinshi/`` keep snapshots and results
addressable without polluting the branch namespace.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from yinshi.config import get_settings
from yinshi.exceptions import (
    GitError,
    RepoNotFoundError,
    WorkspaceNotFoundError,
    YinshiError,
)
from yinshi.services.git import (
    _run_git,
    create_worktree,
    run_git_bytes as _run_git_bytes,
)
from yinshi.services.repository_lifecycle import (
    repository_lifecycle,
    repository_lifecycle_root,
)
from yinshi.services.workspace_files import (
    ChangedFile,
    ChangeKind,
    ensure_secret_guardrails,
    is_secret_path,
)
from yinshi.tenant import TenantContext

logger = logging.getLogger(__name__)

_BRANCH_NAMESPACE = "yinshi/thread-"
_SHORT_DELEGATION_ID_LENGTH = 8
_WORKTREE_DIRECTORY = ".worktrees"
_SNAPSHOT_REF_PREFIX = "refs/yinshi/snapshots/"
_RESULT_REF_PREFIX = "refs/yinshi/results/"
_COMMIT_IDENTITY_ARGS = (
    "-c",
    "user.name=Yinshi",
    "-c",
    "user.email=noreply@yinshi.local",
)
_ZERO_OID = "0" * 40


class ThreadProtectedPathError(YinshiError):
    """Raised when one snapshot would capture a protected secret path."""


class ThreadSnapshotLimitError(YinshiError):
    """Raised when one snapshot exceeds the configured size bounds."""


class ThreadDirtySubmoduleError(YinshiError):
    """Raised when the parent worktree contains dirty submodules."""


class ThreadBranchCollisionError(YinshiError):
    """Raised when the generated child branch or worktree path exists."""


class ThreadSnapshotRefExistsError(YinshiError):
    """Raised when a snapshot ref already exists for one delegation."""


class ThreadResultBoundsError(YinshiError):
    """Raised when one result violates the configured result bounds."""


class ThreadResultRefConflictError(YinshiError):
    """Raised when publication would replace an existing result ref."""


class ThreadUnsafePathError(YinshiError):
    """Raised when a thread path would traverse a symlinked root."""


class ThreadWorkspaceKindError(YinshiError):
    """Raised when a child-only operation receives another workspace kind."""


_MAX_CHANGED_FILE_ENTRIES = 5000
_DELEGATION_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True, slots=True)
class ProvisionedChildWorkspace:
    """Stable metadata for one provisioned delegated child workspace."""

    workspace_id: str
    repo_id: str
    path: str
    branch: str
    base_kind: str
    base_commit: str
    snapshot_ref: str | None


@dataclass(frozen=True, slots=True)
class _ParentLocation:
    """Database-resolved parent workspace and repository locations."""

    workspace_id: str
    repo_id: str
    repo_path: str
    workspace_path: str


def _require_identifier(value: str, name: str) -> str:
    """Return one non-empty trimmed identifier."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _require_delegation_id(value: str) -> str:
    """Return one delegation ID matching the repository's generated shape."""
    normalized = _require_identifier(value, "delegation_id")
    if _DELEGATION_ID_PATTERN.fullmatch(normalized) is None:
        raise ValueError("delegation_id must be 32 lowercase hexadecimal characters")
    return normalized


def _snapshot_ref(delegation_id: str) -> str:
    """Return the hidden snapshot ref for one delegation."""
    return f"{_SNAPSHOT_REF_PREFIX}{delegation_id}"


def _child_branch_name(delegation_id: str) -> str:
    """Return the server-generated child branch for one delegation."""
    return f"{_BRANCH_NAMESPACE}{delegation_id[:_SHORT_DELEGATION_ID_LENGTH]}"


def _result_ref(delegation_id: str) -> str:
    """Return the hidden result ref for one delegation."""
    return f"{_RESULT_REF_PREFIX}{delegation_id}"


@dataclass(frozen=True, slots=True)
class FinalizedThreadGitResult:
    """Stable backend-verified Git result for one finished child thread."""

    base_commit: str
    result_commit: str
    result_ref: str
    changed_files: tuple[ChangedFile, ...]


def _changed_file_kind(status: str) -> ChangeKind:
    """Map one ``diff --name-status`` letter to a stable change kind."""
    letter = status[:1]
    if letter == "A":
        return "added"
    if letter == "D":
        return "deleted"
    if letter == "R":
        return "renamed"
    if letter == "C":
        return "copied"
    if letter in {"M", "T", "U", "X"}:
        return "modified"
    return "unknown"


def _parse_changed_files(output: bytes) -> tuple[ChangedFile, ...]:
    """Parse null-delimited ``diff --name-status -z -M`` output."""
    records = output.split(b"\0")
    changes: list[ChangedFile] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        status = record.decode("utf-8", errors="surrogateescape")
        original_path = None
        if status[:1] in {"R", "C"} and index < len(records):
            # Empirically, ``git diff --name-status -z`` lists renames and
            # copies as source first, then the destination.
            original_path = records[index].decode("utf-8", errors="surrogateescape")
            index += 1
        if index >= len(records):
            break
        path = records[index].decode("utf-8", errors="surrogateescape")
        index += 1
        if is_secret_path(path):
            continue
        changes.append(
            ChangedFile(
                path=path,
                status=status,
                kind=_changed_file_kind(status),
                original_path=original_path,
            )
        )
    return tuple(changes)


def _load_parent_location(
    db: sqlite3.Connection,
    parent_workspace_id: str,
) -> _ParentLocation:
    """Resolve the parent workspace row and its repository root path."""
    workspace = db.execute(
        "SELECT * FROM workspaces WHERE id = ?",
        (parent_workspace_id,),
    ).fetchone()
    if workspace is None:
        raise WorkspaceNotFoundError(f"Workspace {parent_workspace_id} not found")
    repo_id = str(workspace["repo_id"])
    repo = db.execute(
        "SELECT * FROM repos WHERE id = ?",
        (repo_id,),
    ).fetchone()
    if repo is None:
        raise RepoNotFoundError(f"Repo {repo_id} not found")
    repo_path = str(repo["root_path"])
    return _ParentLocation(
        workspace_id=str(workspace["id"]),
        repo_id=repo_id,
        repo_path=repo_path,
        workspace_path=str(workspace["path"]),
    )


async def _assert_submodules_clean(workspace_path: str) -> None:
    """Reject parents whose submodules carry uncommitted or moved state.

    Every listing is parsed as raw Git bytes. Text stripping and whitespace
    splitting corrupt submodule paths that contain leading, trailing, or
    embedded whitespace, which would silently drop the dirty-state rejection.
    """
    listing = await _run_git_bytes(
        ["submodule", "status", "--recursive"],
        cwd=workspace_path,
    )
    for record in listing.split(b"\n"):
        if not record:
            continue
        # The leading status byte is position-stable: ``-`` uninitialized,
        # ``+`` checked-out mismatch, ``U`` merge conflict.
        if record[:1] in {b"-", b"+", b"U"}:
            raise ThreadDirtySubmoduleError("thread parent has dirty submodules")
    # Uncommitted content inside a checkout keeps a space prefix in
    # ``submodule status``, so scan porcelain with submodule checks forced on.
    # Gitlink index entries carry the exact raw submodule paths that porcelain
    # records use, so match those bytes without any whitespace repair.
    index_listing = await _run_git_bytes(["ls-files", "-z", "-s"], cwd=workspace_path)
    submodule_paths: set[bytes] = set()
    for record in index_listing.split(b"\0"):
        if not record:
            continue
        meta, separator, entry_path = record.partition(b"\t")
        fields = meta.split(b" ")
        if separator and len(fields) == 3 and fields[0] == b"160000":
            submodule_paths.add(entry_path)
    if not submodule_paths:
        return
    status = await _run_git_bytes(
        ["status", "--porcelain=v1", "-z", "--ignore-submodules=none"],
        cwd=workspace_path,
    )
    records = status.split(b"\0")
    index_position = 0
    while index_position < len(records):
        record = records[index_position]
        index_position += 1
        if len(record) < 4:
            continue
        entry_path = record[3:]
        if entry_path in submodule_paths:
            raise ThreadDirtySubmoduleError("thread parent has dirty submodules")
        if b"R" in record[:2] or b"C" in record[:2]:
            # Rename and copy records repeat the original path next.
            index_position += 1


_ALTERNATE_INDEX_PREFIX = "yinshi-thread-alt-index-"


async def _write_tree_through_alternate_index(
    workspace_path: str,
    *,
    base_treeish: str | None,
) -> str:
    """Write one tree from the workspace through a private temporary index.

    The real Git index stays untouched: every index-touching subprocess runs
    with ``GIT_INDEX_FILE`` pointing at one temporary alternate index file.
    ``base_treeish`` seeds the index when given; read-tree accepts any
    tree-ish there. Without it the index starts empty and captures only
    workspace content. The temporary file is always removed.
    """
    index_descriptor, index_path = tempfile.mkstemp(
        prefix=_ALTERNATE_INDEX_PREFIX,
    )
    os.close(index_descriptor)
    # Git refuses to read a zero-byte index file, so let it create the
    # alternate index at this unique path instead.
    os.unlink(index_path)
    try:
        index_env = {"GIT_INDEX_FILE": index_path}
        if base_treeish is not None:
            await _run_git(
                ["read-tree", base_treeish],
                cwd=workspace_path,
                env=index_env,
            )
        await _run_git(
            ["add", "-A", "--", "."],
            cwd=workspace_path,
            env=index_env,
        )
        return await _run_git(
            ["write-tree"],
            cwd=workspace_path,
            env=index_env,
        )
    finally:
        with suppress(OSError):
            os.unlink(index_path)


async def _assert_no_protected_candidates(workspace_path: str) -> None:
    """Reject snapshot inputs that contain protected secret paths."""
    tracked = await _run_git_bytes(["ls-files", "-z"], cwd=workspace_path)
    untracked = await _run_git_bytes(
        ["ls-files", "-z", "-o", "--exclude-standard"],
        cwd=workspace_path,
    )
    for record in (tracked + b"\0" + untracked).split(b"\0"):
        if not record:
            continue
        candidate = record.decode("utf-8", errors="surrogateescape")
        if is_secret_path(candidate):
            raise ThreadProtectedPathError("thread snapshot contains a protected secret path")


async def _worktree_status_output(workspace_path: str) -> bytes:
    """Return the raw null-delimited porcelain status for one workspace."""
    return await _run_git_bytes(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=workspace_path,
    )


async def _ref_exists(repo_path: str, ref: str) -> bool:
    """Return whether one fully qualified ref exists in an accessible repository."""
    output = await _run_git(
        ["for-each-ref", "--format=%(refname)", ref],
        cwd=repo_path,
    )
    return ref in output.splitlines()


class ThreadWorkspaceService:
    """Provision and finalize isolated Git workspaces for child threads."""

    async def provision_child(
        self,
        db: sqlite3.Connection,
        tenant: TenantContext | None,
        *,
        parent_workspace_id: str,
        delegation_id: str,
    ) -> ProvisionedChildWorkspace:
        """Provision one delegated child workspace from an exact parent base."""
        normalized_delegation_id = _require_delegation_id(delegation_id)
        location = _load_parent_location(db, parent_workspace_id)
        # A symlinked root would let child paths escape the repository.
        if os.path.islink(location.repo_path):
            raise ThreadUnsafePathError("repository root must not be a symlink")
        if os.path.islink(location.workspace_path):
            raise ThreadUnsafePathError("parent workspace path must not be a symlink")
        worktree_root = os.path.join(location.repo_path, _WORKTREE_DIRECTORY)
        if os.path.islink(worktree_root):
            raise ThreadUnsafePathError("repository worktree directory must not be a symlink")
        branch = _child_branch_name(normalized_delegation_id)
        lock_root = repository_lifecycle_root(db, tenant)

        base_kind = "head"
        base_commit: str | None = None
        snapshot_ref: str | None = None
        # Cleanup may delete a snapshot ref only after this attempt
        # definitely published it. Cancellation during publication leaves
        # ownership uncertain, and an uncertain ref is preserved.
        snapshot_published = False

        async with repository_lifecycle(location.repo_id, lock_root):
            created_workspace_id: str | None = None
            claimed = False
            worktree_path = os.path.join(
                location.repo_path,
                _WORKTREE_DIRECTORY,
                branch,
            )
            try:
                # Claim the target before any mutation so a collision can
                # never delete a pre-existing branch or worktree. A snapshot
                # ref owned by another attempt is likewise never overwritten.
                await self._assert_child_target_available(location.repo_path, branch)
                if await _ref_exists(
                    location.repo_path,
                    _snapshot_ref(delegation_id),
                ):
                    raise ThreadSnapshotRefExistsError(
                        "snapshot ref already exists for this delegation",
                    )
                claimed = True
                await _assert_submodules_clean(location.workspace_path)
                status_before = await _worktree_status_output(location.workspace_path)
                head = await _resolve_head_commit(location.workspace_path)
                if status_before:
                    base_kind = "snapshot"
                    snapshot_ref = _snapshot_ref(normalized_delegation_id)
                    base_commit = await self._create_snapshot(
                        location,
                        normalized_delegation_id,
                        head,
                    )
                    snapshot_published = True
                else:
                    base_kind = "head"
                    base_commit = head
                await create_worktree(
                    location.repo_path,
                    worktree_path,
                    branch,
                    base_ref=base_commit,
                )
                ensure_secret_guardrails(location.repo_path)
                created_workspace_id = self._insert_workspace_row(
                    db,
                    location,
                    branch,
                    worktree_path,
                )
                if base_commit is None:
                    # Unborn clean parent: create_worktree materialized the
                    # existing empty-root behavior; report its commit.
                    base_commit = await _run_git(
                        ["rev-parse", f"refs/heads/{branch}"],
                        cwd=location.repo_path,
                    )
            except BaseException as original_error:
                # Only artifacts claimed by this attempt may be removed. A
                # failed claim means nothing was mutated and pre-existing
                # branches and worktrees must survive untouched.
                if claimed:
                    failures = await self._cleanup_artifacts_unlocked(
                        db,
                        location.repo_path,
                        normalized_delegation_id,
                        branch,
                        workspace_path=None,
                        workspace_id=created_workspace_id,
                        delete_snapshot_ref=snapshot_published,
                        delete_result_ref=False,
                    )
                    if failures:
                        logger.warning(
                            "Thread workspace cleanup left %d artifact(s) for "
                            "delegation reconciliation",
                            len(failures),
                        )
                raise original_error

        return ProvisionedChildWorkspace(
            workspace_id=created_workspace_id,
            repo_id=location.repo_id,
            path=worktree_path,
            branch=branch,
            base_kind=base_kind,
            base_commit=base_commit or "",
            snapshot_ref=snapshot_ref,
        )

    async def discard_partial_child(
        self,
        db: sqlite3.Connection,
        tenant: TenantContext | None,
        *,
        delegation_id: str,
        workspace_id: str | None = None,
        repo_id: str | None = None,
    ) -> None:
        """Remove every artifact from one attempt; safe to repeat."""
        normalized_delegation_id = _require_delegation_id(delegation_id)
        workspace_row = None
        if workspace_id is not None:
            workspace_row = db.execute(
                "SELECT * FROM workspaces WHERE id = ?",
                (workspace_id,),
            ).fetchone()
        if workspace_row is not None and workspace_row["kind"] != "delegated":
            raise ThreadWorkspaceKindError("child operation requires a delegated workspace")
        resolved_repo_id = repo_id or (
            str(workspace_row["repo_id"]) if workspace_row is not None else None
        )
        if resolved_repo_id is None:
            if workspace_id is not None:
                self._delete_delegated_workspace_row(db, workspace_id)
            return
        repo_row = db.execute(
            "SELECT root_path FROM repos WHERE id = ?",
            (resolved_repo_id,),
        ).fetchone()
        if repo_row is None:
            if workspace_id is not None:
                self._delete_delegated_workspace_row(db, workspace_id)
            return
        repo_path = str(repo_row["root_path"])
        branch = (
            str(workspace_row["branch"])
            if workspace_row is not None
            else _child_branch_name(normalized_delegation_id)
        )
        stored_path = str(workspace_row["path"]) if workspace_row is not None else None
        lock_root = repository_lifecycle_root(db, tenant)
        async with repository_lifecycle(resolved_repo_id, lock_root):
            failures = await self._cleanup_artifacts_unlocked(
                db,
                repo_path,
                normalized_delegation_id,
                branch,
                workspace_path=stored_path,
                workspace_id=workspace_id,
                delete_snapshot_ref=True,
                delete_result_ref=True,
            )
        if failures:
            raise GitError(
                f"thread workspace cleanup failed for delegation " f"{normalized_delegation_id}"
            ) from failures[0]
        logger.info("Discarded partial child thread workspace artifacts")

    async def _cleanup_artifacts_unlocked(
        self,
        db: sqlite3.Connection,
        repo_path: str,
        delegation_id: str,
        branch: str,
        *,
        workspace_path: str | None,
        workspace_id: str | None,
        delete_snapshot_ref: bool,
        delete_result_ref: bool,
    ) -> list[GitError]:
        """Remove one attempt's Git artifacts, then its row.

        Missing artifacts are tolerated. The workspace row is deleted only
        when every Git cleanup step succeeds. Provisioning-failure cleanup
        never touches result refs, and deletes the snapshot ref only when the
        caller confirms this attempt definitely published it. Uncertain or
        competing refs stay for reconciliation. Only explicit discard deletes
        both refs. Callers must already hold the repository lifecycle lock.
        """
        failures: list[GitError] = []
        if repo_path:
            worktree_target = workspace_path or os.path.join(
                repo_path,
                _WORKTREE_DIRECTORY,
                branch,
            )
            with suppress(GitError):
                await _run_git(
                    ["worktree", "remove", "--force", worktree_target],
                    cwd=repo_path,
                )
            with suppress(GitError):
                await _run_git(["worktree", "prune"], cwd=repo_path)
            # A drifted directory makes the Git removal fail above. Removing
            # the leftover turns that into success; a persistent target is
            # the only cleanup failure worth reporting.
            self._remove_leftover_worktree_directory(
                repo_path,
                worktree_target,
                failures,
            )
            ref_deletions: list[list[str]] = []
            if delete_snapshot_ref:
                ref_deletions.append(["update-ref", "-d", _snapshot_ref(delegation_id)])
            if delete_result_ref:
                ref_deletions.append(["update-ref", "-d", _result_ref(delegation_id)])
            for args in (
                ["branch", "-D", "--", branch],
                *ref_deletions,
            ):
                try:
                    await _run_git(args, cwd=repo_path)
                except GitError as exc:
                    # Deletion of an already-absent artifact is success.
                    # Verify absence before recording a real failure.
                    ref = args[2] if args[0] == "update-ref" else f"refs/heads/{branch}"
                    if await _ref_exists(repo_path, ref):
                        failures.append(exc)
        if not failures and workspace_id is not None:
            self._delete_delegated_workspace_row(db, workspace_id)
        return failures

    @staticmethod
    def _remove_leftover_worktree_directory(
        repo_path: str,
        worktree_target: str,
        failures: list[GitError],
    ) -> None:
        """Delete one leftover child directory below the repo worktree root.

        Git cannot remove worktree directories that drifted from their
        registered path. The target must stay inside the repository's own
        worktree root, and any failure is reported, never swallowed.
        """
        if not os.path.lexists(worktree_target):
            return
        allowed_root = os.path.realpath(
            os.path.join(repo_path, _WORKTREE_DIRECTORY),
        )
        resolved_target = os.path.realpath(worktree_target)
        if not resolved_target.startswith(allowed_root + os.sep):
            failures.append(
                GitError("thread worktree path escaped the worktree root"),
            )
            return
        try:
            target = Path(resolved_target)
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
        except OSError as exc:
            failures.append(GitError("thread worktree directory removal failed"))
            failures[-1].__cause__ = exc

    @staticmethod
    def _delete_delegated_workspace_row(
        db: sqlite3.Connection,
        workspace_id: str,
    ) -> None:
        """Delete only delegated workspace rows, never user workspaces."""
        db.execute(
            "DELETE FROM workspaces WHERE id = ? AND kind = 'delegated'",
            (workspace_id,),
        )
        db.commit()

    async def finalize_child(
        self,
        db: sqlite3.Connection,
        tenant: TenantContext | None,
        *,
        delegation_id: str,
        workspace_id: str,
        base_commit: str,
    ) -> FinalizedThreadGitResult:
        """Seal the child's final filesystem into one synthetic result commit."""
        normalized_delegation_id = _require_delegation_id(delegation_id)
        normalized_workspace_id = _require_identifier(workspace_id, "workspace_id")
        normalized_base_commit = _require_identifier(base_commit, "base_commit")
        workspace_row = db.execute(
            "SELECT * FROM workspaces WHERE id = ?",
            (normalized_workspace_id,),
        ).fetchone()
        if workspace_row is None:
            raise WorkspaceNotFoundError(f"Workspace {workspace_id} not found")
        if workspace_row["kind"] != "delegated":
            raise ThreadWorkspaceKindError("child operation requires a delegated workspace")
        repo_id = str(workspace_row["repo_id"])
        workspace_path = str(workspace_row["path"])
        repo_row = db.execute(
            "SELECT root_path FROM repos WHERE id = ?",
            (repo_id,),
        ).fetchone()
        if repo_row is None:
            raise RepoNotFoundError(f"Repo {repo_id} not found")
        repo_path = str(repo_row["root_path"])
        lock_root = repository_lifecycle_root(db, tenant)

        async with repository_lifecycle(repo_id, lock_root):
            return await self._finalize_locked(
                repo_path,
                workspace_path,
                normalized_delegation_id,
                normalized_base_commit,
            )

    async def _finalize_locked(
        self,
        repo_path: str,
        workspace_path: str,
        delegation_id: str,
        base_commit: str,
    ) -> FinalizedThreadGitResult:
        """Build the result commit while holding the repository lock."""
        await _run_git(
            ["cat-file", "-e", f"{base_commit}^{{commit}}"],
            cwd=repo_path,
        )
        await _assert_submodules_clean(workspace_path)
        await _assert_no_protected_candidates(workspace_path)
        tree_id = await _write_tree_through_alternate_index(
            workspace_path,
            base_treeish=base_commit,
        )
        await self._assert_snapshot_within_limits(workspace_path, tree_id)

        result_ref = _result_ref(delegation_id)
        existing_commit = await self._resolve_existing_result_commit(
            repo_path,
            result_ref,
            tree_id,
            base_commit,
        )
        if existing_commit is not None:
            result_commit = existing_commit
        else:
            result_commit = await _run_git(
                [
                    *_COMMIT_IDENTITY_ARGS,
                    "commit-tree",
                    tree_id,
                    "-p",
                    base_commit,
                    "-m",
                    f"Yinshi thread result for delegation {delegation_id}",
                ],
                cwd=repo_path,
            )
        # Changed files and their bound are verified before the ref moves, so
        # a bound failure never publishes a new result ref. A rejected new
        # commit stays unreferenced and unreachable.
        changed_files = await self._changed_files_since_base(
            repo_path,
            base_commit,
            result_commit,
        )
        if existing_commit is None:
            try:
                await _run_git(
                    ["update-ref", result_ref, result_commit, _ZERO_OID],
                    cwd=repo_path,
                )
            except GitError as exc:
                # A lost publication race means another writer now owns the
                # ref; a missing ref means a real Git fault. Both fail closed
                # and leave every published ref untouched.
                if await _ref_exists(repo_path, result_ref):
                    raise ThreadResultRefConflictError(
                        "thread result ref was published concurrently",
                    ) from exc
                raise
            logger.info("Created thread result commit for one delegation")
        return FinalizedThreadGitResult(
            base_commit=base_commit,
            result_commit=result_commit,
            result_ref=result_ref,
            changed_files=changed_files,
        )

    async def _resolve_existing_result_commit(
        self,
        repo_path: str,
        result_ref: str,
        tree_id: str,
        base_commit: str,
    ) -> str | None:
        """Return an existing result only when its tree and parent match."""
        if not await _ref_exists(repo_path, result_ref):
            return None
        existing_commit = await _run_git(
            ["rev-parse", "--verify", result_ref],
            cwd=repo_path,
        )
        existing_tree = await _run_git(
            ["rev-parse", f"{existing_commit}^{{tree}}"],
            cwd=repo_path,
        )
        commit_line = await _run_git(
            ["rev-list", "--parents", "-n", "1", existing_commit],
            cwd=repo_path,
        )
        existing_parents = commit_line.split()[1:]
        if existing_tree != tree_id or existing_parents != [base_commit]:
            raise ThreadResultRefConflictError(
                "existing thread result ref has different commit content",
            )
        return existing_commit

    async def _changed_files_since_base(
        self,
        repo_path: str,
        base_commit: str,
        result_commit: str,
    ) -> tuple[ChangedFile, ...]:
        """Return visible changed files between the base and result commits."""
        output = await _run_git_bytes(
            ["diff", "--name-status", "-z", "-M", base_commit, result_commit],
            cwd=repo_path,
        )
        changes = _parse_changed_files(output)
        if len(changes) > _MAX_CHANGED_FILE_ENTRIES:
            raise ThreadResultBoundsError(
                "thread result exceeds the changed-file entry limit",
            )
        return changes

    async def _assert_child_target_available(self, repo_path: str, branch: str) -> None:
        """Reject branch or worktree path collisions before creating anything."""
        refs_output = await _run_git(
            ["for-each-ref", "--format=%(refname)", f"refs/heads/{branch}"],
            cwd=repo_path,
        )
        if refs_output:
            raise ThreadBranchCollisionError(f"thread branch {branch} already exists")
        if os.path.lexists(os.path.join(repo_path, _WORKTREE_DIRECTORY, branch)):
            raise ThreadBranchCollisionError(f"thread worktree path for {branch} already exists")

    async def _create_snapshot(
        self,
        location: _ParentLocation,
        delegation_id: str,
        head: str | None,
    ) -> str:
        """Capture and publish one immutable snapshot commit.

        A private index preserves parent Git state. Atomic publication rejects
        a competing snapshot ref without replacing it.
        """
        await _assert_no_protected_candidates(location.workspace_path)
        tree_id = await _write_tree_through_alternate_index(
            location.workspace_path,
            base_treeish=head,
        )
        await self._assert_snapshot_within_limits(location.workspace_path, tree_id)

        commit_args = [
            *_COMMIT_IDENTITY_ARGS,
            "commit-tree",
            tree_id,
            "-m",
            f"Yinshi thread snapshot for delegation {delegation_id}",
        ]
        if head is not None:
            commit_args.append("-p")
            commit_args.append(head)
        snapshot_commit = await _run_git(commit_args, cwd=location.repo_path)
        try:
            await _run_git(
                ["update-ref", _snapshot_ref(delegation_id), snapshot_commit, _ZERO_OID],
                cwd=location.repo_path,
            )
        except GitError as exc:
            # A lost publication race means another writer now owns the ref;
            # a missing ref means a real Git fault. Both fail closed.
            if await _ref_exists(location.repo_path, _snapshot_ref(delegation_id)):
                raise ThreadSnapshotRefExistsError(
                    "snapshot ref already exists for this delegation",
                ) from exc
            raise
        logger.info("Captured thread snapshot ref for one delegation")
        return snapshot_commit

    async def _assert_snapshot_within_limits(
        self,
        workspace_path: str,
        tree_id: str,
    ) -> None:
        """Reject snapshots beyond the configured file-count and byte bounds."""
        settings = get_settings()
        output = await _run_git_bytes(
            ["ls-tree", "-r", "-l", "-z", tree_id],
            cwd=workspace_path,
        )
        file_count = 0
        total_bytes = 0
        for record in output.split(b"\0"):
            if not record:
                continue
            fields = record.split(b"\t")
            meta = fields[0].split()
            if len(meta) < 3 or meta[1] != b"blob":
                continue
            file_count += 1
            # ``ls-tree -l`` prints the object size space-padded before the
            # path tab, so the size is the last metadata token.
            if len(meta) >= 4 and meta[3].isdigit():
                total_bytes += int(meta[3])
        if file_count > settings.thread_snapshot_max_files:
            raise ThreadSnapshotLimitError(
                "thread snapshot exceeds the configured file-count limit"
            )
        if total_bytes > settings.thread_snapshot_max_bytes:
            raise ThreadSnapshotLimitError("thread snapshot exceeds the configured byte limit")

    def _insert_workspace_row(
        self,
        db: sqlite3.Connection,
        location: _ParentLocation,
        branch: str,
        worktree_path: str,
    ) -> str:
        """Insert one delegated workspace row and return its ID."""
        cursor = db.execute(
            """INSERT INTO workspaces (repo_id, name, branch, path, state, kind,
                                       parent_workspace_id)
               VALUES (?, ?, ?, ?, 'ready', 'delegated', ?)""",
            (location.repo_id, branch, branch, worktree_path, location.workspace_id),
        )
        db.commit()
        row = db.execute(
            "SELECT id FROM workspaces WHERE rowid = ?",
            (cursor.lastrowid,),
        ).fetchone()
        assert row is not None
        return str(row["id"])


async def _resolve_head_commit(workspace_path: str) -> str | None:
    """Return the workspace HEAD commit, or None for an unborn branch."""
    try:
        return await _run_git(
            ["rev-parse", "--verify", "--quiet", "HEAD"],
            cwd=workspace_path,
        )
    except GitError:
        return None
