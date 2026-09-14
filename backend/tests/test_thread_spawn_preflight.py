"""Check agent spawn admission before any durable reservation or Git mutation."""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.test_thread_orchestration import _orchestration_request, seed_parent_stack
from yinshi.config import get_settings
from yinshi.model_catalog import normalize_model_ref
from yinshi.models import ThreadChildCreate
from yinshi.services.orchestration_bridge import VerifiedThreadCaller
from yinshi.services.thread_git_ownership import ThreadGitOwnershipError
from yinshi.services.thread_orchestration import (
    ThreadChildLimitError,
    ThreadHierarchyDisabledError,
    ThreadIdempotencyConflictError,
    ThreadOrchestrationService,
    ThreadRuntimeUnavailableError,
)
from yinshi.services.thread_workspaces import ThreadWorkspaceService


def _enable_agent_delegation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREAD_HIERARCHY_ENABLED", "true")
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "true")
    get_settings.cache_clear()


def _seed_active_caller(
    db: Any, git_repo: str, *, tool_call_id: str = "spawn"
) -> VerifiedThreadCaller:
    seed_parent_stack(db, git_repo)
    run_id = "1" * 32
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) "
        "VALUES (?, 'parent-session', 'origin', 'running')",
        (run_id,),
    )
    db.commit()
    return VerifiedThreadCaller(
        session_id="parent-session",
        run_id=run_id,
        tenant_id=None,
        runtime_id=None,
        tool_call_id=tool_call_id,
        expires_at=time.monotonic() + 60,
        database_path=str(db.execute("PRAGMA database_list").fetchone()[2]),
    )


def _child_body() -> ThreadChildCreate:
    return ThreadChildCreate(
        idempotency_key=str(uuid.uuid4()),
        title="Child",
        task="Inspect",
        start_immediately=False,
    )


async def test_exact_agent_spawn_replay_precedes_flags_runtime_and_git(
    db: Any,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authenticated exact replay returns stored state without another preflight."""
    _enable_agent_delegation(monkeypatch)
    caller = _seed_active_caller(db, git_repo)
    request = _orchestration_request()
    service = ThreadOrchestrationService()
    body = _child_body()

    first = await service.spawn_child(
        request,
        parent_session_id=caller.session_id,
        body=body,
        caller=caller,
        runtime_ready=lambda: True,
    )

    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "false")
    get_settings.cache_clear()

    async def unexpected_preflight(*args: object, **kwargs: object) -> None:
        raise AssertionError("Git preflight ran for an exact replay")

    monkeypatch.setattr(
        ThreadWorkspaceService,
        "preflight_child_admission",
        unexpected_preflight,
    )
    replay = await service.spawn_child(
        request,
        parent_session_id=caller.session_id,
        body=body,
        caller=caller,
        runtime_ready=lambda: False,
    )

    assert replay == first
    assert db.execute("SELECT COUNT(*) FROM thread_delegations").fetchone()[0] == 1


async def test_new_agent_spawn_requires_enabled_flags_before_preflight(
    db: Any,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled delegation flag rejects new work before runtime and Git access."""
    caller = _seed_active_caller(db, git_repo)
    monkeypatch.setenv("THREAD_HIERARCHY_ENABLED", "true")
    monkeypatch.setenv("AGENT_DELEGATION_ENABLED", "false")
    get_settings.cache_clear()
    runtime_calls = 0

    def runtime_ready() -> bool:
        nonlocal runtime_calls
        runtime_calls += 1
        return True

    with pytest.raises(ThreadHierarchyDisabledError):
        await ThreadOrchestrationService().spawn_child(
            _orchestration_request(),
            parent_session_id=caller.session_id,
            body=_child_body(),
            caller=caller,
            runtime_ready=runtime_ready,
        )

    assert runtime_calls == 0
    assert db.execute("SELECT COUNT(*) FROM thread_delegations").fetchone()[0] == 0


async def test_unhealthy_selected_runtime_rejects_before_git_or_reservation(
    db: Any,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unhealthy established runtime produces the existing unavailable result."""
    _enable_agent_delegation(monkeypatch)
    caller = _seed_active_caller(db, git_repo)

    async def unexpected_preflight(*args: object, **kwargs: object) -> None:
        raise AssertionError("Git preflight ran after runtime rejection")

    monkeypatch.setattr(
        ThreadWorkspaceService,
        "preflight_child_admission",
        unexpected_preflight,
        raising=False,
    )
    with pytest.raises(ThreadRuntimeUnavailableError) as error:
        await ThreadOrchestrationService().spawn_child(
            _orchestration_request(),
            parent_session_id=caller.session_id,
            body=_child_body(),
            caller=caller,
            runtime_ready=lambda: False,
        )

    assert error.value.code == "runtime_unavailable"
    assert db.execute("SELECT COUNT(*) FROM thread_delegations").fetchone()[0] == 0


async def test_preflight_runs_without_database_and_limits_are_rechecked(
    db: Any,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External checks run after connection closure and limits remain transactional."""
    from yinshi.api import deps
    from yinshi.services import thread_orchestration

    _enable_agent_delegation(monkeypatch)
    monkeypatch.setenv("THREAD_MAX_DIRECT_CHILDREN", "1")
    get_settings.cache_clear()
    caller = _seed_active_caller(db, git_repo)
    request = _orchestration_request()
    database_active = False
    original_runner = deps.run_db_operation_for_request

    async def tracked_runner(
        request: Any,
        operation: Callable[[Any], Any],
        *,
        shared_request_budget: bool = True,
    ) -> Any:
        def tracked(database: Any) -> Any:
            nonlocal database_active
            assert not database_active
            database_active = True
            try:
                return operation(database)
            finally:
                database_active = False

        return await original_runner(
            request,
            tracked,
            shared_request_budget=shared_request_budget,
        )

    monkeypatch.setattr(thread_orchestration, "run_db_operation_for_request", tracked_runner)
    original_preflight = ThreadWorkspaceService.preflight_child_admission

    def runtime_ready() -> bool:
        assert not database_active
        return True

    async def preflight_then_fill_limit(
        self: ThreadWorkspaceService,
        context: Any,
        *,
        database_identity: str,
    ) -> None:
        assert not database_active
        await original_preflight(self, context, database_identity=database_identity)
        assert not database_active
        db.execute(
            "INSERT INTO thread_delegations "
            "(id, parent_session_id, idempotency_key, initiator, title, task, role, "
            "requested_model, status) VALUES (?, 'parent-session', ?, 'user', 'Existing', "
            "'Existing task', 'general', 'test/model', 'completed')",
            ("e" * 32, str(uuid.uuid4())),
        )
        db.commit()

    monkeypatch.setattr(
        ThreadWorkspaceService,
        "preflight_child_admission",
        preflight_then_fill_limit,
    )

    with pytest.raises(ThreadChildLimitError):
        await ThreadOrchestrationService().spawn_child(
            request,
            parent_session_id=caller.session_id,
            body=_child_body(),
            caller=caller,
            runtime_ready=runtime_ready,
        )

    assert db.execute("SELECT COUNT(*) FROM thread_delegations").fetchone()[0] == 1
    common = (
        await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        )
    ).stdout.strip()
    assert not (Path(common) / ".repository-lifecycle-locks").exists()


async def test_preflight_rejects_a_symlinked_repository_before_reservation(
    db: Any,
    git_repo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Physical path validation rejects a redirected repository root."""
    _enable_agent_delegation(monkeypatch)
    caller = _seed_active_caller(db, git_repo)
    linked_repo = tmp_path / "linked-repo"
    linked_repo.symlink_to(git_repo, target_is_directory=True)
    db.execute("UPDATE repos SET root_path = ? WHERE id = 'repo1'", (str(linked_repo),))
    db.commit()

    with pytest.raises(ThreadGitOwnershipError):
        await ThreadOrchestrationService().spawn_child(
            _orchestration_request(),
            parent_session_id=caller.session_id,
            body=_child_body(),
            caller=caller,
            runtime_ready=lambda: True,
        )

    assert db.execute("SELECT COUNT(*) FROM thread_delegations").fetchone()[0] == 0
    common = (
        await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        )
    ).stdout.strip()
    assert not (Path(common) / ".repository-lifecycle-locks").exists()


async def test_agent_replay_rejects_a_manual_row_with_matching_task_metadata(
    db: Any,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent delivery cannot replay a manual reservation with the same UUID."""
    _enable_agent_delegation(monkeypatch)
    caller = _seed_active_caller(db, git_repo)
    body = _child_body()
    deterministic_key = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"yinshi:thread-spawn:{caller.run_id}:{caller.tool_call_id}",
        )
    )
    db.execute(
        "INSERT INTO thread_delegations "
        "(id, parent_session_id, idempotency_key, initiator, title, task, context, role, "
        "requested_model, requested_thinking, status, retry_of_delegation_id, auto_start) "
        "VALUES (?, ?, ?, 'user', ?, ?, ?, ?, ?, ?, 'completed', NULL, ?)",
        (
            "d" * 32,
            caller.session_id,
            deterministic_key,
            body.title,
            body.task,
            body.context,
            body.role,
            normalize_model_ref(body.model),
            body.thinking,
            int(body.start_immediately),
        ),
    )
    db.commit()

    with pytest.raises(ThreadIdempotencyConflictError):
        await ThreadOrchestrationService().spawn_child(
            _orchestration_request(),
            parent_session_id=caller.session_id,
            body=body,
            caller=caller,
            runtime_ready=lambda: False,
        )


async def test_new_agent_spawn_does_not_run_stale_provisioning_recovery(
    db: Any,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent admission performs no unrelated recovery before reservation."""
    from yinshi.services import thread_orchestration

    _enable_agent_delegation(monkeypatch)
    caller = _seed_active_caller(db, git_repo)

    async def unexpected_recovery(*args: object, **kwargs: object) -> None:
        raise AssertionError("stale recovery ran during agent admission")

    monkeypatch.setattr(
        thread_orchestration,
        "reconcile_stale_provisioning",
        unexpected_recovery,
    )
    outcome = await ThreadOrchestrationService().spawn_child(
        _orchestration_request(),
        parent_session_id=caller.session_id,
        body=_child_body(),
        caller=caller,
        runtime_ready=lambda: True,
    )

    assert outcome.status == "queued"
