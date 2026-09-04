"""Read-only thread query service tests."""

from __future__ import annotations

import sqlite3

SEED_REPO_SQL = """
INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'r', '/tmp/r');
INSERT INTO workspaces (id, repo_id, name, branch, path)
    VALUES ('ws1', 'repo1', 'w', 'branch', '/tmp/r/w');
INSERT INTO sessions (id, workspace_id, title)
    VALUES ('root1', 'ws1', 'Root task');
INSERT INTO sessions (id, workspace_id, title)
    VALUES ('child1', 'ws1', 'Child task');
"""


def _seed_tree(db: sqlite3.Connection) -> None:
    """Create one root session with one delegated child."""
    db.executescript(SEED_REPO_SQL)
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status, role
           ) VALUES (
               'del1', 'root1', 'child1', 'key1',
               'agent', 'Child task', 'do it', 'model', 'running', 'implementation'
           )""")
    db.commit()


def test_get_thread_projects_root_and_child(db):
    """Existing sessions appear as roots and delegated children report parentage."""
    from yinshi.services.thread_queries import get_thread

    _seed_tree(db)

    root = get_thread(db, "root1")
    assert root["id"] == "root1"
    assert root["parent_id"] is None
    assert root["root_id"] == "root1"
    assert root["depth"] == 0
    assert root["origin"] == "user"
    assert root["delegation_id"] is None
    assert root["title"] == "Root task"
    assert root["child_count"] == 1
    assert root["active_child_count"] == 1

    child = get_thread(db, "child1")
    assert child["parent_id"] == "root1"
    assert child["root_id"] == "root1"
    assert child["depth"] == 1
    assert child["origin"] == "agent"
    assert child["delegation_id"] == "del1"
    assert child["state"] == "running"
    assert child["role"] == "implementation"
    assert child["child_count"] == 0


def test_get_thread_origin_comes_from_delegation_initiator(db):
    """A user-created delegation makes the child origin 'user', not 'agent'."""
    from yinshi.services.thread_queries import get_thread

    db.executescript(SEED_REPO_SQL)
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'del1', 'root1', 'child1', 'key1',
               'user', 'Manual child', 'task', 'model', 'running'
           )""")
    db.commit()

    child = get_thread(db, "child1")
    assert child["origin"] == "user"


def test_get_thread_fails_closed_on_parentage_cycle(db):
    """A parentage cycle must raise instead of looping forever."""
    from yinshi.services.thread_queries import ThreadCycleError, get_thread

    db.executescript(SEED_REPO_SQL)
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('child2', 'ws1')")
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'del1', 'root1', 'child1', 'key1',
               'agent', 't', 'task', 'model', 'running'
           )""")
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'del2', 'child1', 'child2', 'key2',
               'agent', 't', 'task', 'model', 'running'
           )""")
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'del3', 'child2', 'root1', 'key3',
               'agent', 't', 'task', 'model', 'running'
           )""")
    db.commit()

    try:
        get_thread(db, "root1")
    except ThreadCycleError:
        pass
    else:
        raise AssertionError("cycle was not detected")


def test_get_tree_returns_nodes_placeholders_and_counts(db):
    """The tree projection returns bounded nodes, placeholders, and counts."""
    from yinshi.services.thread_queries import get_tree

    _seed_tree(db)
    # One direct provisioning placeholder plus one under the running child.
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'del2', 'root1', 'key2',
               'user', 'Pending child', 'task', 'model', 'provisioning'
           )""")
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('grandchild1', 'ws1')")
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'del3', 'child1', 'grandchild1', 'key3',
               'agent', 'Grand task', 'task', 'model', 'queued'
           )""")
    db.commit()

    tree = get_tree(db, "root1")
    assert tree["root"]["id"] == "root1"
    node_ids = {node["id"] for node in tree["nodes"]}
    assert node_ids == {"child1", "grandchild1"}
    placeholder_ids = {placeholder["delegation_id"] for placeholder in tree["placeholders"]}
    assert placeholder_ids == {"del2"}
    assert tree["thread_count"] == 4
    assert tree["active_descendant_count"] == 3  # running child + queued grandchild + placeholder
    depths = {node["id"]: node["depth"] for node in tree["nodes"]}
    assert depths == {"child1": 1, "grandchild1": 2}


def test_get_tree_from_any_member_resolves_root_tree(db):
    """A tree request for a nested member returns the full root tree."""
    from yinshi.services.thread_queries import get_tree

    _seed_tree(db)
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('grandchild1', 'ws1')")
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'del3', 'child1', 'grandchild1', 'key3',
               'agent', 'Grand task', 'task', 'model', 'queued'
           )""")
    db.commit()

    tree = get_tree(db, "grandchild1")
    assert tree["root"]["id"] == "root1"
    node_ids = {node["id"] for node in tree["nodes"]}
    assert node_ids == {"child1", "grandchild1"}
    by_id = {node["id"]: node for node in tree["nodes"]}
    assert by_id["child1"]["parent_id"] == "root1"
    assert by_id["child1"]["root_id"] == "root1"
    assert by_id["grandchild1"]["parent_id"] == "child1"
    assert by_id["grandchild1"]["root_id"] == "root1"
    assert tree["thread_count"] == 3


def test_thread_limits_report_usage_and_spawning_allowance(db, monkeypatch, tmp_path):
    """Limits expose configured bounds, usage, and can_spawn_child."""
    from yinshi.services.thread_queries import get_thread_limits

    _seed_tree(db)

    limits = get_thread_limits(db, "root1")
    assert limits["max_depth"] == 1
    assert limits["max_direct_children"] == 4
    assert limits["max_active_descendants"] == 4
    assert limits["max_total_threads"] == 20
    assert limits["tree_depth"] == 1
    assert limits["direct_children"] == 1
    assert limits["active_descendants"] == 1
    assert limits["total_threads"] == 2
    assert limits["can_spawn_child"] is True

    child_limits = get_thread_limits(db, "child1")
    # The child already sits at maximum depth.
    assert child_limits["can_spawn_child"] is False


def test_thread_limits_count_placeholders_and_use_root_totals(db, monkeypatch):
    """Placeholders reserve limits, and a child check uses root-wide totals."""
    from yinshi.services.thread_queries import get_thread_limits

    monkeypatch.setenv("THREAD_MAX_DEPTH", "2")
    monkeypatch.setenv("THREAD_MAX_ACTIVE_DESCENDANTS", "4")
    from yinshi.config import get_settings

    get_settings.cache_clear()

    db.executescript(SEED_REPO_SQL)
    # Three running children plus one provisioning placeholder. A fourth
    # running child sits below c2 so root-wide totals reach the cap.
    for child_id, key in (("c2", "k2"), ("c3", "k3"), ("c4", "k4")):
        db.execute(
            "INSERT INTO sessions (id, workspace_id) VALUES (?, 'ws1')",
            (child_id,),
        )
        db.execute(
            """INSERT INTO thread_delegations (
                   id, parent_session_id, child_session_id, idempotency_key,
                   initiator, title, task, requested_model, status
               ) VALUES (?, 'root1', ?, ?, 'agent', 't', 'task', 'm', 'running')""",
            (f"del-{child_id}", child_id, key),
        )
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('grand1', 'ws1')")
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'del-g1', 'c2', 'grand1', 'kg1',
               'agent', 'g', 'task', 'm', 'running'
           )""")
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'del-ph', 'root1', 'kph',
               'user', 'Pending', 'task', 'm', 'provisioning'
           )""")
    db.commit()

    root_limits = get_thread_limits(db, "root1")
    # The placeholder counts as a direct child and an active descendant.
    assert root_limits["direct_children"] == 4
    assert root_limits["active_descendants"] == 5
    assert root_limits["total_threads"] == 6
    assert root_limits["can_spawn_child"] is False

    # c2 is not at max depth (2), but root-wide active descendants hit 4.
    child_limits = get_thread_limits(db, "c2")
    assert child_limits["active_descendants"] == 5
    assert child_limits["total_threads"] == 6
    assert child_limits["can_spawn_child"] is False
    get_settings.cache_clear()


def test_legacy_owner_counts_authorized_placeholders(db):
    """Legacy owner filtering keeps authorized placeholder reservations in counts."""
    from yinshi.services.thread_queries import get_thread

    db.executescript("""
        INSERT INTO repos (id, name, root_path, owner_email)
            VALUES ('repoA', 'a', '/tmp/rA', 'a@example.com');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('wsA', 'repoA', 'w', 'b', '/tmp/rA/w');
        INSERT INTO sessions (id, workspace_id, title)
            VALUES ('sessA', 'wsA', 'Owned root');
        INSERT INTO thread_delegations (
            id, parent_session_id, idempotency_key,
            initiator, title, task, requested_model, status
        ) VALUES (
            'del-pending', 'sessA', 'pending-key',
            'user', 'Pending', 'task', 'm', 'provisioning'
        );
    """)
    db.commit()

    thread = get_thread(db, "sessA", owner_email="a@example.com")
    assert thread["child_count"] == 1
    assert thread["active_child_count"] == 1


def test_legacy_cross_owner_ancestry_fails_closed(db):
    """Corrupted cross-owner ancestry leaks no foreign descendant metadata."""
    from yinshi.services.thread_queries import get_thread, get_tree, list_children

    db.executescript("""
        INSERT INTO repos (id, name, root_path, owner_email)
            VALUES ('repoA', 'a', '/tmp/rA', 'a@example.com');
        INSERT INTO repos (id, name, root_path, owner_email)
            VALUES ('repoB', 'b', '/tmp/rB', 'b@example.com');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('wsA', 'repoA', 'w', 'b', '/tmp/rA/w');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('wsB', 'repoB', 'w', 'b', '/tmp/rB/w');
        INSERT INTO sessions (id, workspace_id, title)
            VALUES ('sessA', 'wsA', 'Owned root');
        INSERT INTO sessions (id, workspace_id, title)
            VALUES ('sessB', 'wsB', 'Foreign child');
        -- Corrupted ancestry written directly: the foreign session appears
        -- as a child of the owned session.
        INSERT INTO thread_delegations (
            id, parent_session_id, child_session_id, idempotency_key,
            initiator, title, task, requested_model, status
        ) VALUES (
            'del-x', 'sessA', 'sessB', 'kx',
            'agent', 'Smuggled', 'task', 'm', 'running'
        );
    """)
    db.commit()

    parent = get_thread(db, "sessA", owner_email="a@example.com")
    assert parent["child_count"] == 0
    assert parent["active_child_count"] == 0
    assert parent["can_spawn_child"] is True

    assert list_children(db, "sessA", owner_email="a@example.com") == []

    tree = get_tree(db, "sessA", owner_email="a@example.com")
    assert tree["nodes"] == []
    assert tree["thread_count"] == 1
    titles = {node["title"] for node in tree["nodes"]}
    assert "Foreign child" not in titles
