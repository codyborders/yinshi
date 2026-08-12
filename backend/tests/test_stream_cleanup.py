"""Prompt stream finalization cleanup tests."""

import sqlite3
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from tests.factories import create_full_stack, make_mock_sidecar


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


def test_prompt_final_status_failure_still_releases_runtime_resources(
    client: TestClient,
    git_repo: str,
) -> None:
    """Final persistence failure must not block mandatory runtime cleanup."""
    from yinshi.api import stream

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

    original_get_db = stream.get_db_for_request

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
        patch("yinshi.api.stream.get_db_for_request", side_effect=fail_final_status),
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
