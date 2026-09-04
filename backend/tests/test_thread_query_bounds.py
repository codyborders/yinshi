"""Bounds for read-only thread tree queries."""

from __future__ import annotations


def test_get_tree_rejects_excessive_placeholder_rows(db):
    """Placeholder reservations cannot bypass the tree query bound."""
    import pytest

    from yinshi.services.thread_queries import ThreadTreeSizeError, get_tree

    db.executescript("""
        INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'r', '/tmp/r');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('ws1', 'repo1', 'w', 'branch', '/tmp/r/w');
        INSERT INTO sessions (id, workspace_id, title)
            VALUES ('root1', 'ws1', 'Root task');
    """)
    db.executemany(
        """INSERT INTO thread_delegations (
               id, parent_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (?, 'root1', ?, 'user', 'Pending', 'task', 'm', 'provisioning')""",
        [(f"del-{index}", f"key-{index}") for index in range(501)],
    )
    db.commit()

    with pytest.raises(ThreadTreeSizeError):
        get_tree(db, "root1")
