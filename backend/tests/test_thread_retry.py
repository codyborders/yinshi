"""Retry service and API tests: lineage, overrides, statuses, replay."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.test_thread_orchestration import _orchestration_request
from tests.test_thread_workspaces import seed_parent_stack
from yinshi.models import ThreadChildCreate


def _spawn_failed_child(service, db, *, title: str = "Original child"):
    """Spawn one started child through a recording journal, then fail it."""
    from yinshi.services.prompt_journal import PromptJournal, PromptRun

    class RecordingJournal(PromptJournal):
        def __init__(self) -> None:
            self.starts: list[dict[str, object]] = []

        async def start(self, **kwargs) -> PromptRun:
            self.starts.append(kwargs)
            return PromptRun(
                id=uuid.uuid4().hex,
                session_id=str(kwargs["session_id"]),
                status="starting",
            )

    request = _orchestration_request()
    journal = RecordingJournal()
    request.app.state.prompt_journal = journal
    spawned = asyncio.run(
        service.spawn_child(
            request,
            parent_session_id="parent-session",
            body=ThreadChildCreate(
                idempotency_key=str(uuid.uuid4()),
                title=title,
                task="Original task text.",
                context="Original context.",
                role="implementation",
                model="model-x",
                thinking="high",
                start_immediately=True,
            ),
        )
    )
    db.execute(
        "UPDATE thread_delegations SET status = 'failed', error_code = 'start_failed',"
        " error_detail_safe = 'initial prompt run failed to start' WHERE id = ?",
        (spawned.delegation_id,),
    )
    db.commit()
    return spawned, request, journal


def test_retry_creates_lineage_child_copied_from_original(db, git_repo) -> None:
    """Retry spawns a new running child linked to the failed original."""
    from yinshi.models import ThreadRetryCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    spawned, request, journal = _spawn_failed_child(service, db)

    outcome = asyncio.run(
        service.retry_child(
            request,
            child_session_id=spawned.child_session_id,
            body=ThreadRetryCreate(idempotency_key=str(uuid.uuid4())),
        )
    )

    assert outcome.status == "running"
    assert outcome.child_session_id
    assert outcome.delegation_id != spawned.delegation_id
    assert outcome.child_session_id != spawned.child_session_id
    delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (outcome.delegation_id,),
    ).fetchone()
    assert delegation["retry_of_delegation_id"] == spawned.delegation_id
    assert delegation["title"] == "Original child"
    assert delegation["task"] == "Original task text."
    assert delegation["context"] == "Original context."
    assert delegation["role"] == "implementation"
    assert delegation["requested_model"] == "model-x"
    assert delegation["requested_thinking"] == "high"
    original = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    assert original["status"] == "failed"
    assert original["child_session_id"] == spawned.child_session_id
    assert len(journal.starts) == 2


def test_retry_model_thinking_overrides_stored_choices(db, git_repo) -> None:
    """Retry overrides replace the stored model and thinking choices."""
    from yinshi.models import ThreadRetryCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    spawned, request, journal = _spawn_failed_child(service, db)

    outcome = asyncio.run(
        service.retry_child(
            request,
            child_session_id=spawned.child_session_id,
            body=ThreadRetryCreate(
                idempotency_key=str(uuid.uuid4()),
                model="model-y",
                thinking=" Low ",
            ),
        )
    )

    delegation = db.execute(
        "SELECT requested_model, requested_thinking FROM thread_delegations WHERE id = ?",
        (outcome.delegation_id,),
    ).fetchone()
    assert delegation["requested_model"] == "model-y"
    assert delegation["requested_thinking"] == "low"
    started = journal.starts[1]
    assert started["body"].thinking == "low"
    original = db.execute(
        "SELECT requested_model, requested_thinking FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    assert original["requested_model"] == "model-x"
    assert original["requested_thinking"] == "high"


def test_retry_rejects_non_terminal_statuses(db, git_repo) -> None:
    """Only failed, cancelled, and interrupted children may be retried."""
    import pytest as pytest_module

    from yinshi.models import ThreadRetryCreate
    from yinshi.services.thread_orchestration import (
        ThreadOrchestrationService,
        ThreadRetryNotAllowedError,
    )

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    spawned, request, _ = _spawn_failed_child(service, db)
    for status in ("provisioning", "queued", "running", "cancelling", "completed"):
        db.execute(
            "UPDATE thread_delegations SET status = ? WHERE id = ?",
            (status, spawned.delegation_id),
        )
        db.commit()
        with pytest_module.raises(ThreadRetryNotAllowedError):
            asyncio.run(
                service.retry_child(
                    request,
                    child_session_id=spawned.child_session_id,
                    body=ThreadRetryCreate(idempotency_key=str(uuid.uuid4())),
                )
            )
        rows = db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"]
        assert rows == 1


def test_retry_replay_returns_same_child_without_restart(db, git_repo) -> None:
    """The same retry key replays to the same child and never restarts it."""
    from yinshi.models import ThreadRetryCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    spawned, request, journal = _spawn_failed_child(service, db)
    body = ThreadRetryCreate(idempotency_key=str(uuid.uuid4()))

    first = asyncio.run(
        service.retry_child(
            request,
            child_session_id=spawned.child_session_id,
            body=body,
        )
    )
    second = asyncio.run(
        service.retry_child(
            request,
            child_session_id=spawned.child_session_id,
            body=body,
        )
    )

    assert second == first
    assert first.status == "running"
    assert len(journal.starts) == 2
    assert db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"] == 2
    assert (
        db.execute("SELECT COUNT(*) AS n FROM workspaces WHERE kind = 'delegated'").fetchone()["n"]
        == 2
    )


def test_retry_preserves_original_resources_and_result(db, git_repo) -> None:
    """The original child's workspace and sealed result survive a retry."""
    from yinshi.models import ThreadRetryCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    spawned, request, _ = _spawn_failed_child(service, db)
    original_delegation = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    original_workspace = str(original_delegation["child_workspace_id"])
    db.execute(
        """INSERT INTO thread_results (delegation_id, source, sealed, summary)
           VALUES (?, 'reported', 1, 'original result')""",
        (spawned.delegation_id,),
    )
    db.commit()

    outcome = asyncio.run(
        service.retry_child(
            request,
            child_session_id=spawned.child_session_id,
            body=ThreadRetryCreate(idempotency_key=str(uuid.uuid4())),
        )
    )

    retry_delegation = db.execute(
        "SELECT child_workspace_id FROM thread_delegations WHERE id = ?",
        (outcome.delegation_id,),
    ).fetchone()
    assert retry_delegation["child_workspace_id"] != original_workspace
    original_session = db.execute(
        "SELECT id FROM sessions WHERE id = ?",
        (spawned.child_session_id,),
    ).fetchone()
    assert original_session is not None
    result = db.execute(
        "SELECT sealed, summary FROM thread_results WHERE delegation_id = ?",
        (spawned.delegation_id,),
    ).fetchone()
    assert result is not None
    assert result["sealed"] == 1
    assert result["summary"] == "original result"
    assert db.execute("SELECT COUNT(*) AS n FROM thread_results").fetchone()["n"] == 1


def test_retry_maps_unknown_and_undelegated_sessions_to_not_found(db, git_repo) -> None:
    """Unknown, parent, and foreign child sessions never reveal retries."""
    import yinshi.services.thread_orchestration as orchestration_module
    from yinshi.models import ThreadRetryCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    seed_parent_stack(db, git_repo)
    service = ThreadOrchestrationService()
    spawned, request, _ = _spawn_failed_child(service, db)
    body = ThreadRetryCreate(idempotency_key=str(uuid.uuid4()))

    with pytest.raises(orchestration_module.ThreadNotFoundError):
        asyncio.run(
            service.retry_child(
                request,
                child_session_id="missing-session",
                body=body,
            )
        )
    with pytest.raises(orchestration_module.ThreadNotFoundError):
        asyncio.run(
            service.retry_child(
                request,
                child_session_id="parent-session",
                body=body,
            )
        )
    db.execute("UPDATE repos SET owner_email = 'owner@example.com' WHERE id = 'repo1'")
    db.commit()
    foreign_request = _orchestration_request()
    foreign_request.state.user_email = "other@example.com"
    with pytest.raises(orchestration_module.ThreadParentNotAuthorizedError):
        asyncio.run(
            service.retry_child(
                foreign_request,
                child_session_id=spawned.child_session_id,
                body=body,
            )
        )
    assert db.execute("SELECT COUNT(*) AS n FROM thread_delegations").fetchone()["n"] == 1


def test_thread_retry_create_rejects_noncanonical_uuid() -> None:
    """Only canonical UUIDs are accepted as retry idempotency keys."""
    from pydantic import ValidationError

    from yinshi.models import ThreadRetryCreate

    with pytest.raises(ValidationError):
        ThreadRetryCreate(idempotency_key="not-a-uuid")
    with pytest.raises(ValidationError):
        ThreadRetryCreate(idempotency_key=str(uuid.uuid4()).upper())


def test_thread_retry_create_requires_canonical_uuid() -> None:
    """The retry idempotency key must be one canonical UUID."""
    from pydantic import ValidationError

    from yinshi.models import ThreadRetryCreate

    body = ThreadRetryCreate(idempotency_key=str(uuid.uuid4()))
    assert body.model is None
    assert body.thinking is None
    with pytest.raises(ValidationError):
        ThreadRetryCreate(idempotency_key="not-a-uuid")
    with pytest.raises(ValidationError):
        ThreadRetryCreate(idempotency_key=str(uuid.uuid4()).upper())
