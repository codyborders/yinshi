"""Desktop-local terminal context resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from yinshi.config import get_settings
from yinshi.db import get_db
from yinshi.utils.paths import is_path_inside


@dataclass(frozen=True, slots=True)
class DesktopTerminalContext:
    """Validated host paths and sidecar socket for one local workspace terminal."""

    workspace_path: str
    repo_root_path: str
    socket_path: str


def _managed_directory(path_value: str, *, base_path: str) -> str:
    """Resolve an existing directory and require it to remain inside managed storage."""
    if not path_value or not os.path.isabs(path_value):
        raise PermissionError("Desktop terminal path is outside managed storage")
    try:
        resolved_path = str(Path(path_value).resolve(strict=True))
        resolved_base = str(Path(base_path).resolve(strict=True))
    except OSError as error:
        raise PermissionError("Desktop terminal path is outside managed storage") from error
    if not os.path.isdir(resolved_path) or not is_path_inside(resolved_path, resolved_base):
        raise PermissionError("Desktop terminal path is outside managed storage")
    return resolved_path


def resolve_desktop_terminal_context(workspace_id: str) -> DesktopTerminalContext:
    """Resolve one local workspace terminal context from app-managed storage."""
    if not isinstance(workspace_id, str):
        raise TypeError("workspace_id must be a string")
    if not workspace_id or len(workspace_id) > 128:
        raise ValueError("workspace_id must contain 1-128 characters")
    settings = get_settings()
    if not settings.allowed_repo_base or not os.path.isabs(settings.allowed_repo_base):
        raise PermissionError("Desktop repository storage is not configured")
    if not os.path.isabs(settings.sidecar_socket_path):
        raise PermissionError("Desktop sidecar socket path is invalid")

    with get_db() as database:
        row = database.execute(
            """
            SELECT w.path AS workspace_path, r.root_path AS repo_root_path
            FROM workspaces w
            JOIN repos r ON r.id = w.repo_id
            WHERE w.id = ?
            """,
            (workspace_id,),
        ).fetchone()
    if row is None:
        raise LookupError("Desktop workspace not found")

    profile_directory = str(Path(settings.db_path).resolve().parent)
    socket_path = str(Path(settings.sidecar_socket_path).resolve())
    if not is_path_inside(socket_path, profile_directory):
        raise PermissionError("Desktop sidecar socket is outside managed storage")
    return DesktopTerminalContext(
        workspace_path=_managed_directory(
            row["workspace_path"],
            base_path=settings.allowed_repo_base,
        ),
        repo_root_path=_managed_directory(
            row["repo_root_path"],
            base_path=settings.allowed_repo_base,
        ),
        socket_path=socket_path,
    )
