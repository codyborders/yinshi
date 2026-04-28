"""Workspace file tree, Git status, and safe file access helpers."""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from yinshi.exceptions import GitError
from yinshi.utils.paths import is_path_inside

FileNodeType = Literal["file", "directory"]
ChangeKind = Literal["added", "copied", "deleted", "modified", "renamed", "untracked", "unknown"]

_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".cache",
        ".git",
        ".hg",
        ".mypy_cache",
        ".next",
        ".parcel-cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "out",
        "target",
        "venv",
    }
)
_SECRET_FILE_NAMES = frozenset({".env"})
_SECRET_FILE_PREFIXES = (".env.",)
_MAX_TREE_ENTRIES = 5000
_MAX_TEXT_BYTES = 512 * 1024
_GUARDRAIL_MARKER = "# Yinshi secret guardrails"
_GUARDRAIL_PATTERNS = (".env", ".env.*")
_PRE_COMMIT_MARKER = "# Yinshi secret commit guard"
_PRE_PUSH_MARKER = "# Yinshi secret push guard"
_SECRET_PATH_GREP = "grep -E '(^|/)\\.env(\\..*)?$' >/dev/null"
_PRE_COMMIT_GUARD = f"""{_PRE_COMMIT_MARKER}
if git diff --cached --name-only --diff-filter=ACM | {_SECRET_PATH_GREP}; then
  echo 'Yinshi blocks committing .env files. Move secrets out of Git.' >&2
  exit 1
fi
"""
_PRE_PUSH_GUARD = f"""{_PRE_PUSH_MARKER}
if git ls-files | {_SECRET_PATH_GREP}; then
  echo 'Yinshi blocks pushing tracked .env files. Move secrets out of Git.' >&2
  exit 1
fi
"""


@dataclass(frozen=True, slots=True)
class FileNode:
    """One visible file tree node."""

    name: str
    path: str
    type: FileNodeType
    children: tuple["FileNode", ...] = ()


@dataclass(frozen=True, slots=True)
class ChangedFile:
    """One visible changed file from Git status."""

    path: str
    status: str
    kind: ChangeKind
    original_path: str | None = None


@dataclass(frozen=True, slots=True)
class _VisibleDirectoryEntry:
    """One lstat-classified child entry safe to include in the UI tree."""

    path: Path
    relative_path: str
    node_type: FileNodeType


def _workspace_root(workspace_path: str) -> Path:
    """Return a validated workspace root path."""
    if not isinstance(workspace_path, str):
        raise TypeError("workspace_path must be a string")
    normalized_workspace_path = workspace_path.strip()
    if not normalized_workspace_path:
        raise ValueError("workspace_path must not be empty")
    root = Path(normalized_workspace_path).resolve()
    if not root.is_dir():
        raise FileNotFoundError("workspace path does not exist")
    return root


def _repo_root(repo_root_path: str) -> Path | None:
    """Return a repository root path, or None when a mocked path is absent."""
    if not isinstance(repo_root_path, str):
        raise TypeError("repo_root_path must be a string")
    normalized_repo_root_path = repo_root_path.strip()
    if not normalized_repo_root_path:
        raise ValueError("repo_root_path must not be empty")
    root = Path(normalized_repo_root_path).resolve()
    if not root.is_dir():
        return None
    return root


def _is_secret_path(relative_path: str) -> bool:
    """Return whether a relative path points at a protected .env-style file."""
    parts = Path(relative_path).parts
    if not parts:
        return False
    for part in parts:
        if part in _SECRET_FILE_NAMES:
            return True
        if part.startswith(_SECRET_FILE_PREFIXES):
            return True
    return False


def _has_excluded_segment(relative_path: str) -> bool:
    """Return whether any path segment is intentionally hidden from the UI."""
    parts = Path(relative_path).parts
    return any(part in _EXCLUDED_DIRECTORY_NAMES for part in parts)


def _is_visible_relative_path(relative_path: str) -> bool:
    """Return whether a relative path is safe to show in workspace UI."""
    if not relative_path or relative_path == ".":
        return True
    if _is_secret_path(relative_path):
        return False
    return not _has_excluded_segment(relative_path)


def validate_visible_relative_path(workspace_path: str, relative_path: str) -> Path:
    """Resolve one user-supplied relative path inside the workspace and UI allowlist."""
    if not isinstance(relative_path, str):
        raise TypeError("relative_path must be a string")
    normalized_relative_path = relative_path.strip().lstrip("/")
    if not normalized_relative_path:
        raise ValueError("relative_path must not be empty")
    if os.path.isabs(relative_path):
        raise ValueError("relative_path must not be absolute")
    root = _workspace_root(workspace_path)
    candidate = (root / normalized_relative_path).resolve()
    if not is_path_inside(str(candidate), str(root)):
        raise ValueError("path must stay inside workspace")
    display_path = candidate.relative_to(root).as_posix()
    if not _is_visible_relative_path(display_path):
        raise PermissionError("path is not available through the workspace UI")
    return candidate


def _node_to_dict(node: FileNode) -> dict[str, object]:
    """Serialize one file node for API responses."""
    return {
        "name": node.name,
        "path": node.path,
        "type": node.type,
        "children": [_node_to_dict(child) for child in node.children],
    }


def file_tree_to_dicts(nodes: tuple[FileNode, ...]) -> list[dict[str, object]]:
    """Serialize file tree nodes for API responses."""
    return [_node_to_dict(node) for node in nodes]


def _visible_directory_entries(
    directory_path: Path, root: Path
) -> tuple[_VisibleDirectoryEntry, ...]:
    """Return visible child entries without following symlinks."""
    entries: list[_VisibleDirectoryEntry] = []
    for child in directory_path.iterdir():
        try:
            relative_path = child.relative_to(root).as_posix()
        except ValueError:
            continue
        if not _is_visible_relative_path(relative_path):
            continue
        try:
            child_stat = child.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(child_stat.st_mode):
            continue
        if stat.S_ISDIR(child_stat.st_mode):
            node_type: FileNodeType = "directory"
        elif stat.S_ISREG(child_stat.st_mode):
            node_type = "file"
        else:
            continue
        entries.append(
            _VisibleDirectoryEntry(
                path=child,
                relative_path=relative_path,
                node_type=node_type,
            )
        )
    return tuple(
        sorted(entries, key=lambda entry: (entry.node_type == "file", entry.path.name.lower()))
    )


def build_file_tree(workspace_path: str) -> tuple[FileNode, ...]:
    """Build a bounded visible file tree for one workspace."""
    root = _workspace_root(workspace_path)
    entry_count = 0

    def build_directory(directory_path: Path) -> tuple[FileNode, ...]:
        nonlocal entry_count
        children: list[FileNode] = []
        for entry in _visible_directory_entries(directory_path, root):
            if entry_count >= _MAX_TREE_ENTRIES:
                break
            entry_count += 1
            if entry.node_type == "directory":
                children.append(
                    FileNode(
                        name=entry.path.name,
                        path=entry.relative_path,
                        type="directory",
                        children=build_directory(entry.path),
                    )
                )
            else:
                children.append(
                    FileNode(name=entry.path.name, path=entry.relative_path, type="file")
                )
        return tuple(children)

    return build_directory(root)


def _change_kind(status: str) -> ChangeKind:
    """Map Git porcelain status text to a UI change kind."""
    if "?" in status:
        return "untracked"
    if "R" in status:
        return "renamed"
    if "C" in status:
        return "copied"
    if "D" in status:
        return "deleted"
    if "A" in status:
        return "added"
    if "M" in status or "T" in status:
        return "modified"
    return "unknown"


def _parse_porcelain_z(output: bytes) -> tuple[ChangedFile, ...]:
    """Parse null-delimited Git porcelain v1 status output."""
    records = [
        record for record in output.decode("utf-8", errors="surrogateescape").split("\0") if record
    ]
    changes: list[ChangedFile] = []
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 4:
            index += 1
            continue
        status = record[:2]
        path_text = record[3:]
        original_path = None
        index += 1
        if ("R" in status or "C" in status) and index < len(records):
            original_path = records[index]
            index += 1
        if _is_visible_relative_path(path_text):
            changes.append(
                ChangedFile(
                    path=path_text,
                    status=status,
                    kind=_change_kind(status),
                    original_path=original_path,
                )
            )
    return tuple(changes)


async def changed_files(workspace_path: str) -> tuple[ChangedFile, ...]:
    """Return visible changed files from Git status for one workspace."""
    root = _workspace_root(workspace_path)
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(root),
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise GitError(stderr.decode("utf-8", errors="replace") or "git status failed")
    return _parse_porcelain_z(stdout)


def changed_files_to_dicts(changes: tuple[ChangedFile, ...]) -> list[dict[str, object]]:
    """Serialize changed files for API responses."""
    return [
        {
            "path": change.path,
            "status": change.status,
            "kind": change.kind,
            "original_path": change.original_path,
        }
        for change in changes
    ]


def read_text_file(workspace_path: str, relative_path: str) -> str:
    """Read a bounded UTF-8-ish text file from a visible workspace path."""
    file_path = validate_visible_relative_path(workspace_path, relative_path)
    if not file_path.is_file():
        raise FileNotFoundError("file does not exist")
    data = file_path.read_bytes()
    if len(data) > _MAX_TEXT_BYTES:
        raise ValueError("file is too large to preview")
    if b"\x00" in data:
        raise ValueError("binary files cannot be previewed")
    return data.decode("utf-8", errors="replace")


def write_text_file(workspace_path: str, relative_path: str, content: str) -> None:
    """Write text content to a visible workspace file path."""
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    encoded_content = content.encode("utf-8")
    if len(encoded_content) > _MAX_TEXT_BYTES:
        raise ValueError("file is too large to edit through the browser")
    file_path = validate_visible_relative_path(workspace_path, relative_path)
    if file_path.exists() and not file_path.is_file():
        raise ValueError("path is not a file")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


async def _changed_file_for_path(root: Path, display_path: str) -> ChangedFile | None:
    """Return Git status for one path without scanning the whole worktree."""
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(root),
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        display_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise GitError(stderr.decode("utf-8", errors="replace") or "git status failed")
    return next(iter(_parse_porcelain_z(stdout)), None)


async def diff_file(workspace_path: str, relative_path: str) -> str:
    """Return a Git diff for one visible file path."""
    root = _workspace_root(workspace_path)
    file_path = validate_visible_relative_path(workspace_path, relative_path)
    display_path = file_path.relative_to(root).as_posix()
    matching_change = await _changed_file_for_path(root, display_path)
    if matching_change is not None and matching_change.kind == "untracked":
        content = read_text_file(workspace_path, display_path)
        added_lines = [f"+{line}" for line in content.splitlines()]
        return "\n".join([f"+++ b/{display_path}", *added_lines])

    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(root),
        "diff",
        "HEAD",
        "--",
        display_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise GitError(stderr.decode("utf-8", errors="replace") or "git diff failed")
    return stdout.decode("utf-8", errors="replace")


def _install_secret_hook_guard(hook_path: Path, marker: str, guard_script: str) -> None:
    """Install a secret guard before any existing hook body can exit."""
    existing_hook = hook_path.read_text(encoding="utf-8") if hook_path.exists() else "#!/bin/sh\n"
    if marker not in existing_hook:
        if existing_hook.startswith("#!"):
            shebang, separator, remainder = existing_hook.partition("\n")
            existing_body = remainder if separator else ""
            updated_hook = shebang + "\n" + guard_script + existing_body
        else:
            updated_hook = "#!/bin/sh\n" + guard_script + existing_hook
        hook_path.write_text(updated_hook, encoding="utf-8")
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR)


def ensure_secret_guardrails(repo_root_path: str) -> None:
    """Install repo-local guardrails that keep .env files out of normal Git flow."""
    root = _repo_root(repo_root_path)
    if root is None:
        return
    git_dir = root / ".git"
    if not git_dir.is_dir():
        return
    info_dir = git_dir / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    exclude_path = info_dir / "exclude"
    existing_exclude = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    if _GUARDRAIL_MARKER not in existing_exclude:
        suffix = "" if existing_exclude.endswith("\n") or not existing_exclude else "\n"
        exclude_path.write_text(
            existing_exclude
            + suffix
            + _GUARDRAIL_MARKER
            + "\n"
            + "\n".join(_GUARDRAIL_PATTERNS)
            + "\n",
            encoding="utf-8",
        )

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    _install_secret_hook_guard(hooks_dir / "pre-commit", _PRE_COMMIT_MARKER, _PRE_COMMIT_GUARD)
    _install_secret_hook_guard(hooks_dir / "pre-push", _PRE_PUSH_MARKER, _PRE_PUSH_GUARD)
