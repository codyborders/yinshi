"""Read-only thread projections over existing sessions and delegations.

A thread is a projection over an existing ``sessions`` row. Parentage comes
exclusively from ``thread_delegations.child_session_id``. Each metadata read
resolves one bounded root-tree snapshot (``_RootTreeSnapshot``) that owns root
resolution, visible descendants, placeholders, depths, counts, and the
configured limits. Projections are pure views over that snapshot. Every walk
is bounded and fails closed on cycles.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

from yinshi.config import get_settings

ACTIVE_DELEGATION_STATUSES: Final[frozenset[str]] = frozenset(
    {"provisioning", "queued", "running", "cancelling"}
)
TERMINAL_DELEGATION_STATUSES: Final[frozenset[str]] = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)
_ANCESTRY_HOP_LIMIT: Final[int] = 32
_TREE_NODE_LIMIT: Final[int] = 500


class ThreadCycleError(RuntimeError):
    """Raised when parentage records contain a cycle."""


class ThreadTreeSizeError(RuntimeError):
    """Raised when a thread tree exceeds the size bounds."""


class ThreadNotFoundError(KeyError):
    """Raised when a session does not exist."""


@dataclass(frozen=True, slots=True)
class _TreeLimits:
    """Configured spawn bounds applied to one root-tree snapshot."""

    max_depth: int
    max_direct_children: int
    max_active_descendants: int
    max_total_threads: int

    @classmethod
    def from_settings(cls) -> _TreeLimits:
        """Capture the currently configured thread limits."""
        settings = get_settings()
        return cls(
            max_depth=settings.thread_max_depth,
            max_direct_children=settings.thread_max_direct_children,
            max_active_descendants=settings.thread_max_active_descendants,
            max_total_threads=settings.thread_max_total,
        )

    def allows_spawn(
        self,
        depth: int,
        own_children: int,
        active_descendants: int,
        total_threads: int,
    ) -> bool:
        """Return whether one candidate spawn stays inside every limit."""
        return (
            depth < self.max_depth
            and own_children < self.max_direct_children
            and active_descendants < self.max_active_descendants
            and total_threads < self.max_total_threads
        )


@dataclass(frozen=True, slots=True)
class _DelegationView:
    """Owner-agnostic view of one delegation row from any query shape."""

    id: str
    parent_id: str
    role: str
    status: str
    initiator: str

    @classmethod
    def from_chain_row(cls, row: sqlite3.Row) -> _DelegationView:
        """Read a delegation from an ancestry-chain ``SELECT *`` row."""
        return cls(
            id=str(row["id"]),
            parent_id=str(row["parent_session_id"]),
            role=str(row["role"]),
            status=str(row["status"]),
            initiator=str(row["initiator"]),
        )

    @classmethod
    def from_walk_row(cls, row: sqlite3.Row) -> _DelegationView:
        """Read a delegation from a joined descendant-walk row alias."""
        return cls(
            id=str(row["delegation_id"]),
            parent_id=str(row["parent_session_id"]),
            role=str(row["delegation_role"]),
            status=str(row["delegation_status"]),
            initiator=str(row["initiator"]),
        )


@dataclass(frozen=True, slots=True)
class _TreeDescendant:
    """One visible delegated child row plus its depth below the root."""

    row: sqlite3.Row
    depth: int

    @property
    def session_id(self) -> str:
        """Return the child session id."""
        return str(self.row["id"])

    @property
    def parent_id(self) -> str:
        """Return the session id this child was delegated from."""
        return str(self.row["parent_session_id"])

    @property
    def is_active(self) -> bool:
        """Return whether the child's delegation is still active."""
        return str(self.row["delegation_status"]) in ACTIVE_DELEGATION_STATUSES


@dataclass(frozen=True, slots=True)
class _RootTreeSnapshot:
    """One bounded root-tree read shared by every projection.

    The snapshot owns root resolution, the visible descendant walk, bounded
    placeholders, per-parent child counts, active/total counts, the tree
    depth, and the configured limits. It is immutable and never cached, so
    each metadata read reuses one captured traversal.
    """

    root_id: str
    root_session: sqlite3.Row
    member_session: sqlite3.Row
    member_chain: tuple[sqlite3.Row, ...]
    descendants: tuple[_TreeDescendant, ...]
    placeholders: tuple[dict[str, Any], ...]
    child_counts: Mapping[str, int]
    active_child_counts: Mapping[str, int]
    active_descendants: int
    total_threads: int
    tree_depth: int
    limits: _TreeLimits

    @property
    def member_depth(self) -> int:
        """Return the requested session's hop distance below the root."""
        return len(self.member_chain)

    @property
    def member_delegation(self) -> sqlite3.Row | None:
        """Return the delegation that made the requested session a child."""
        return self.member_chain[0] if self.member_chain else None

    def child_count(self, session_id: str) -> int:
        """Return the visible direct-child count for one session."""
        return int(self.child_counts.get(session_id, 0))

    def active_child_count(self, session_id: str) -> int:
        """Return the visible active direct-child count for one session."""
        return int(self.active_child_counts.get(session_id, 0))

    def allows_spawn(self, depth: int, own_children: int) -> bool:
        """Apply the configured limits using root-wide active/total counts."""
        return self.limits.allows_spawn(
            depth,
            own_children,
            self.active_descendants,
            self.total_threads,
        )


def _fetch_session(db: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    """Load one session row or raise ThreadNotFoundError."""
    row = cast(
        sqlite3.Row,
        db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone(),
    )
    if row is None:
        raise ThreadNotFoundError(session_id)
    return row


def _child_delegation(
    db: sqlite3.Connection,
    session_id: str,
) -> sqlite3.Row | None:
    """Return the delegation that made this session a child, when present."""
    return cast(
        sqlite3.Row | None,
        db.execute(
            "SELECT * FROM thread_delegations WHERE child_session_id = ?",
            (session_id,),
        ).fetchone(),
    )


def _direct_children(
    db: sqlite3.Connection,
    session_id: str,
    owner_email: str | None = None,
) -> list[sqlite3.Row]:
    """Return child session rows joined with their delegations.

    With ``owner_email`` set (legacy mode), children in workspaces owned by a
    different account are excluded so foreign metadata is never disclosed.
    """
    if owner_email is None:
        return db.execute(
            """SELECT s.*, d.id AS delegation_id, d.status AS delegation_status,
                  d.role AS delegation_role, d.created_at AS delegated_at,
                  d.parent_session_id AS parent_session_id,
                  d.initiator AS initiator
           FROM thread_delegations d
           JOIN sessions s ON s.id = d.child_session_id
           WHERE d.parent_session_id = :session_id
           ORDER BY d.created_at, d.id""",
            {"session_id": session_id},
        ).fetchall()
    return db.execute(
        """SELECT s.*, d.id AS delegation_id, d.status AS delegation_status,
                  d.role AS delegation_role, d.created_at AS delegated_at,
                  d.parent_session_id AS parent_session_id,
                  d.initiator AS initiator
           FROM thread_delegations d
           JOIN sessions s ON s.id = d.child_session_id
           JOIN workspaces w ON s.workspace_id = w.id
           JOIN repos r ON w.repo_id = r.id
           WHERE d.parent_session_id = :session_id
             AND (:owner_email IS NULL
                  OR r.owner_email IS NULL
                  OR r.owner_email = :owner_email)
           ORDER BY d.created_at, d.id""",
        {"owner_email": owner_email, "session_id": session_id},
    ).fetchall()


def _ancestry_chain(
    db: sqlite3.Connection,
    session_id: str,
    owner_email: str | None = None,
) -> list[sqlite3.Row]:
    """Return child-delegation rows from this session up to its visible root."""
    chain: list[sqlite3.Row] = []
    visited: set[str] = {session_id}
    current: str | None = session_id
    while current is not None:
        delegation = _child_delegation(db, current)
        if delegation is None:
            break
        parent_id = str(delegation["parent_session_id"])
        if owner_email is not None:
            parent = db.execute(
                "SELECT r.owner_email FROM sessions s "
                "JOIN workspaces w ON s.workspace_id = w.id "
                "JOIN repos r ON w.repo_id = r.id WHERE s.id = ?",
                (parent_id,),
            ).fetchone()
            if parent is None or parent["owner_email"] not in {None, owner_email}:
                raise ThreadNotFoundError(session_id)
        if parent_id in visited:
            raise ThreadCycleError("thread parentage records contain a cycle")
        chain.append(delegation)
        visited.add(parent_id)
        current = parent_id
        if len(chain) > _ANCESTRY_HOP_LIMIT:
            raise ThreadCycleError("thread parentage ancestry exceeds the hop limit")
    return chain


def _descendant_nodes(
    db: sqlite3.Connection,
    root_id: str,
    *,
    owner_email: str | None = None,
) -> list[_TreeDescendant]:
    """Collect visible descendant nodes with strict depth and node bounds."""
    nodes: list[_TreeDescendant] = []
    frontier: deque[tuple[str, int]] = deque([(root_id, 0)])
    seen: set[str] = {root_id}
    while frontier:
        parent_id, depth = frontier.popleft()
        if depth >= _ANCESTRY_HOP_LIMIT:
            continue
        for row in _direct_children(db, parent_id, owner_email=owner_email):
            child_id = str(row["id"])
            if child_id in seen:
                raise ThreadCycleError("thread parentage records contain a cycle")
            seen.add(child_id)
            nodes.append(_TreeDescendant(row=row, depth=depth + 1))
            if len(nodes) > _TREE_NODE_LIMIT:
                raise ThreadTreeSizeError("thread tree exceeds the size bound")
            frontier.append((child_id, depth + 1))
    return nodes


def _placeholder_rows(
    db: sqlite3.Connection,
    session_ids: list[str],
    *,
    max_rows: int,
    owner_email: str | None = None,
) -> list[dict[str, Any]]:
    """Return bounded provisioning delegations without child sessions."""
    placeholders: list[dict[str, Any]] = []
    for parent_id in session_ids:
        if owner_email is None:
            rows = db.execute(
                """SELECT id, parent_session_id, title, role, status, created_at
                   FROM thread_delegations
                   WHERE child_session_id IS NULL AND parent_session_id = ?
                   ORDER BY created_at, id""",
                (parent_id,),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT d.id, d.parent_session_id, d.title, d.role,
                          d.status, d.created_at
                   FROM thread_delegations d
                   JOIN sessions s ON s.id = d.parent_session_id
                   JOIN workspaces w ON s.workspace_id = w.id
                   JOIN repos r ON w.repo_id = r.id
                   WHERE d.child_session_id IS NULL AND d.parent_session_id = :parent_id
                     AND (r.owner_email IS NULL OR r.owner_email = :owner_email)
                   ORDER BY d.created_at, d.id""",
                {"owner_email": owner_email, "parent_id": parent_id},
            ).fetchall()
        for row in rows:
            placeholders.append(
                {
                    "delegation_id": str(row["id"]),
                    "parent_id": str(row["parent_session_id"]),
                    "title": row["title"],
                    "role": str(row["role"]),
                    "status": str(row["status"]),
                    "created_at": row["created_at"],
                }
            )
            if len(placeholders) > max_rows:
                raise ThreadTreeSizeError("thread placeholders exceed the size bound")
    return placeholders


def _build_root_tree_snapshot(
    db: sqlite3.Connection,
    session_id: str,
    owner_email: str | None = None,
) -> _RootTreeSnapshot:
    """Resolve one bounded root-tree snapshot with a single traversal."""
    session = _fetch_session(db, session_id)
    chain = _ancestry_chain(db, session_id, owner_email=owner_email)
    root_id = str(chain[-1]["parent_session_id"]) if chain else session_id
    root_session = session if root_id == session_id else _fetch_session(db, root_id)

    descendants = _descendant_nodes(db, root_id, owner_email=owner_email)
    placeholders = _placeholder_rows(
        db,
        [root_id] + [node.session_id for node in descendants],
        max_rows=_TREE_NODE_LIMIT - len(descendants),
        owner_email=owner_email,
    )

    child_counts: Counter[str] = Counter()
    active_child_counts: Counter[str] = Counter()
    active_descendants = 0
    tree_depth = 0
    for node in descendants:
        child_counts[node.parent_id] += 1
        if node.is_active:
            active_child_counts[node.parent_id] += 1
            active_descendants += 1
        tree_depth = max(tree_depth, node.depth)
    for placeholder in placeholders:
        parent_id = str(placeholder["parent_id"])
        child_counts[parent_id] += 1
        if str(placeholder["status"]) in ACTIVE_DELEGATION_STATUSES:
            active_child_counts[parent_id] += 1
            active_descendants += 1

    return _RootTreeSnapshot(
        root_id=root_id,
        root_session=root_session,
        member_session=session,
        member_chain=tuple(chain),
        descendants=tuple(descendants),
        placeholders=tuple(placeholders),
        child_counts=child_counts,
        active_child_counts=active_child_counts,
        active_descendants=active_descendants,
        total_threads=1 + len(descendants) + len(placeholders),
        tree_depth=tree_depth,
        limits=_TreeLimits.from_settings(),
    )


def _project_thread(
    snapshot: _RootTreeSnapshot,
    *,
    session: sqlite3.Row,
    delegation: _DelegationView | None,
    depth: int,
) -> dict[str, Any]:
    """Project one session row and its child delegation as a thread."""
    session_id = str(session["id"])
    child_count = snapshot.child_count(session_id)
    return {
        "id": session_id,
        "delegation_id": None if delegation is None else delegation.id,
        "parent_id": None if delegation is None else delegation.parent_id,
        "root_id": snapshot.root_id,
        "depth": depth,
        "title": session["title"],
        "role": "general" if delegation is None else delegation.role,
        "origin": "user" if delegation is None else delegation.initiator,
        "state": str(session["status"]) if delegation is None else delegation.status,
        "workspace_id": str(session["workspace_id"]),
        "model": str(session["model"]),
        "child_count": child_count,
        "active_child_count": snapshot.active_child_count(session_id),
        "can_spawn_child": snapshot.allows_spawn(depth, child_count),
        "created_at": session["created_at"],
    }


def _project_root_thread(snapshot: _RootTreeSnapshot) -> dict[str, Any]:
    """Project the snapshot root, which never carries a child delegation."""
    return _project_thread(snapshot, session=snapshot.root_session, delegation=None, depth=0)


def _project_descendant_thread(
    snapshot: _RootTreeSnapshot,
    node: _TreeDescendant,
) -> dict[str, Any]:
    """Project one visible descendant from its joined walk row."""
    return _project_thread(
        snapshot,
        session=node.row,
        delegation=_DelegationView.from_walk_row(node.row),
        depth=node.depth,
    )


def get_thread(
    db: sqlite3.Connection,
    session_id: str,
    owner_email: str | None = None,
) -> dict[str, Any]:
    """Return one bounded thread projection for a session."""
    snapshot = _build_root_tree_snapshot(db, session_id, owner_email=owner_email)
    member_delegation = snapshot.member_delegation
    return _project_thread(
        snapshot,
        session=snapshot.member_session,
        delegation=(
            _DelegationView.from_chain_row(member_delegation)
            if member_delegation is not None
            else None
        ),
        depth=snapshot.member_depth,
    )


def get_tree(
    db: sqlite3.Connection,
    session_id: str,
    owner_email: str | None = None,
) -> dict[str, Any]:
    """Return the bounded thread tree containing the requested session."""
    snapshot = _build_root_tree_snapshot(db, session_id, owner_email=owner_email)
    return {
        "root": _project_root_thread(snapshot),
        "nodes": [_project_descendant_thread(snapshot, node) for node in snapshot.descendants],
        "placeholders": list(snapshot.placeholders),
        "thread_count": snapshot.total_threads,
        "active_descendant_count": snapshot.active_descendants,
        "tree_depth": snapshot.tree_depth,
    }


def list_children(
    db: sqlite3.Connection,
    session_id: str,
    owner_email: str | None = None,
) -> list[dict[str, Any]]:
    """Return bounded projections for the direct children of a session."""
    snapshot = _build_root_tree_snapshot(db, session_id, owner_email=owner_email)
    return [
        _project_descendant_thread(snapshot, node)
        for node in snapshot.descendants
        if node.parent_id == session_id
    ]


def get_thread_limits(
    db: sqlite3.Connection,
    session_id: str,
    owner_email: str | None = None,
) -> dict[str, Any]:
    """Return configured limits with root-wide usage and remaining allowances."""
    snapshot = _build_root_tree_snapshot(db, session_id, owner_email=owner_email)
    direct_children = snapshot.child_count(session_id)
    return {
        "max_depth": snapshot.limits.max_depth,
        "max_direct_children": snapshot.limits.max_direct_children,
        "max_active_descendants": snapshot.limits.max_active_descendants,
        "max_total_threads": snapshot.limits.max_total_threads,
        "tree_depth": snapshot.tree_depth,
        "direct_children": direct_children,
        "active_descendants": snapshot.active_descendants,
        "total_threads": snapshot.total_threads,
        "can_spawn_child": snapshot.allows_spawn(snapshot.member_depth, direct_children),
    }


def get_thread_result(
    db: sqlite3.Connection,
    session_id: str,
    owner_email: str | None = None,
) -> dict[str, Any] | None:
    """Return the stored result for a visible delegated child, when present."""
    _fetch_session(db, session_id)
    _ancestry_chain(db, session_id, owner_email=owner_email)
    row = db.execute(
        """SELECT r.*
           FROM thread_results r
           JOIN thread_delegations d ON d.id = r.delegation_id
           WHERE d.child_session_id = ? AND r.sealed = 1""",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "delegation_id": str(row["delegation_id"]),
        "version": int(row["version"]),
        "source": str(row["source"]),
        "sealed": bool(row["sealed"]),
        "summary": row["summary"],
        "tests": json.loads(str(row["tests_json"])),
        "warnings": json.loads(str(row["warnings_json"])),
        "base_commit": row["base_commit"],
        "result_commit": row["result_commit"],
        "result_ref": row["result_ref"],
        "changed_files": json.loads(str(row["changed_files_json"])),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "sealed_at": row["sealed_at"],
    }
