"""Centralized delegated-child deletion policy for workspaces."""

from __future__ import annotations

import sqlite3

import pytest

GUARD_SEED_SQL = """
INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'r', '/tmp/r');
INSERT INTO workspaces (id, repo_id, name, branch, path)
    VALUES ('ws1', 'repo1', 'w', 'branch', '/tmp/r/w');
INSERT INTO sessions (id, workspace_id) VALUES ('root1', 'ws1');
INSERT INTO sessions (id, workspace_id) VALUES ('child1', 'ws1');
INSERT INTO thread_delegations (
    id, parent_session_id, child_session_id, idempotency_key,
    initiator, title, task, requested_model, status
) VALUES (
    'del1', 'root1', 'child1', 'key1',
    'agent', 'Child', 'task', 'model', 'running'
);
"""


def test_policy_rejects_workspace_parenting_delegated_children(db: sqlite3.Connection) -> None:
    """A workspace whose sessions parent children fails the deletion policy."""
    from yinshi.exceptions import WorkspaceHasDelegatedThreads
    from yinshi.services.workspace import ensure_workspace_has_no_delegated_children

    db.executescript(GUARD_SEED_SQL)
    db.commit()

    with pytest.raises(WorkspaceHasDelegatedThreads, match="delegated child threads"):
        ensure_workspace_has_no_delegated_children(db, "ws1")
