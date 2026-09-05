"""POST /api/threads/{child_session_id}/report API contract tests."""

from __future__ import annotations


def _post(client, child_session_id: str, **overrides: object):
    payload: dict[str, object] = {
        "expected_version": 0,
        "summary": "Work finished.",
    }
    payload.update(overrides)
    return client.post(
        f"/api/threads/{child_session_id}/report",
        json=payload,
        headers={"X-Requested-With": "XMLHttpRequest"},
    )


def _seed(noauth_client, db, git_repo, monkeypatch) -> None:
    """Seed one delegated child in the request-visible database."""
    import json as json_module

    from tests.test_thread_workspaces import seed_parent_stack

    seed_parent_stack(db, git_repo)
    db.executescript("""
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
    assert json_module is not None


def test_report_route_inserts_draft_and_returns_result_out(
    noauth_client, db, git_repo, monkeypatch
) -> None:
    """The report route stores the draft and returns the typed result body."""
    _seed(noauth_client, db, git_repo, monkeypatch)

    response = _post(
        noauth_client,
        "child-session",
        tests=[{"command": "pytest -q", "status": "passed"}],
        warnings=["flaky"],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["delegation_id"] == "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
    assert body["version"] == 1
    assert body["source"] == "reported"
    assert body["sealed"] is False
    assert body["summary"] == "Work finished."
    assert body["tests"] == [{"command": "pytest -q", "status": "passed", "summary": None}]
    assert body["warnings"] == ["flaky"]
    assert body["base_commit"] is None
    assert body["result_commit"] is None
    assert body["result_ref"] is None


def test_report_route_stale_conflict_maps_to_409(noauth_client, db, git_repo, monkeypatch) -> None:
    """A stale changed payload maps to a typed 409 conflict response."""
    _seed(noauth_client, db, git_repo, monkeypatch)
    first = _post(noauth_client, "child-session", summary="first")
    assert first.status_code == 200

    stale = _post(noauth_client, "child-session", expected_version=0, summary="second")

    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "result_version_conflict"
