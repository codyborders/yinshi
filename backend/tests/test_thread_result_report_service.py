"""report_result service behavior: draft insert, update, conflict, replay."""

from __future__ import annotations

import json


def _orchestration_request() -> object:
    """Build one minimal request carrying no tenant, like spawn tests."""
    from types import SimpleNamespace

    from fastapi import Request

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


def _seed_child(db) -> str:
    """Insert one repo, workspace, parent session, and delegated child."""
    db.executescript("""
        INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'repo', '/tmp/r');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('parent-ws', 'repo1', 'p', 'main', '/tmp/r/w');
        INSERT INTO sessions (id, workspace_id) VALUES ('parent-session', 'parent-ws');
        INSERT INTO workspaces (id, repo_id, name, branch, path, kind, parent_workspace_id)
            VALUES ('child-ws', 'repo1', 'c', 'yinshi/thread-d4e5f6a7',
                    '/tmp/r/.worktrees/c', 'delegated', 'parent-ws');
        INSERT INTO sessions (id, workspace_id) VALUES ('child-session', 'child-ws');
        INSERT INTO thread_delegations (
            id, parent_session_id, child_session_id, child_workspace_id,
            idempotency_key, initiator, title, task, requested_model, status,
            base_commit
        ) VALUES (
            'd4e5f6a7b8c9d0e1f2a3b4c5d6e7f801', 'parent-session', 'child-session',
            'child-ws', 'k1', 'user', 'Child', 'task', 'm', 'completed', 'baseabc'
        );
        """)
    db.commit()
    return "child-session"


def test_report_inserts_version_one_reported_draft(db) -> None:
    """expected_version 0 on a child without a result inserts version 1."""
    import asyncio

    from yinshi.models import ThreadResultReportCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    _seed_child(db)
    service = ThreadOrchestrationService()

    outcome = asyncio.run(
        service.report_result(
            _orchestration_request(),
            child_session_id="child-session",
            body=ThreadResultReportCreate(
                expected_version=0,
                summary="Work done.",
                tests=[{"command": "pytest -q", "status": "passed"}],
                warnings=["flaky retry"],
            ),
        )
    )

    assert outcome["delegation_id"] == "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
    assert outcome["version"] == 1
    assert outcome["source"] == "reported"
    assert outcome["sealed"] is False
    assert outcome["summary"] == "Work done."
    assert outcome["tests"] == [{"command": "pytest -q", "status": "passed", "summary": None}]
    assert outcome["warnings"] == ["flaky retry"]
    row = db.execute(
        "SELECT * FROM thread_results WHERE delegation_id = ?",
        ("d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801",),
    ).fetchone()
    assert row is not None
    assert row["version"] == 1
    assert row["source"] == "reported"
    assert row["sealed"] == 0


def _seed_draft(db, delegation_id: str = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801") -> None:
    """Insert one unsealed version-1 draft for the seeded child."""
    db.execute(
        """INSERT INTO thread_results (
               delegation_id, version, source, summary, tests_json, warnings_json
           ) VALUES (?, 1, 'reported', 'first', '[{\"command\":\"pytest -q\",\"status\":\"passed\",\"summary\":null}]', '[]')""",
        (delegation_id,),
    )
    db.commit()


def test_report_matching_version_updates_and_increments(db) -> None:
    """A report at the current version updates the same row to version 2."""
    import asyncio

    from yinshi.models import ThreadResultReportCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    _seed_child(db)
    _seed_draft(db)
    service = ThreadOrchestrationService()

    outcome = asyncio.run(
        service.report_result(
            _orchestration_request(),
            child_session_id="child-session",
            body=ThreadResultReportCreate(
                expected_version=1,
                summary="second",
                tests=[{"command": "pytest -q", "status": "failed", "summary": "boom"}],
                warnings=["w1"],
            ),
        )
    )

    assert outcome["version"] == 2
    assert outcome["summary"] == "second"
    rows = db.execute(
        "SELECT version, summary, tests_json, warnings_json FROM thread_results"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["version"] == 2
    assert rows[0]["summary"] == "second"
    assert json.loads(rows[0]["tests_json"]) == [
        {"command": "pytest -q", "status": "failed", "summary": "boom"}
    ]
    assert json.loads(rows[0]["warnings_json"]) == ["w1"]


def test_report_stale_exact_payload_replay_returns_current(db) -> None:
    """A stale report whose normalized payload matches returns the current row."""
    import asyncio

    from yinshi.models import ThreadResultReportCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    _seed_child(db)
    _seed_draft(db)
    service = ThreadOrchestrationService()

    outcome = asyncio.run(
        service.report_result(
            _orchestration_request(),
            child_session_id="child-session",
            body=ThreadResultReportCreate(
                expected_version=0,
                summary="first",
                tests=[{"command": "pytest -q", "status": "passed"}],
            ),
        )
    )

    assert outcome["version"] == 1
    assert outcome["summary"] == "first"
    rows = db.execute("SELECT version, summary FROM thread_results").fetchall()
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    assert rows[0]["summary"] == "first"


def test_report_stale_changed_payload_conflicts(db) -> None:
    """A stale report with a changed payload raises the typed conflict."""
    import asyncio

    import pytest

    from yinshi.models import ThreadResultReportCreate
    from yinshi.services.thread_orchestration import (
        ThreadOrchestrationService,
        ThreadResultVersionConflictError,
    )

    _seed_child(db)
    _seed_draft(db)
    service = ThreadOrchestrationService()

    with pytest.raises(ThreadResultVersionConflictError) as excinfo:
        asyncio.run(
            service.report_result(
                _orchestration_request(),
                child_session_id="child-session",
                body=ThreadResultReportCreate(expected_version=0, summary="changed"),
            )
        )

    assert excinfo.value.code == "result_version_conflict"
    rows = db.execute("SELECT version, summary FROM thread_results").fetchall()
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    assert rows[0]["summary"] == "first"


def test_report_sealed_row_conflicts_and_stays_immutable(db) -> None:
    """A report against a sealed row raises the sealed conflict, changing nothing."""
    import asyncio

    import pytest

    from yinshi.models import ThreadResultReportCreate
    from yinshi.services.thread_orchestration import (
        ThreadOrchestrationService,
        ThreadResultSealedError,
    )

    _seed_child(db)
    _seed_draft(db)
    db.execute("""UPDATE thread_results SET sealed = 1, summary = 'sealed summary',
           sealed_at = CURRENT_TIMESTAMP""")
    db.commit()
    service = ThreadOrchestrationService()

    with pytest.raises(ThreadResultSealedError) as excinfo:
        asyncio.run(
            service.report_result(
                _orchestration_request(),
                child_session_id="child-session",
                body=ThreadResultReportCreate(expected_version=1, summary="sneaky"),
            )
        )

    assert excinfo.value.code == "result_sealed"
    rows = db.execute("SELECT sealed, version, summary FROM thread_results").fetchall()
    assert len(rows) == 1
    assert rows[0]["sealed"] == 1
    assert rows[0]["version"] == 1
    assert rows[0]["summary"] == "sealed summary"


def test_report_reconciles_stale_provisioning_first(db) -> None:
    """A report reconciles aged provisioning reservations in the same database."""
    import asyncio

    from yinshi.models import ThreadResultReportCreate
    from yinshi.services.thread_orchestration import ThreadOrchestrationService

    _seed_child(db)
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, idempotency_key, initiator,
               title, task, requested_model, status, updated_at
           ) VALUES (
               'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'parent-session', 'stale-key',
               'user', 'Stale child', 'task', 'm', 'provisioning',
               datetime('now', '-700 seconds')
           )""")
    db.commit()
    service = ThreadOrchestrationService()

    asyncio.run(
        service.report_result(
            _orchestration_request(),
            child_session_id="child-session",
            body=ThreadResultReportCreate(expected_version=0, summary="done"),
        )
    )

    stale_row = db.execute(
        "SELECT * FROM thread_delegations WHERE id = ?",
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
    ).fetchone()
    assert stale_row is not None
    assert stale_row["status"] == "interrupted"
    assert stale_row["error_code"] == "provisioning_stale"


def test_report_hides_unknown_foreign_and_root_sessions(db) -> None:
    """Unknown, foreign, and non-child sessions raise hidden not-found errors."""
    import asyncio

    import pytest

    from yinshi.models import ThreadResultReportCreate
    from yinshi.services.thread_orchestration import (
        ThreadNotFoundError,
        ThreadOrchestrationService,
        ThreadParentNotAuthorizedError,
    )

    _seed_child(db)
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('root-session', 'parent-ws')")
    db.execute("UPDATE repos SET owner_email = 'a@example.com' WHERE id = 'repo1'")
    db.commit()
    service = ThreadOrchestrationService()
    body = ThreadResultReportCreate(expected_version=0, summary="done")

    with pytest.raises(ThreadNotFoundError):
        asyncio.run(
            service.report_result(
                _orchestration_request(),
                child_session_id="missing-session",
                body=body,
            )
        )
    with pytest.raises(ThreadNotFoundError):
        asyncio.run(
            service.report_result(
                _orchestration_request(),
                child_session_id="root-session",
                body=body,
            )
        )
    foreign_request = _orchestration_request()
    foreign_request.state.user_email = "b@example.com"
    with pytest.raises(ThreadParentNotAuthorizedError):
        asyncio.run(
            service.report_result(
                foreign_request,
                child_session_id="child-session",
                body=body,
            )
        )
