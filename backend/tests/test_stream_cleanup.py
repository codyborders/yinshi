"""Prompt stream finalization cleanup tests."""

import asyncio
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from tests.factories import create_full_stack, make_mock_sidecar


def _seed_prompt_session(database: sqlite3.Connection) -> str:
    repository_id = uuid.uuid4().hex
    workspace_id = uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    database.execute(
        "INSERT INTO repos (id, name, root_path) VALUES (?, 'repo', '/tmp/repo')",
        (repository_id,),
    )
    database.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path)
           VALUES (?, ?, 'workspace', 'branch', '/tmp/workspace')""",
        (workspace_id, repository_id),
    )
    database.execute(
        "INSERT INTO sessions (id, workspace_id, status) VALUES (?, ?, 'running')",
        (session_id, workspace_id),
    )
    database.commit()
    return session_id


def test_prompt_finalization_releases_exact_activity_reservation(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Prompt cleanup must release the reservation acquired before sidecar use."""
    from yinshi.api.stream import ExecutionContext
    from yinshi.main import app

    stack = create_full_stack(auth_client, git_repo, name="activity-reservation-cleanup")
    session_id = stack["session"]["id"]
    runtime_id = stack["workspace"]["id"]
    reservation = object()
    container_manager = AsyncMock()
    container_manager.acquire_activity.return_value = reservation

    async def result_query(*_args, **_kwargs):
        yield {"type": "message", "data": {"type": "result", "usage": {}}}

    sidecar = make_mock_sidecar(result_query)
    previous_manager = app.state.container_manager
    app.state.container_manager = container_manager
    try:
        with (
            patch("yinshi.api.stream.create_sidecar_connection", return_value=sidecar),
            patch(
                "yinshi.api.stream._resolve_execution_context",
                new=AsyncMock(
                    return_value=ExecutionContext(
                        sidecar_socket="/tmp/activity.sock",
                        effective_cwd=stack["workspace"]["path"],
                        key_source="platform",
                        provider="test-provider",
                        provider_auth=None,
                        provider_config=None,
                        runtime_id=runtime_id,
                    )
                ),
            ),
        ):
            response = auth_client.post(
                f"/api/sessions/{session_id}/prompt",
                json={"prompt": "release the acquired reservation"},
            )
    finally:
        app.state.container_manager = previous_manager

    assert response.status_code == 200
    container_manager.acquire_activity.assert_awaited_once()
    acquire_call = container_manager.acquire_activity.await_args
    assert acquire_call is not None
    assert len(acquire_call.args[0]) == 32
    assert acquire_call.kwargs == {"runtime_id": runtime_id}
    container_manager.release_activity.assert_awaited_once_with(reservation)


def test_prompt_missing_runtime_preserves_stream_error_contract(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """A runtime removed before streaming must return the existing safe SSE error."""
    from yinshi.api.stream import ExecutionContext
    from yinshi.main import app

    stack = create_full_stack(auth_client, git_repo, name="missing-runtime-stream")
    session_id = stack["session"]["id"]
    runtime_id = stack["workspace"]["id"]
    container_manager = AsyncMock()
    container_manager.acquire_activity.return_value = None
    create_sidecar = AsyncMock()
    previous_manager = app.state.container_manager
    app.state.container_manager = container_manager
    try:
        with (
            patch("yinshi.api.stream.create_sidecar_connection", create_sidecar),
            patch(
                "yinshi.api.stream._resolve_execution_context",
                new=AsyncMock(
                    return_value=ExecutionContext(
                        sidecar_socket="/tmp/missing.sock",
                        effective_cwd=stack["workspace"]["path"],
                        key_source="platform",
                        provider="test-provider",
                        provider_auth=None,
                        provider_config=None,
                        runtime_id=runtime_id,
                    )
                ),
            ),
        ):
            response = auth_client.post(
                f"/api/sessions/{session_id}/prompt",
                json={"prompt": "use a runtime removed before streaming"},
            )
    finally:
        app.state.container_manager = previous_manager

    assert response.status_code == 200
    assert response.text == ('data: {"type": "error", "error": "An internal error occurred"}\n\n')
    create_sidecar.assert_not_awaited()
    container_manager.release_activity.assert_not_awaited()


def test_cancel_lookup_storage_failure_returns_bounded_json_503(
    client: TestClient,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel lookup uses bounded asynchronous database handling."""
    from yinshi.api import deps

    class TemporaryOperationalError(Exception):
        pass

    class FailingConnection:
        def __init__(self, connection) -> None:
            self.connection = connection

        def execute(self, query, parameters=()):
            if "FROM sessions s" in query:
                raise TemporaryOperationalError("disk I/O error")
            return self.connection.execute(query, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    original_get_db = deps.get_db_for_request

    @contextmanager
    def failed_lookup(request):
        with original_get_db(request) as database:
            yield FailingConnection(database)

    stack = create_full_stack(client, git_repo, name="cancel-storage-failure")
    monkeypatch.setattr(deps, "get_db_for_request", failed_lookup)
    monkeypatch.setattr(
        "yinshi.tenant._load_sqlcipher_module",
        lambda: SimpleNamespace(OperationalError=TemporaryOperationalError),
    )
    monkeypatch.setattr(deps, "_TENANT_DB_REQUEST_RETRY_BUDGET_SECONDS", 0.0)

    response = client.post(f"/api/sessions/{stack['session']['id']}/cancel")

    assert response.status_code == 503
    assert response.json() == {"detail": "Tenant storage is temporarily unavailable"}
    assert response.headers["retry-after"] == "1"


@pytest.mark.asyncio
async def test_prompt_workspace_preparation_blocks_concurrent_deletion(
    db: sqlite3.Connection,
    git_repo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt preparation holds the repository lock until metadata is applied."""
    from yinshi.api import stream
    from yinshi.services.workspace import (
        WorkspaceCheckoutPreparation,
        delete_workspace,
    )
    from yinshi.tenant import TenantContext

    repo_id = "prompt-preparation-lock"
    workspace_id = "prompt-preparation-workspace"
    original_workspace_path = str(Path(git_repo) / ".worktrees" / "original")
    repaired_workspace_path = tmp_path / "tenant" / "repos" / repo_id / ".worktrees" / "repaired"
    tenant = TenantContext(
        user_id="a" * 32,
        email="prompt-lock@example.com",
        data_dir=str(tmp_path / "tenant"),
        db_path=str(tmp_path / "tenant.db"),
    )
    db.execute(
        "INSERT INTO repos (id, name, root_path) VALUES (?, ?, ?)",
        (repo_id, "prompt-lock", git_repo),
    )
    db.execute(
        """INSERT INTO workspaces (id, repo_id, name, branch, path)
           VALUES (?, ?, ?, ?, ?)""",
        (workspace_id, repo_id, "prompt-lock", "repaired", original_workspace_path),
    )
    db.commit()

    preparation_entered = asyncio.Event()
    release_preparation = asyncio.Event()
    state_loads = [0]

    async def direct_database_operation(_request, operation, *, background=False):
        del background
        return operation(db)

    async def paused_preparation(_tenant, state):
        preparation_entered.set()
        await release_preparation.wait()
        repaired_workspace_path.mkdir(parents=True)
        return WorkspaceCheckoutPreparation(
            workspace_id=state.workspace_id,
            repo_id=state.repo_id,
            repo_path=str(repaired_workspace_path.parents[1]),
            remote_url=state.remote_url,
            installation_id=state.installation_id,
            workspace_paths=((workspace_id, str(repaired_workspace_path)),),
            update_repo_metadata=True,
            repaired_repo=True,
        )

    original_load = stream.load_workspace_checkout_state

    def counted_load(database, target_workspace_id):
        state_loads[0] += 1
        return original_load(database, target_workspace_id)

    async def remove_worktree(_repo_path, workspace_path):
        Path(workspace_path).rmdir()

    monkeypatch.setattr(stream, "_prompt_database_operation", direct_database_operation)
    monkeypatch.setattr(stream, "load_workspace_checkout_state", counted_load)
    monkeypatch.setattr(stream, "prepare_workspace_checkout_for_tenant", paused_preparation)
    monkeypatch.setattr("yinshi.services.workspace.delete_worktree", remove_worktree)

    preparation = asyncio.create_task(
        stream._prepare_prompt_workspace_checkout(object(), tenant, workspace_id)
    )
    await asyncio.wait_for(preparation_entered.wait(), timeout=1)
    deletion = asyncio.create_task(delete_workspace(db, workspace_id))
    await asyncio.sleep(0)

    assert not deletion.done()
    release_preparation.set()
    await asyncio.wait_for(preparation, timeout=1)
    await asyncio.wait_for(deletion, timeout=1)

    assert state_loads == [2]
    assert db.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone() is None
    assert not repaired_workspace_path.exists()


def test_prompt_workspace_mid_operation_storage_failure_returns_bounded_json_503(
    auth_client: TestClient,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workspace database exhaustion does not replay prepared filesystem work."""
    from yinshi.api import deps, stream

    class TemporaryOperationalError(Exception):
        pass

    class FailingConnection:
        def __init__(self, connection) -> None:
            self.connection = connection

        def execute(self, query, parameters=()):
            if "SELECT * FROM workspaces WHERE id = ?" in query:
                workspace_reads[0] += 1
                if workspace_reads[0] == 3:
                    raise TemporaryOperationalError("disk I/O error")
            return self.connection.execute(query, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    original_get_db = deps.get_db_for_request
    original_prepare = stream.prepare_workspace_checkout_for_tenant
    workspace_reads = [0]
    preparation_calls = [0]

    @contextmanager
    def fail_workspace_apply(request):
        with original_get_db(request) as database:
            yield FailingConnection(database)

    async def counted_prepare(*args, **kwargs):
        preparation_calls[0] += 1
        return await original_prepare(*args, **kwargs)

    stack = create_full_stack(auth_client, git_repo, name="workspace-storage-failure")
    monkeypatch.setattr(deps, "get_db_for_request", fail_workspace_apply)
    monkeypatch.setattr(stream, "prepare_workspace_checkout_for_tenant", counted_prepare)
    monkeypatch.setattr(
        "yinshi.tenant._load_sqlcipher_module",
        lambda: SimpleNamespace(OperationalError=TemporaryOperationalError),
    )
    monkeypatch.setattr(deps, "_TENANT_DB_REQUEST_RETRY_BUDGET_SECONDS", 0.0)

    response = auth_client.post(
        f"/api/sessions/{stack['session']['id']}/prompt",
        json={"prompt": "fail workspace storage"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Tenant storage is temporarily unavailable"}
    assert response.headers["retry-after"] == "1"
    assert preparation_calls == [1]


def test_prompt_reservation_cleanup_uses_fresh_retry_budget(
    auth_client: TestClient,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired foreground budget does not prevent reservation cleanup retry."""
    from yinshi.api import deps, stream

    class TemporaryOperationalError(Exception):
        pass

    class FailingConnection:
        def __init__(self, connection) -> None:
            self.connection = connection

        def execute(self, query, parameters=()):
            if "UPDATE sessions SET status = 'idle'" in query and not cleanup_failed[0]:
                cleanup_failed[0] = True
                raise TemporaryOperationalError("disk I/O error")
            return self.connection.execute(query, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    original_get_db = deps.get_db_for_request
    cleanup_failed = [False]

    @contextmanager
    def temporary_cleanup_failure(request):
        with original_get_db(request) as database:
            yield FailingConnection(database)

    async def fail_execution_context(request, *_args, **_kwargs):
        request.state.tenant_database_retry_deadline = asyncio.get_running_loop().time() - 1
        raise RuntimeError("context resolution failed")

    stack = create_full_stack(auth_client, git_repo, name="cleanup-retry-budget")
    monkeypatch.setattr(deps, "get_db_for_request", temporary_cleanup_failure)
    monkeypatch.setattr(stream, "_resolve_execution_context", fail_execution_context)
    monkeypatch.setattr(
        "yinshi.tenant._load_sqlcipher_module",
        lambda: SimpleNamespace(OperationalError=TemporaryOperationalError),
    )
    monkeypatch.setattr(deps, "_TENANT_DB_RETRY_DELAY_SECONDS", 0.0)

    with pytest.raises(RuntimeError, match="context resolution failed"):
        auth_client.post(
            f"/api/sessions/{stack['session']['id']}/prompt",
            json={"prompt": "clean up after failure"},
        )

    assert cleanup_failed == [True]
    assert auth_client.get(f"/api/sessions/{stack['session']['id']}").json()["status"] == "idle"


@pytest.mark.asyncio
async def test_prompt_cancellation_remains_authoritative_when_cleanup_fails(
    client: TestClient,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup failure does not replace caller cancellation."""
    from yinshi.api import stream
    from yinshi.main import app

    stack = create_full_stack(client, git_repo, name="cancel-cleanup-authority")
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"/api/sessions/{stack['session']['id']}/prompt",
            "raw_path": b"/api/prompt",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
            "app": app,
            "state": {},
        }
    )
    cleanup = AsyncMock(side_effect=RuntimeError("cleanup failed"))
    monkeypatch.setattr(
        stream,
        "_resolve_execution_context",
        AsyncMock(side_effect=asyncio.CancelledError),
    )
    monkeypatch.setattr(stream, "_set_prompt_session_idle", cleanup)

    with pytest.raises(asyncio.CancelledError):
        await stream.prompt_session(
            stack["session"]["id"],
            stream.PromptRequest(prompt="cancel during setup"),
            request,
        )

    cleanup.assert_awaited_once_with(
        request,
        stack["session"]["id"],
        ANY,
        background=True,
    )


@pytest.mark.asyncio
async def test_prompt_cleanup_and_finalization_preserve_newer_turn(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delayed cleanup and finalization cannot release a newer prompt turn."""
    from yinshi.api import stream

    async def direct_database_operation(_request, operation, *, background=False):
        del background
        return operation(db)

    monkeypatch.setattr(stream, "_prompt_database_operation", direct_database_operation)

    cancelled_session_id = _seed_prompt_session(db)
    newer_turn_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO messages (session_id, role, content, turn_id) VALUES (?, 'user', ?, ?)",
        (cancelled_session_id, "newer prompt", newer_turn_id),
    )
    db.commit()

    await stream._cleanup_cancelled_prompt_reservation(
        object(),
        cancelled_session_id,
        uuid.uuid4().hex,
    )

    assert (
        db.execute("SELECT status FROM sessions WHERE id = ?", (cancelled_session_id,)).fetchone()[
            0
        ]
        == "running"
    )

    finalized_session_id = _seed_prompt_session(db)
    old_turn_id = uuid.uuid4().hex
    latest_turn_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO messages (session_id, role, content, turn_id) VALUES (?, 'user', ?, ?)",
        (finalized_session_id, "old prompt", old_turn_id),
    )
    db.execute(
        "INSERT INTO messages (session_id, role, content, turn_id) VALUES (?, 'user', ?, ?)",
        (finalized_session_id, "new prompt", latest_turn_id),
    )
    db.commit()

    await stream._persist_assistant_turn(
        object(),
        message_id=uuid.uuid4().hex,
        session_id=finalized_session_id,
        turn_id=old_turn_id,
        content="old answer",
        turn_status="completed",
        finalize_session=True,
    )

    assert (
        db.execute("SELECT status FROM sessions WHERE id = ?", (finalized_session_id,)).fetchone()[
            0
        ]
        == "running"
    )


def test_prompt_final_persistence_recovers_partial_assistant_turn(
    client: TestClient,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Temporary final I/O fills partial fields and returns the session to idle."""
    from yinshi.api import deps
    from yinshi.api.stream import ExecutionContext

    session_id = create_full_stack(client, git_repo, name="final-storage-retry")["session"]["id"]

    async def assistant_chunks(*_args, **_kwargs):
        for index in range(10):
            yield {
                "type": "message",
                "data": {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": f"part-{index} "}]},
                },
            }

    class TemporaryOperationalError(Exception):
        pass

    class FailingConnection:
        def __init__(self, connection) -> None:
            self.connection = connection

        def execute(self, query, parameters=()):
            if (
                "UPDATE messages SET content = ?, full_message = COALESCE" in query
                and not failure_reported[0]
            ):
                failure_reported[0] = True
                raise TemporaryOperationalError("disk I/O error")
            return self.connection.execute(query, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    original_get_db = deps.get_db_for_request
    failure_reported = [False]

    @contextmanager
    def temporary_final_failure(request):
        with original_get_db(request) as database:
            yield FailingConnection(database)

    sidecar = make_mock_sidecar(assistant_chunks)
    monkeypatch.setattr(deps, "get_db_for_request", temporary_final_failure)
    monkeypatch.setattr(
        "yinshi.tenant._load_sqlcipher_module",
        lambda: SimpleNamespace(OperationalError=TemporaryOperationalError),
    )
    monkeypatch.setattr(deps, "_TENANT_DB_RETRY_DELAY_SECONDS", 0.0)
    with (
        patch("yinshi.api.stream.create_sidecar_connection", return_value=sidecar),
        patch(
            "yinshi.api.stream._resolve_execution_context",
            new=AsyncMock(
                return_value=ExecutionContext(
                    sidecar_socket=None,
                    effective_cwd=git_repo,
                    key_source="platform",
                    provider="test-provider",
                    provider_auth=None,
                    provider_config=None,
                )
            ),
        ),
    ):
        response = client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "recover final persistence"},
        )

    assert response.status_code == 200
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assistants = [message for message in messages if message["role"] == "assistant"]
    assert len([message for message in messages if message["role"] == "user"]) == 1
    assert len(assistants) == 1
    assert assistants[0]["content"] == "".join(f"part-{index} " for index in range(10))
    assert assistants[0]["turn_status"] == "completed"
    assert json.loads(assistants[0]["full_message"])["schema"] == "yinshi.assistant_turn.v1"
    assert client.get(f"/api/sessions/{session_id}").json()["status"] == "idle"
    assert failure_reported == [True]


def test_prompt_terminal_reconciliation_releases_session_after_finalization_exhaustion(
    auth_client: TestClient,
    git_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Journal terminal persistence releases a session after stream storage exhaustion."""
    from yinshi.api import deps
    from yinshi.api.stream import ExecutionContext
    from yinshi.main import app
    from yinshi.services.prompt_journal import PromptJournal

    class TemporaryOperationalError(Exception):
        pass

    class FailingConnection:
        def __init__(self, connection) -> None:
            self.connection = connection

        def execute(self, query, parameters=()):
            if (
                final_turn_failures[0]
                and "turn_status = COALESCE" in query
                and len(parameters) >= 3
                and parameters[2] is not None
            ):
                raise TemporaryOperationalError("disk I/O error")
            return self.connection.execute(query, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    async def assistant_chunks(*_args, **_kwargs):
        for index in range(10):
            yield {
                "type": "message",
                "data": {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": f"part-{index} "}]},
                },
            }
        yield {"type": "message", "data": {"type": "result", "usage": {}}}

    original_get_db = deps.get_db_for_request
    final_turn_failures = [True]

    @contextmanager
    def fail_final_turn(request):
        with original_get_db(request) as database:
            yield FailingConnection(database)

    stack = create_full_stack(auth_client, git_repo, name="journal-final-reconciliation")
    session_id = stack["session"]["id"]
    sidecar = make_mock_sidecar(assistant_chunks)
    monkeypatch.setattr(app.state, "prompt_journal", PromptJournal())
    monkeypatch.setattr(deps, "get_db_for_request", fail_final_turn)
    monkeypatch.setattr(
        "yinshi.tenant._load_sqlcipher_module",
        lambda: SimpleNamespace(OperationalError=TemporaryOperationalError),
    )
    monkeypatch.setattr(deps, "_TENANT_DB_RETRY_DEADLINE_SECONDS", 0.0)

    def wait_for_status(run_id: str, expected: str) -> dict:
        body = {}
        for _ in range(100):
            response = auth_client.get(f"/api/sessions/{session_id}/runs/{run_id}/events/0")
            assert response.status_code == 200
            body = response.json()
            if body["status"] == expected:
                return body
            asyncio.run(asyncio.sleep(0))
        raise AssertionError(f"prompt run did not reach {expected}")

    with (
        patch("yinshi.api.stream.create_sidecar_connection", return_value=sidecar),
        patch(
            "yinshi.api.stream._resolve_execution_context",
            new=AsyncMock(
                return_value=ExecutionContext(
                    sidecar_socket=None,
                    effective_cwd=git_repo,
                    key_source="platform",
                    provider="test-provider",
                    provider_auth=None,
                    provider_config=None,
                )
            ),
        ),
    ):
        first = auth_client.post(
            f"/api/sessions/{session_id}/runs",
            json={
                "prompt": "exhaust final persistence",
                "idempotency_key": str(uuid.uuid4()),
            },
        )
        assert first.status_code == 202
        wait_for_status(first.json()["id"], "failed")

        messages = auth_client.get(f"/api/sessions/{session_id}/messages").json()
        users = [message for message in messages if message["role"] == "user"]
        assistants = [message for message in messages if message["role"] == "assistant"]
        assert len(users) == 1
        assert users[0]["turn_id"] == first.json()["id"]
        assert len(assistants) == 1
        assert assistants[0]["content"] == "".join(f"part-{index} " for index in range(10))
        assert assistants[0]["turn_status"] is None
        assert auth_client.get(f"/api/sessions/{session_id}").json()["status"] == "idle"

        final_turn_failures[0] = False
        second = auth_client.post(
            f"/api/sessions/{session_id}/runs",
            json={
                "prompt": "prompt after reconciliation",
                "idempotency_key": str(uuid.uuid4()),
            },
        )
        assert second.status_code == 202
        wait_for_status(second.json()["id"], "completed")
        messages = auth_client.get(f"/api/sessions/{session_id}/messages").json()
        users = [message for message in messages if message["role"] == "user"]
        assert [message["turn_id"] for message in users] == [
            first.json()["id"],
            second.json()["id"],
        ]


def test_prompt_final_status_failure_still_releases_runtime_resources(
    client: TestClient,
    git_repo: str,
) -> None:
    """Final persistence failure must not block mandatory runtime cleanup."""
    from yinshi.api import deps

    session_id = create_full_stack(client, git_repo, name="cleanup-test")["session"]["id"]

    async def empty_query(*_args, **_kwargs):
        if False:
            yield {}

    class StatusFailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, parameters=()):
            if "UPDATE sessions SET status = 'idle'" in query:
                raise sqlite3.OperationalError("final persistence failed")
            return self.connection.execute(query, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    original_get_db = deps.get_db_for_request

    @contextmanager
    def fail_final_status(request):
        with original_get_db(request) as database:
            yield StatusFailingConnection(database)

    sidecar = make_mock_sidecar(empty_query)
    coordinator = AsyncMock()
    coordinator.release.side_effect = RuntimeError("release failed")
    touch_container = Mock(side_effect=RuntimeError("touch failed"))
    sidecar.disconnect.side_effect = RuntimeError("disconnect failed")
    with (
        patch("yinshi.api.deps.get_db_for_request", side_effect=fail_final_status),
        patch("yinshi.api.stream.create_sidecar_connection", return_value=sidecar),
        patch("yinshi.api.stream.get_run_coordinator", return_value=coordinator),
        patch("yinshi.api.stream.touch_tenant_container", touch_container),
        pytest.raises(sqlite3.OperationalError, match="final persistence failed"),
    ):
        client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "trigger final persistence"},
        )

    touch_container.assert_called_once()
    coordinator.release.assert_awaited_once_with(session_id)
    sidecar.disconnect.assert_awaited_once()
