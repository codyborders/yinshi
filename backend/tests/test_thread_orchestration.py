"""Thread orchestration lifecycle policy and spawn workflow tests."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import Request

from tests.test_thread_workspaces import seed_parent_stack
from yinshi.models import ThreadChildCreate
from yinshi.services.prompt_journal import PromptJournal
from yinshi.services.thread_lifecycle import (
    can_transition,
    cancellation_target,
    initial_run_idempotency_key,
    is_terminal_delegation_status,
)
from yinshi.services.thread_orchestration import ThreadSpawnOutcome


def test_child_create_rejects_unknown_client_fields() -> None:
    """Client requests cannot inject backend-derived orchestration fields."""
    with pytest.raises(ValueError):
        ThreadChildCreate(
            idempotency_key=str(uuid.uuid4()),
            title="Child",
            task="Review parser",
            workspace_id="foreign-workspace",
        )


def test_every_phase3_transition_is_exactly_encoded() -> None:
    """can_transition matches the full Phase 3 transition map exactly."""
    allowed = {
        ("provisioning", "queued"),
        ("provisioning", "failed"),
        ("provisioning", "cancelled"),
        ("queued", "running"),
        ("queued", "failed"),
        ("queued", "cancelled"),
        ("running", "cancelling"),
        ("running", "completed"),
        ("running", "failed"),
        ("running", "interrupted"),
        ("cancelling", "completed"),
        ("cancelling", "failed"),
        ("cancelling", "cancelled"),
        ("cancelling", "interrupted"),
    }
    statuses = (
        "provisioning",
        "queued",
        "running",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    )
    for current in statuses:
        for target in statuses:
            expected = (current, target) in allowed
            assert can_transition(current, target) is expected, (current, target)


def test_terminal_delegation_status_set_is_exact() -> None:
    """Only completed, failed, cancelled, and interrupted are terminal."""
    assert is_terminal_delegation_status("completed")
    assert is_terminal_delegation_status("failed")
    assert is_terminal_delegation_status("cancelled")
    assert is_terminal_delegation_status("interrupted")
    assert not is_terminal_delegation_status("provisioning")
    assert not is_terminal_delegation_status("queued")
    assert not is_terminal_delegation_status("running")
    assert not is_terminal_delegation_status("cancelling")


def test_cancellation_losing_to_completion_preserves_completion() -> None:
    """A cancelling delegation may still resolve to completed."""
    assert can_transition("cancelling", "completed")


def test_cancellation_targets_are_stable_per_status() -> None:
    """Cancellation resolves each status to its plan-stable target."""
    assert cancellation_target("provisioning") == "cancelled"
    assert cancellation_target("queued") == "cancelled"
    assert cancellation_target("running") == "cancelling"
    assert cancellation_target("cancelling") == "cancelling"
    assert cancellation_target("completed") == "completed"
    assert cancellation_target("failed") == "failed"
    assert cancellation_target("cancelled") == "cancelled"
    assert cancellation_target("interrupted") == "interrupted"


def test_initial_run_idempotency_key_is_deterministic_canonical_uuid() -> None:
    """The initial child run derives one stable canonical UUID from the delegation."""
    delegation_id = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
    first = initial_run_idempotency_key(delegation_id)
    assert first == initial_run_idempotency_key(delegation_id)
    assert str(uuid.UUID(first)) == first
    assert first != initial_run_idempotency_key("d4e5f6a7b8c9d0e1f2a3b4c5d6e7f802")
    with pytest.raises(ValueError):
        initial_run_idempotency_key("not-a-delegation-id")


def _orchestration_request() -> Request:
    """Build one minimal request carrying no tenant, like prompt-journal tests."""
    from types import SimpleNamespace

    app = SimpleNamespace(state=SimpleNamespace())
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
            "app": app,
            "state": {},
        }
    )


def test_spawn_child_stores_normalized_request_metadata(db, git_repo) -> None:
    """One spawn stores normalized request fields and attaches a queued child."""
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    body = ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()),
        title="  Implement OAuth validation  ",
        task="Implement state validation.",
        context="  Callback routes live in auth_routes.py.  ",
        role="implementation",
        model="model-x",
        thinking=" high ",
        start_immediately=False,
    )

    outcome = asyncio.run(
        service.spawn_child(
            _orchestration_request(),
            parent_session_id="parent-session",
            body=body,
        )
    )

    assert outcome.status == "queued"
    assert outcome.child_session_id is not None
    assert outcome.error_code is None
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?", (outcome.delegation_id,)
    ).fetchone()
    assert delegation is not None
    assert delegation["status"] == "queued"
    assert delegation["initiator"] == "user"
    assert delegation["title"] == "Implement OAuth validation"
    assert delegation["context"] == "Callback routes live in auth_routes.py."
    assert delegation["requested_thinking"] == "high"
    assert delegation["error_code"] is None
    assert delegation["child_session_id"] == outcome.child_session_id
    assert delegation["child_workspace_id"] is not None
    assert db.execute("SELECT COUNT(*) AS n FROM prompt_runs").fetchone()["n"] == 0


def test_same_key_replay_after_attachment_returns_attached_child(db, git_repo) -> None:
    """One replayed key returns the attached child without provisioning twice."""
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    body = ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()),
        title="Implement OAuth validation",
        task="Implement state validation.",
        start_immediately=False,
    )

    first = asyncio.run(
        service.spawn_child(
            _orchestration_request(),
            parent_session_id="parent-session",
            body=body,
        )
    )
    second = asyncio.run(
        service.spawn_child(
            _orchestration_request(),
            parent_session_id="parent-session",
            body=body,
        )
    )

    assert second == first
    assert first.status == "queued"
    assert first.child_session_id is not None
    assert db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"] == 1
    assert db.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 2
    assert db.execute("SELECT COUNT(*) AS n FROM workspaces").fetchone()["n"] == 2


def test_concurrent_final_slot_spawns_allow_exactly_one(db, git_repo, monkeypatch) -> None:
    """Two concurrent last-slot spawns serialize into exactly one reservation."""
    from yinshi.config import get_settings
    from yinshi.services.thread_orchestration import (
        ThreadChildLimitError,
        ThreadOrchestrationService,
    )

    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("THREAD_MAX_DIRECT_CHILDREN", "1")
    get_settings.cache_clear()
    service = ThreadOrchestrationService()
    request = _orchestration_request()

    async def spawn_with_key(key: str):
        # Each call opens its own database connection under the hood.
        return await service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=key,
                title=f"child {key}",
                task="task",
                start_immediately=False,
            ),
        )

    async def gather_both():
        results = await asyncio.gather(
            spawn_with_key(str(uuid.uuid4())),
            spawn_with_key(str(uuid.uuid4())),
            return_exceptions=True,
        )
        return results

    outcomes = asyncio.run(gather_both())

    succeeded = [item for item in outcomes if isinstance(item, ThreadSpawnOutcome)]
    limit_errors = [item for item in outcomes if isinstance(item, ThreadChildLimitError)]
    assert len(succeeded) == 1
    assert len(limit_errors) == 1
    assert succeeded[0].status == "queued"
    assert db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"] == 1


def test_spawn_fails_closed_on_parentage_cycle(db, git_repo) -> None:
    """A parentage cycle in the ancestry fails the spawn closed."""
    from yinshi.services.thread_orchestration import ThreadOrchestrationService
    from yinshi.services.thread_queries import ThreadCycleError

    seed_parent_stack(db, git_repo)
    db.execute("""INSERT INTO workspaces (id, repo_id, name, branch, path, state, kind,
                                   parent_workspace_id)
           VALUES ('a-ws', 'repo1', 'a', 'a', '/tmp/a-ws', 'ready', 'delegated',
                   'parent-ws'),
                  ('b-ws', 'repo1', 'b', 'b', '/tmp/b-ws', 'ready', 'delegated',
                   'parent-ws')""")
    db.execute("""INSERT INTO sessions (id, workspace_id) VALUES
           ('cycle-a', 'a-ws'), ('cycle-b', 'b-ws')""")
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES
           ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'cycle-a', 'cycle-b',
            'key-a', 'user', 'A', 'task', 'model-x', 'completed'),
           ('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'cycle-b', 'cycle-a',
            'key-b', 'user', 'B', 'task', 'model-x', 'completed')""")
    db.commit()
    service = ThreadOrchestrationService()

    with pytest.raises(ThreadCycleError):
        asyncio.run(
            service.spawn_child(
                _orchestration_request(),
                parent_session_id="cycle-a",
                body=ThreadChildCreate(
                    idempotency_key=str(uuid.uuid4()),
                    title="cyclic",
                    task="task",
                    start_immediately=False,
                ),
            )
        )


def test_spawn_enforces_total_tree_limit(db, git_repo, monkeypatch) -> None:
    """A root tree at its total-thread maximum cannot reserve another."""
    from yinshi.config import get_settings
    from yinshi.services.thread_orchestration import (
        ThreadOrchestrationService,
        ThreadTreeLimitError,
    )

    seed_parent_stack(db, git_repo)
    db.execute("""INSERT INTO workspaces (id, repo_id, name, branch, path, state, kind,
                                   parent_workspace_id)
           VALUES ('child-ws', 'repo1', 'child', 'child-branch', '/tmp/child-ws',
                   'ready', 'delegated', 'parent-ws')""")
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('child-session', 'child-ws')")
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'parent-session',
               'child-session', 'key-1', 'user', 'Child', 'task', 'model-x',
               'completed'
           )""")
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, idempotency_key, initiator,
               title, task, requested_model, status
           ) SELECT
               'c' || substr('0000000000000000000000000000000' || rowid, -31),
               'child-session', 'gkey-' || rowid, 'user', 'Grandchild', 'task',
               'model-x', 'completed'
           FROM (SELECT 1 AS rowid UNION SELECT 2 UNION SELECT 3 UNION SELECT 4
                 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7)""")
    db.commit()
    monkeypatch.setenv("THREAD_MAX_DIRECT_CHILDREN", "8")
    monkeypatch.setenv("THREAD_MAX_TOTAL", "9")
    get_settings.cache_clear()
    service = ThreadOrchestrationService()

    with pytest.raises(ThreadTreeLimitError):
        asyncio.run(
            service.spawn_child(
                _orchestration_request(),
                parent_session_id="parent-session",
                body=ThreadChildCreate(
                    idempotency_key=str(uuid.uuid4()),
                    title="one tree member too many",
                    task="task",
                    start_immediately=False,
                ),
            )
        )
    assert db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"] == 8


def test_spawn_enforces_active_descendant_limit(db, git_repo, monkeypatch) -> None:
    """A root tree at its active-descendant maximum cannot reserve another."""
    from yinshi.config import get_settings
    from yinshi.services.thread_orchestration import (
        ThreadActiveDescendantsLimitError,
        ThreadOrchestrationService,
    )

    seed_parent_stack(db, git_repo)
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, idempotency_key, initiator,
               title, task, requested_model, status
           ) VALUES (
               'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'parent-session', 'key-1',
               'user', 'Active one', 'task', 'model-x', 'provisioning'
           ),
           ('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'parent-session', 'key-2',
            'user', 'Done one', 'task', 'model-x', 'completed')""")
    db.commit()
    monkeypatch.setenv("THREAD_MAX_DIRECT_CHILDREN", "8")
    monkeypatch.setenv("THREAD_MAX_ACTIVE_DESCENDANTS", "1")
    get_settings.cache_clear()
    service = ThreadOrchestrationService()

    with pytest.raises(ThreadActiveDescendantsLimitError):
        asyncio.run(
            service.spawn_child(
                _orchestration_request(),
                parent_session_id="parent-session",
                body=ThreadChildCreate(
                    idempotency_key=str(uuid.uuid4()),
                    title="one active too many",
                    task="task",
                    start_immediately=False,
                ),
            )
        )
    assert db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"] == 2


def test_spawn_enforces_direct_child_limit(db, git_repo, monkeypatch) -> None:
    """A parent at its direct-child maximum cannot reserve another child."""
    from yinshi.config import get_settings
    from yinshi.services.thread_orchestration import (
        ThreadChildLimitError,
        ThreadOrchestrationService,
    )

    seed_parent_stack(db, git_repo)
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, idempotency_key, initiator,
               title, task, requested_model, status
           ) VALUES (
               'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'parent-session', 'key-1',
               'user', 'Existing', 'task', 'model-x', 'provisioning'
           )""")
    db.commit()
    monkeypatch.setenv("THREAD_MAX_DIRECT_CHILDREN", "1")
    get_settings.cache_clear()
    service = ThreadOrchestrationService()

    with pytest.raises(ThreadChildLimitError):
        asyncio.run(
            service.spawn_child(
                _orchestration_request(),
                parent_session_id="parent-session",
                body=ThreadChildCreate(
                    idempotency_key=str(uuid.uuid4()),
                    title="one too many",
                    task="task",
                    start_immediately=False,
                ),
            )
        )
    assert db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"] == 1


def test_spawn_enforces_max_depth(db, git_repo) -> None:
    """A child beyond the configured maximum depth is rejected atomically."""
    from yinshi.services.thread_orchestration import (
        ThreadDepthLimitError,
        ThreadOrchestrationService,
    )

    seed_parent_stack(db, git_repo)
    db.execute("""INSERT INTO workspaces (id, repo_id, name, branch, path, state, kind,
                                   parent_workspace_id)
           VALUES ('child-ws', 'repo1', 'child', 'child-branch', '/tmp/child-ws',
                   'ready', 'delegated', 'parent-ws')""")
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('child-session', 'child-ws')")
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'parent-session',
               'child-session', 'key-1', 'user', 'Child', 'task', 'model-x',
               'queued'
           )""")
    db.commit()
    service = ThreadOrchestrationService()

    with pytest.raises(ThreadDepthLimitError):
        asyncio.run(
            service.spawn_child(
                _orchestration_request(),
                parent_session_id="child-session",
                body=ThreadChildCreate(
                    idempotency_key=str(uuid.uuid4()),
                    title="too deep",
                    task="task",
                    start_immediately=False,
                ),
            )
        )
    assert db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"] == 1


def test_spawn_uses_tenant_database_isolation_for_authorization(
    db,
    db_path,
    git_repo,
    tmp_path,
    monkeypatch,
) -> None:
    """Tenant mode authorizes by tenant database membership, not repo owner."""
    import sqlite3 as sqlite3_module
    from contextlib import contextmanager

    import yinshi.api.deps as deps_module
    from yinshi.services.thread_orchestration import ThreadOrchestrationService
    from yinshi.tenant import TenantContext

    seed_parent_stack(db, git_repo)
    # A foreign repo owner must not matter in tenant mode.
    db.execute("UPDATE repos SET owner_email = 'someone-else@example.com' WHERE id = 'repo1'")
    db.commit()

    @contextmanager
    def fake_user_db(tenant):
        conn = sqlite3_module.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3_module.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(deps_module, "get_user_db", fake_user_db)
    request = _orchestration_request()
    request.state.tenant = TenantContext(
        user_id="tenant-user",
        email="tenant@example.com",
        data_dir=str(tmp_path),
        db_path=db_path,
    )
    service = ThreadOrchestrationService()

    outcome = asyncio.run(
        service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="t",
                task="task",
                start_immediately=False,
            ),
        )
    )

    assert outcome.status == "queued"


def test_spawn_allows_legacy_parent_owned_by_requesting_user(db, git_repo) -> None:
    """A legacy parent owned by the requesting user reserves normally."""
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    db.execute("UPDATE repos SET owner_email = 'owner@example.com' WHERE id = 'repo1'")
    db.commit()
    request = _orchestration_request()
    request.state.user_email = "owner@example.com"
    service = ThreadOrchestrationService()

    outcome = asyncio.run(
        service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="t",
                task="task",
                start_immediately=False,
            ),
        )
    )

    assert outcome.status == "queued"


def test_spawn_rejects_legacy_parent_owned_by_another_user(db, git_repo) -> None:
    """A legacy parent owned by another account is never reservable."""
    from yinshi.services.thread_orchestration import (
        ThreadOrchestrationService,
        ThreadParentNotAuthorizedError,
    )

    seed_parent_stack(db, git_repo)
    db.execute("UPDATE repos SET owner_email = 'owner@example.com' WHERE id = 'repo1'")
    db.commit()
    request = _orchestration_request()
    request.state.user_email = "other@example.com"
    service = ThreadOrchestrationService()

    with pytest.raises(ThreadParentNotAuthorizedError):
        asyncio.run(
            service.spawn_child(
                request,
                parent_session_id="parent-session",
                body=ThreadChildCreate(
                    idempotency_key=str(uuid.uuid4()),
                    title="t",
                    task="task",
                    start_immediately=False,
                ),
            )
        )
    assert db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"] == 0


def test_spawn_requires_enabled_hierarchy(db, git_repo, monkeypatch) -> None:
    """A disabled hierarchy flag fails the spawn closed before any write."""
    from yinshi.config import get_settings
    from yinshi.services.thread_orchestration import (
        ThreadHierarchyDisabledError,
        ThreadOrchestrationService,
    )

    seed_parent_stack(db, git_repo)
    monkeypatch.setenv("THREAD_HIERARCHY_ENABLED", "false")
    get_settings.cache_clear()
    service = ThreadOrchestrationService()

    with pytest.raises(ThreadHierarchyDisabledError):
        asyncio.run(
            service.spawn_child(
                _orchestration_request(),
                parent_session_id="parent-session",
                body=ThreadChildCreate(
                    idempotency_key=str(uuid.uuid4()),
                    title="t",
                    task="task",
                    start_immediately=False,
                ),
            )
        )
    assert db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"] == 0


def test_duplicate_spawn_with_mismatched_request_conflicts(db, git_repo) -> None:
    """One key reuse with a different normalized request is rejected."""
    from yinshi.services.thread_orchestration import (
        ThreadIdempotencyConflictError,
        ThreadOrchestrationService,
    )

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    key = str(uuid.uuid4())

    asyncio.run(
        service.spawn_child(
            _orchestration_request(),
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=key,
                title="Same title",
                task="First task",
                start_immediately=False,
            ),
        )
    )
    with pytest.raises(ThreadIdempotencyConflictError):
        asyncio.run(
            service.spawn_child(
                _orchestration_request(),
                parent_session_id="parent-session",
                body=ThreadChildCreate(
                    idempotency_key=key,
                    title="Same title",
                    task="Different task",
                    start_immediately=False,
                ),
            )
        )
    assert db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"] == 1


def test_spawn_child_attaches_delegated_child(db, git_repo) -> None:
    """One successful reservation becomes workspace, session, and queued state."""
    from tests.test_thread_workspaces import run_git
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()

    outcome = asyncio.run(
        service.spawn_child(
            _orchestration_request(),
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Implement OAuth validation",
                task="Implement state validation.",
                start_immediately=False,
            ),
        )
    )

    assert outcome.status == "queued"
    assert outcome.child_session_id is not None
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?", (outcome.delegation_id,)
    ).fetchone()
    assert delegation["status"] == "queued"
    assert delegation["child_session_id"] == outcome.child_session_id
    assert delegation["child_workspace_id"] is not None
    assert delegation["base_kind"] == "head"
    assert delegation["snapshot_ref"] is None
    child_workspace = db.execute(
        "SELECT * FROM workspaces WHERE id = ?",
        (delegation["child_workspace_id"],),
    ).fetchone()
    assert child_workspace["kind"] == "delegated"
    assert child_workspace["parent_workspace_id"] == "parent-ws"
    child_branch = f"yinshi/thread-{outcome.delegation_id[:8]}"
    assert child_workspace["branch"] == child_branch
    child_session = db.execute(
        "SELECT * FROM sessions WHERE id = ?", (outcome.child_session_id,)
    ).fetchone()
    assert child_session is not None
    assert child_session["workspace_id"] == delegation["child_workspace_id"]
    branch_output = run_git(
        "for-each-ref", "--format=%(refname)", f"refs/heads/{child_branch}", cwd=git_repo
    )
    assert child_branch in branch_output
    worktrees = run_git("worktree", "list", "--porcelain", cwd=git_repo)
    assert child_workspace["path"] in worktrees


def test_spawn_child_keeps_database_closed_during_git_work(db, git_repo, monkeypatch) -> None:
    """No request database connection stays open while Git artifacts are created."""
    from contextlib import contextmanager

    import yinshi.api.deps as deps_module
    from yinshi.services import thread_workspaces as workspaces_module
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    real_get_db = deps_module.get_db
    open_connections = {"count": 0}
    observed = {"during_git": None, "after_git": None}

    @contextmanager
    def counting_get_db():
        open_connections["count"] += 1
        try:
            with real_get_db() as conn:
                yield conn
        finally:
            open_connections["count"] -= 1

    monkeypatch.setattr(deps_module, "get_db", counting_get_db)
    real_create = workspaces_module.ThreadWorkspaceService.create_child_git_artifacts

    async def observing(self, context, **kwargs):
        observed["during_git"] = open_connections["count"]
        staged = await real_create(self, context, **kwargs)
        observed["after_git"] = open_connections["count"]
        return staged

    monkeypatch.setattr(
        workspaces_module.ThreadWorkspaceService,
        "create_child_git_artifacts",
        observing,
    )
    service = ThreadOrchestrationService()

    outcome = asyncio.run(
        service.spawn_child(
            _orchestration_request(),
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="t",
                task="task",
                start_immediately=False,
            ),
        )
    )

    assert outcome.status == "queued"
    assert observed == {"during_git": 0, "after_git": 0}


def test_thread_spawn_out_model_shape() -> None:
    """ThreadSpawnOut exposes the stable child identity fields only."""
    from pydantic import ValidationError

    from yinshi.models import ThreadSpawnOut

    out = ThreadSpawnOut(delegation_id="dele", status="queued", child_session_id="sess")
    assert out.model_dump() == {
        "delegation_id": "dele",
        "status": "queued",
        "child_session_id": "sess",
        "error_code": None,
    }
    with pytest.raises(ValidationError):
        ThreadSpawnOut(delegation_id="dele", status="not-a-status")


def test_thread_child_create_normalizes_and_rejects_thinking() -> None:
    """Thinking normalizes to canonical levels and rejects unknown values."""
    from pydantic import ValidationError

    body = ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()),
        title="t",
        task="task",
        thinking=" High ",
    )
    assert body.thinking == "high"
    with pytest.raises(ValidationError):
        ThreadChildCreate(
            idempotency_key=str(uuid.uuid4()),
            title="t",
            task="task",
            thinking="ultra",
        )


def test_spawn_replay_does_not_schedule_prompt_twice(db, git_repo) -> None:
    """One replayed started spawn never starts a second prompt run."""
    from yinshi.services.prompt_journal import PromptJournal, PromptRun
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    class RecordingJournal(PromptJournal):
        def __init__(self) -> None:
            self.starts: list[dict[str, object]] = []

        async def start(self, **kwargs) -> PromptRun:
            self.starts.append(kwargs)
            return PromptRun(
                id="cccccccccccccccccccccccccccccccc",
                session_id=str(kwargs["session_id"]),
                status="starting",
            )

    seed_parent_stack(db, git_repo)
    journal = RecordingJournal()
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    request.app.state.prompt_journal = journal
    body = ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()),
        title="t",
        task="task",
        start_immediately=True,
    )

    first = asyncio.run(service.spawn_child(request, parent_session_id="parent-session", body=body))
    second = asyncio.run(
        service.spawn_child(_orchestration_request(), parent_session_id="parent-session", body=body)
    )

    assert len(journal.starts) == 1
    assert second == first
    assert first.status == "running"


def test_spawn_start_failure_marks_failed_and_keeps_workspace(db, git_repo) -> None:
    """One rejected start fails the delegation and preserves the attached child."""
    from yinshi.services.prompt_journal import PromptJournal, PromptRun
    from yinshi.services.thread_orchestration import (
        ThreadOrchestrationService,
        ThreadPromptStartError,
    )

    class FailingJournal(PromptJournal):
        def __init__(self) -> None:
            self.starts: list[dict[str, object]] = []

        async def start(self, **kwargs) -> PromptRun:
            self.starts.append(kwargs)
            raise RuntimeError("boom")

    seed_parent_stack(db, git_repo)
    journal = FailingJournal()
    service = ThreadOrchestrationService()
    request = _orchestration_request()
    request.app.state.prompt_journal = journal
    key = str(uuid.uuid4())
    body = ThreadChildCreate(
        idempotency_key=key,
        title="t",
        task="task",
        start_immediately=True,
    )

    with pytest.raises(ThreadPromptStartError):
        asyncio.run(service.spawn_child(request, parent_session_id="parent-session", body=body))

    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE idempotency_key = ?", (key,)
    ).fetchone()
    assert delegation is not None
    assert delegation["status"] == "failed"
    assert delegation["error_code"] == "start_failed"
    assert delegation["error_detail_safe"]
    assert delegation["child_session_id"] is not None
    assert delegation["child_workspace_id"] is not None
    assert (
        db.execute("SELECT COUNT(*) AS n FROM workspaces WHERE kind = 'delegated'").fetchone()["n"]
        == 1
    )
    assert len(journal.starts) == 1

    replay = asyncio.run(
        service.spawn_child(_orchestration_request(), parent_session_id="parent-session", body=body)
    )
    assert replay.status == "failed"
    assert replay.error_code == "start_failed"
    assert replay.child_session_id == delegation["child_session_id"]
    assert len(journal.starts) == 1


def test_spawn_without_start_keeps_queued_and_skips_prompt(db, git_repo) -> None:
    """start_immediately=False leaves the child queued and never calls the journal."""
    from yinshi.services.prompt_journal import PromptJournal, PromptRun
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    class RecordingJournal(PromptJournal):
        def __init__(self) -> None:
            self.starts: list[dict[str, object]] = []

        async def start(self, **kwargs) -> PromptRun:
            self.starts.append(kwargs)
            return PromptRun(
                id="dddddddddddddddddddddddddddddddd",
                session_id=str(kwargs["session_id"]),
                status="starting",
            )

    seed_parent_stack(db, git_repo)
    journal = RecordingJournal()
    request = _orchestration_request()
    request.app.state.prompt_journal = journal
    service = ThreadOrchestrationService()

    outcome = asyncio.run(
        service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="t",
                task="task",
                start_immediately=False,
            ),
        )
    )

    assert outcome.status == "queued"
    assert outcome.error_code is None
    assert journal.starts == []
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?", (outcome.delegation_id,)
    ).fetchone()
    assert delegation["status"] == "queued"
    assert delegation["started_at"] is None


def test_spawn_child_marks_failed_reservation_when_git_fails(db, git_repo, tmp_path) -> None:
    """A Git failure marks the reservation failed with one safe error code."""
    from tests.test_thread_workspaces import run_git
    from yinshi.exceptions import GitError
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    db.execute(
        "UPDATE workspaces SET path = ? WHERE id = 'parent-ws'",
        (str(not_a_repo),),
    )
    db.commit()
    service = ThreadOrchestrationService()
    key = str(uuid.uuid4())

    with pytest.raises(GitError):
        asyncio.run(
            service.spawn_child(
                _orchestration_request(),
                parent_session_id="parent-session",
                body=ThreadChildCreate(
                    idempotency_key=key,
                    title="t",
                    task="task",
                    start_immediately=False,
                ),
            )
        )

    delegation = db.execute(
        """SELECT * FROM thread_delegations
           WHERE parent_session_id = 'parent-session' AND idempotency_key = ?""",
        (key,),
    ).fetchone()
    assert delegation is not None
    assert delegation["status"] == "failed"
    assert delegation["error_code"] == "provision_failed"
    assert delegation["child_session_id"] is None
    assert delegation["child_workspace_id"] is None
    assert (
        db.execute("SELECT COUNT(*) AS n FROM workspaces WHERE kind = 'delegated'").fetchone()["n"]
        == 0
    )
    child_branch = f"yinshi/thread-{delegation['id'][:8]}"
    assert (
        run_git("for-each-ref", "--format=%(refname)", f"refs/heads/{child_branch}", cwd=git_repo)
        == ""
    )
    assert child_branch not in run_git("worktree", "list", "--porcelain", cwd=git_repo)


def test_spawn_child_replays_failed_reservation_stably(db, git_repo, tmp_path) -> None:
    """One replayed failed reservation returns its failed outcome without retry."""
    from tests.test_thread_workspaces import run_git
    from yinshi.exceptions import GitError
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    db.execute(
        "UPDATE workspaces SET path = ? WHERE id = 'parent-ws'",
        (str(not_a_repo),),
    )
    db.commit()
    service = ThreadOrchestrationService()
    key = str(uuid.uuid4())
    body = ThreadChildCreate(
        idempotency_key=key,
        title="t",
        task="task",
        start_immediately=False,
    )

    with pytest.raises(GitError):
        asyncio.run(
            service.spawn_child(
                _orchestration_request(),
                parent_session_id="parent-session",
                body=body,
            )
        )
    replay = asyncio.run(
        service.spawn_child(
            _orchestration_request(),
            parent_session_id="parent-session",
            body=body,
        )
    )

    assert replay.status == "failed"
    assert replay.child_session_id is None
    assert replay.error_code == "provision_failed"
    assert db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"] == 1
    assert (
        db.execute("SELECT COUNT(*) AS n FROM workspaces WHERE kind = 'delegated'").fetchone()["n"]
        == 0
    )
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?", (replay.delegation_id,)
    ).fetchone()
    assert delegation is not None
    assert delegation["status"] == "failed"
    assert delegation["error_detail_safe"]
    child_branch = f"yinshi/thread-{replay.delegation_id[:8]}"
    assert (
        run_git("for-each-ref", "--format=%(refname)", f"refs/heads/{child_branch}", cwd=git_repo)
        == ""
    )


def test_spawn_child_attach_failure_rolls_back_and_cleans_artifacts(db, git_repo) -> None:
    """One attach failure rolls back rows, cleans Git artifacts, and fails."""
    import sqlite3 as sqlite3_module
    from pathlib import Path

    from tests.test_thread_workspaces import run_git
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    # A dirty parent forces a published snapshot ref that cleanup must remove.
    Path(git_repo, "dirty.txt").write_text("dirty", encoding="utf-8")
    db.execute(
        "CREATE TRIGGER fail_child_attach BEFORE INSERT ON sessions "
        "BEGIN SELECT RAISE(ABORT, 'simulated attach failure'); END",
    )
    db.commit()
    service = ThreadOrchestrationService()
    key = str(uuid.uuid4())

    with pytest.raises(sqlite3_module.IntegrityError):
        asyncio.run(
            service.spawn_child(
                _orchestration_request(),
                parent_session_id="parent-session",
                body=ThreadChildCreate(
                    idempotency_key=key,
                    title="t",
                    task="task",
                    start_immediately=False,
                ),
            )
        )

    delegation = db.execute(
        """SELECT * FROM thread_delegations
           WHERE parent_session_id = 'parent-session' AND idempotency_key = ?""",
        (key,),
    ).fetchone()
    assert delegation is not None
    assert delegation["status"] == "failed"
    assert delegation["error_code"] == "provision_failed"
    assert delegation["child_session_id"] is None
    assert delegation["child_workspace_id"] is None
    assert delegation["snapshot_ref"] == f"refs/yinshi/snapshots/{delegation['id']}"
    assert delegation["base_kind"] == "snapshot"
    assert len(delegation["base_commit"]) in {40, 64}
    assert delegation["git_artifacts_claimed"] == 0
    assert (
        db.execute("SELECT COUNT(*) AS n FROM workspaces WHERE kind = 'delegated'").fetchone()["n"]
        == 0
    )
    child_branch = f"yinshi/thread-{delegation['id'][:8]}"
    assert (
        run_git("for-each-ref", "--format=%(refname)", f"refs/heads/{child_branch}", cwd=git_repo)
        == ""
    )
    assert child_branch not in run_git("worktree", "list", "--porcelain", cwd=git_repo)
    assert (
        run_git(
            "for-each-ref",
            "--format=%(refname)",
            f"refs/yinshi/snapshots/{delegation['id']}",
            cwd=git_repo,
        )
        == ""
    )


def test_spawn_child_lost_cas_keeps_winner_status_and_cleans_artifacts(
    db,
    db_path,
    git_repo,
    monkeypatch,
) -> None:
    """A lost attach CAS rolls back, cleans artifacts, and never overwrites."""
    import sqlite3 as sqlite3_module

    import yinshi.services.thread_workspaces as workspaces_module
    from tests.test_thread_workspaces import run_git
    from yinshi.services.thread_orchestration import (
        ThreadAttachConflictError,
        ThreadOrchestrationService,
    )

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    key = str(uuid.uuid4())
    real_create = workspaces_module.ThreadWorkspaceService.create_child_git_artifacts

    async def racing(self, context, **kwargs):
        # Simulate one concurrent writer claiming the reservation between the
        # staging and the attach update, like a cancellation worker would.
        conn = sqlite3_module.connect(db_path, check_same_thread=False)
        try:
            conn.execute(
                "UPDATE thread_delegations SET status = 'cancelled' WHERE idempotency_key = ?",
                (key,),
            )
            conn.commit()
        finally:
            conn.close()
        return await real_create(self, context, **kwargs)

    monkeypatch.setattr(
        workspaces_module.ThreadWorkspaceService,
        "create_child_git_artifacts",
        racing,
    )

    with pytest.raises(ThreadAttachConflictError):
        asyncio.run(
            service.spawn_child(
                _orchestration_request(),
                parent_session_id="parent-session",
                body=ThreadChildCreate(
                    idempotency_key=key,
                    title="t",
                    task="task",
                    start_immediately=False,
                ),
            )
        )

    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE idempotency_key = ?", (key,)
    ).fetchone()
    assert delegation is not None
    assert delegation["status"] == "cancelled"
    assert delegation["error_code"] is None
    assert delegation["child_session_id"] is None
    assert (
        db.execute("SELECT COUNT(*) AS n FROM workspaces WHERE kind = 'delegated'").fetchone()["n"]
        == 0
    )
    child_branch = f"yinshi/thread-{delegation['id'][:8]}"
    assert (
        run_git("for-each-ref", "--format=%(refname)", f"refs/heads/{child_branch}", cwd=git_repo)
        == ""
    )
    assert child_branch not in run_git("worktree", "list", "--porcelain", cwd=git_repo)


def test_spawn_start_immediately_schedules_one_child_prompt(db, git_repo) -> None:
    """A started spawn schedules one child prompt and returns running."""
    from yinshi.services.prompt_journal import PromptJournal, PromptRun
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    class RecordingJournal(PromptJournal):
        def __init__(self) -> None:
            self.starts: list[dict[str, object]] = []

        async def start(self, **kwargs) -> PromptRun:
            self.starts.append(kwargs)
            return PromptRun(
                id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                session_id=str(kwargs["session_id"]),
                status="starting",
            )

    seed_parent_stack(db, git_repo)
    journal = RecordingJournal()
    request = _orchestration_request()
    request.app.state.prompt_journal = journal
    body = ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()),
        title="Implement OAuth validation",
        task="Implement state validation.",
        context="Callback routes live in auth_routes.py.",
        role="implementation",
        model="model-x",
        thinking="high",
        start_immediately=True,
    )

    outcome = asyncio.run(
        ThreadOrchestrationService().spawn_child(
            request,
            parent_session_id="parent-session",
            body=body,
        )
    )

    assert outcome.status == "running"
    assert outcome.child_session_id is not None
    assert len(journal.starts) == 1
    start = journal.starts[0]
    assert start["session_id"] == outcome.child_session_id
    assert "# Implement OAuth validation" in start["body"].prompt
    assert "## Task\nImplement state validation." in start["body"].prompt
    assert "## Context\nCallback routes live in auth_routes.py." in start["body"].prompt


def test_spawn_start_losing_running_cas_cancels_accepted_run(db, git_repo) -> None:
    """A cancel winning before the running CAS compensates the accepted run."""
    from yinshi.services.prompt_journal import PromptJournal, PromptRun
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    race_run_id = "cccccccccccccccccccccccccccccccc"

    class RacyCancelJournal(PromptJournal):
        """Accepted start loses the running CAS to a concurrent cancel."""

        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, str]] = []

        async def start(self, **kwargs) -> PromptRun:
            session_id = str(kwargs["session_id"])
            db.execute(
                """INSERT INTO prompt_runs (id, session_id, idempotency_key, status)
                   VALUES (?, ?, ?, 'starting')""",
                (race_run_id, session_id, str(kwargs["idempotency_key"])),
            )
            db.execute(
                """UPDATE thread_delegations
                   SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE child_session_id = ? AND status = 'queued'""",
                (session_id,),
            )
            db.commit()
            return PromptRun(id=race_run_id, session_id=session_id, status="starting")

        async def cancel(self, *, request, session_id, run_id) -> PromptRun:
            self.cancel_calls.append({"session_id": str(session_id), "run_id": str(run_id)})
            db.execute(
                "UPDATE prompt_runs SET status = 'cancelled' WHERE id = ?",
                (str(run_id),),
            )
            db.commit()
            return PromptRun(id=str(run_id), session_id=str(session_id), status="cancelled")

    seed_parent_stack(db, git_repo)
    journal = RacyCancelJournal()
    request = _orchestration_request()
    request.app.state.prompt_journal = journal

    outcome = asyncio.run(
        ThreadOrchestrationService().spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Racing child",
                task="Race the running CAS.",
                start_immediately=True,
            ),
        )
    )

    assert outcome.status == "cancelled"
    assert outcome.child_session_id is not None
    assert journal.cancel_calls == [{"session_id": outcome.child_session_id, "run_id": race_run_id}]
    delegation = db.execute(
        "SELECT status FROM thread_delegations WHERE id = ?",
        (outcome.delegation_id,),
    ).fetchone()
    assert delegation["status"] == "cancelled"
    run = db.execute("SELECT status FROM prompt_runs WHERE id = ?", (race_run_id,)).fetchone()
    assert run["status"] == "cancelled"


def test_spawn_start_race_completed_run_stays_completed(db, git_repo) -> None:
    """Compensation keeps an already completed run in its terminal state."""
    from yinshi.services.prompt_journal import PromptJournal, PromptRun
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    race_run_id = "dddddddddddddddddddddddddddddddd"

    class CompletedBeforeCompensationJournal(PromptJournal):
        """The accepted run completes before the compensation lands."""

        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, str]] = []

        async def start(self, **kwargs) -> PromptRun:
            session_id = str(kwargs["session_id"])
            db.execute(
                """INSERT INTO prompt_runs (id, session_id, idempotency_key, status)
                   VALUES (?, ?, ?, 'completed')""",
                (race_run_id, session_id, str(kwargs["idempotency_key"])),
            )
            db.execute(
                """UPDATE thread_delegations
                   SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE child_session_id = ? AND status = 'queued'""",
                (session_id,),
            )
            db.commit()
            return PromptRun(id=race_run_id, session_id=session_id, status="running")

        async def cancel(self, *, request, session_id, run_id) -> PromptRun:
            self.cancel_calls.append({"session_id": str(session_id), "run_id": str(run_id)})
            return PromptRun(id=str(run_id), session_id=str(session_id), status="completed")

    seed_parent_stack(db, git_repo)
    journal = CompletedBeforeCompensationJournal()
    request = _orchestration_request()
    request.app.state.prompt_journal = journal

    outcome = asyncio.run(
        ThreadOrchestrationService().spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Completed racing child",
                task="Race the running CAS.",
                start_immediately=True,
            ),
        )
    )

    assert outcome.status == "cancelled"
    assert journal.cancel_calls == [{"session_id": outcome.child_session_id, "run_id": race_run_id}]
    run = db.execute("SELECT status FROM prompt_runs WHERE id = ?", (race_run_id,)).fetchone()
    assert run["status"] == "completed"


def _spawn_queued_child(service, request, title: str) -> ThreadSpawnOutcome:
    """Spawn one queued child through the orchestration service."""
    return asyncio.run(
        service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title=title,
                task="Wait for orchestration.",
                start_immediately=False,
            ),
        )
    )


def test_cancel_queued_child_is_stable_and_preserves_workspace(db, git_repo) -> None:
    """Queued-child cancellation is idempotent and keeps attached resources."""
    from pathlib import Path

    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    spawned = asyncio.run(
        service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title="Queued child",
                task="Wait for manual start.",
                start_immediately=False,
            ),
        )
    )

    first = asyncio.run(service.cancel_child(request, thread_id=spawned.child_session_id))
    second = asyncio.run(service.cancel_child(request, thread_id=spawned.child_session_id))

    assert first.status == "cancelled"
    assert second == first
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    assert delegation["completed_at"] is not None
    workspace = db.execute(
        "SELECT path FROM workspaces WHERE id = ?",
        (delegation["child_workspace_id"],),
    ).fetchone()
    assert workspace is not None
    assert Path(workspace["path"]).is_dir()


class DeadExecutorJournal(PromptJournal):
    """PromptJournal with a dead executor so only cancel logic runs."""

    def __init__(self) -> None:
        async def dead_executor(request, session_id, body):
            raise AssertionError("prompt executor must not run")
            yield

        super().__init__(executor=dead_executor)


def _seed_running_child(db, request, service) -> ThreadSpawnOutcome:
    """Spawn one queued child and force it into the running state."""
    spawned = _spawn_queued_child(service, request, "Running child")
    db.execute(
        """UPDATE thread_delegations SET status = 'running',
               started_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (spawned.delegation_id,),
    )
    db.execute(
        """INSERT INTO prompt_runs (id, session_id, idempotency_key, status)
           VALUES ('c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0', ?, ?, 'running')""",
        (
            spawned.child_session_id,
            initial_run_idempotency_key(spawned.delegation_id),
        ),
    )
    db.commit()
    return spawned


def test_cancel_running_child_adopts_durable_cancelled_run(db, git_repo) -> None:
    """Running cancellation CASes to cancelling, stops the run, adopts cancelled."""
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    journal = DeadExecutorJournal()
    # The live flow started the run through this journal instance, so its
    # database recovery pass already happened before the run went active.
    journal._recovered_database_paths.add(journal._database_path(request))
    request.app.state.prompt_journal = journal
    service = ThreadOrchestrationService()
    spawned = _seed_running_child(db, request, service)

    outcome = asyncio.run(service.cancel_child(request, thread_id=spawned.child_session_id))

    assert outcome.status == "cancelled"
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    assert delegation["status"] == "cancelled"
    assert delegation["completed_at"] is not None
    run = db.execute(
        "SELECT status FROM prompt_runs WHERE id = 'c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0'"
    ).fetchone()
    assert run is not None
    assert run["status"] == "cancelled"


def test_cancel_running_child_losing_to_completed_preserves_completed(db, git_repo) -> None:
    """A run that completed first keeps its completed delegation outcome."""
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    journal = DeadExecutorJournal()
    journal._recovered_database_paths.add(journal._database_path(request))
    request.app.state.prompt_journal = journal
    service = ThreadOrchestrationService()
    spawned = _seed_running_child(db, request, service)
    db.execute("""UPDATE prompt_runs SET status = 'completed'
           WHERE id = 'c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0'""")
    db.commit()

    outcome = asyncio.run(service.cancel_child(request, thread_id=spawned.child_session_id))

    assert outcome.status == "completed"
    delegation = db.execute(
        "SELECT status FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    assert delegation["status"] == "completed"


def test_cancel_running_child_after_restart_adopts_interrupted(db, git_repo) -> None:
    """A running delegation without a live task adopts durable interrupted state."""
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    # A fresh journal instance models a process restart: its first recovery
    # pass resolves previously active runs from durable event state.
    request.app.state.prompt_journal = DeadExecutorJournal()
    service = ThreadOrchestrationService()
    spawned = _seed_running_child(db, request, service)

    outcome = asyncio.run(service.cancel_child(request, thread_id=spawned.child_session_id))

    assert outcome.status == "interrupted"
    delegation = db.execute(
        "SELECT status FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    assert delegation["status"] == "interrupted"


def test_cancel_repeating_on_cancelling_converges_to_durable_state(db, git_repo) -> None:
    """A cancelling delegation repeats until the durable run outcome wins."""
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    journal = DeadExecutorJournal()
    journal._recovered_database_paths.add(journal._database_path(request))
    request.app.state.prompt_journal = journal
    service = ThreadOrchestrationService()
    spawned = _seed_running_child(db, request, service)
    db.execute(
        """UPDATE thread_delegations SET status = 'cancelling'
           WHERE id = ?""",
        (spawned.delegation_id,),
    )
    db.commit()

    outcome = asyncio.run(service.cancel_child(request, thread_id=spawned.child_session_id))

    assert outcome.status == "cancelled"
    assert outcome == asyncio.run(service.cancel_child(request, thread_id=spawned.child_session_id))


def test_cancel_unknown_thread_maps_to_not_found(db, git_repo) -> None:
    """Cancelling an unknown session or delegation maps to not-found."""
    from yinshi.services.thread_orchestration import (
        ThreadNotFoundError,
        ThreadOrchestrationService,
    )

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    with pytest.raises(ThreadNotFoundError):
        asyncio.run(service.cancel_child(request, thread_id="missing-thread"))
    with pytest.raises(ThreadNotFoundError):
        asyncio.run(service.cancel_child(request, thread_id="parent-session"))


def test_cancel_terminal_child_repeats_stably(db, git_repo) -> None:
    """Cancelling a completed child preserves the completed stored decision."""
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    spawned = _spawn_queued_child(service, request, "Completed child")
    db.execute(
        """UPDATE thread_delegations SET status = 'completed',
               completed_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (spawned.delegation_id,),
    )
    db.commit()

    first = asyncio.run(service.cancel_child(request, thread_id=spawned.child_session_id))
    second = asyncio.run(service.cancel_child(request, thread_id=spawned.child_session_id))

    assert first.status == "completed"
    assert second == first
    assert first == ThreadSpawnOutcome.from_row(
        db.execute(
            "SELECT * FROM thread_delegations WHERE id = ?",
            (spawned.delegation_id,),
        ).fetchone()
    )


def test_cancel_foreign_thread_maps_to_not_found(db, git_repo, monkeypatch) -> None:
    """Foreign children and foreign reservations hide behind not-found."""
    import yinshi.services.thread_orchestration as orchestration_module
    from yinshi.services.thread_orchestration import (
        ThreadOrchestrationService,
        ThreadParentNotAuthorizedError,
    )

    seed_parent_stack(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    spawned = _spawn_queued_child(service, request, "Foreign child")
    db.execute("UPDATE repos SET owner_email = 'owner@example.com' WHERE id = 'repo1'")
    db.commit()
    monkeypatch.setattr(orchestration_module, "get_user_email", lambda req: "other@example.com")

    with pytest.raises(ThreadParentNotAuthorizedError):
        asyncio.run(service.cancel_child(request, thread_id=spawned.child_session_id))
    with pytest.raises(ThreadParentNotAuthorizedError):
        asyncio.run(service.cancel_child(request, thread_id=spawned.delegation_id))
