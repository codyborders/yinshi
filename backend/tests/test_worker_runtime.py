"""Verify restricted worker authentication and route composition.

The worker contract runs in-process for BYOC dispatch. Tests prove that a
connection-specific bearer sets one tenant and that control-plane routes stay absent.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from yinshi.tenant import TenantContext
from yinshi.worker_auth import WorkerPrincipal
from yinshi.worker_runtime import WorkerHttpDispatcher


def _principal(tmp_path: Path) -> WorkerPrincipal:
    data_directory = tmp_path / "worker-user"
    return WorkerPrincipal(
        tenant=TenantContext(
            user_id="worker-user-1",
            email="worker-user@runner.invalid",
            data_dir=str(data_directory),
            db_path=str(data_directory / "yinshi.db"),
        ),
        bearer_token="w" * 48,
    )


def test_worker_app_requires_internal_bearer_and_excludes_control_routes(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Only the injected tenant can reach execution routes."""
    from yinshi.main import create_app

    principal = _principal(tmp_path)
    app = create_app(mode="worker", worker_principal=principal)

    with TestClient(app) as client:
        assert client.get("/api/repos").status_code == 401
        response = client.get(
            "/api/repos",
            headers={"Authorization": f"Bearer {principal.bearer_token}"},
        )
        assert response.status_code == 200
        assert response.json() == []
        assert (
            client.get(
                "/api/settings/runner",
                headers={"Authorization": f"Bearer {principal.bearer_token}"},
            ).status_code
            == 404
        )
    route_paths = set(app.openapi()["paths"])
    assert "/auth/providers/{provider}/start" in route_paths
    assert "/auth/providers/{provider}/callback" in route_paths
    assert "/auth/login/google" not in route_paths
    assert "/auth/desktop/token" not in route_paths


@pytest.mark.asyncio
async def test_worker_http_dispatcher_uses_existing_repository_contract(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """In-process dispatch returns the same repository response as worker HTTP."""
    from yinshi.main import create_app

    principal = _principal(tmp_path)
    app = create_app(mode="worker", worker_principal=principal)
    dispatcher = WorkerHttpDispatcher(app=app, principal=principal)

    response = await dispatcher.request(method="GET", path="/api/repos", body=None)

    assert response.status_code == 200
    assert response.body == []
    assert response.content_type == "application/json"
    with pytest.raises(ValueError, match="relative application path"):
        await dispatcher.request(
            method="GET",
            path="https://attacker.example/api/repos",
            body=None,
        )


@pytest.mark.asyncio
async def test_worker_dispatcher_keeps_json_contract_after_temporary_storage(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Temporary tenant storage returns JSON 503 without poisoning later requests."""
    from yinshi.main import create_app
    from yinshi.tenant import TenantDatabaseTemporarilyUnavailable

    principal = _principal(tmp_path)
    app = create_app(mode="worker", worker_principal=principal)

    @app.get("/api/temporary-storage-test")
    async def temporary_storage_test() -> None:
        raise TenantDatabaseTemporarilyUnavailable(
            "Tenant database storage is temporarily unavailable"
        )

    dispatcher = WorkerHttpDispatcher(app=app, principal=principal)
    response = await dispatcher.request(
        method="GET",
        path="/api/temporary-storage-test",
        body=None,
    )
    subsequent = await dispatcher.request(method="GET", path="/api/repos", body=None)

    assert response.status_code == 503
    assert response.body == {"detail": "Tenant storage is temporarily unavailable"}
    assert response.content_type == "application/json"
    assert subsequent.status_code == 200


@pytest.mark.asyncio
async def test_worker_shared_storage_budget_returns_before_transport_timeout(
    tmp_path: Path,
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route prechecks and work share one budget below the worker timeout."""
    from yinshi.api import deps
    from yinshi.main import create_app

    class TemporaryOperationalError(Exception):
        pass

    @contextmanager
    def connection(_request):
        from yinshi.db import get_db

        with get_db() as database:
            yield database

    principal = _principal(tmp_path)
    app = create_app(mode="worker", worker_principal=principal)
    first_calls = 0

    @app.get("/api/storage-budget-test")
    async def storage_budget_test(request: Request) -> None:
        def precheck(_database):
            nonlocal first_calls
            first_calls += 1
            if first_calls == 1:
                raise TemporaryOperationalError("disk I/O error")

        await deps.run_db_operation_for_request(request, precheck)
        await deps.run_db_operation_for_request(
            request,
            lambda _database: (_ for _ in ()).throw(TemporaryOperationalError("disk I/O error")),
        )

    monkeypatch.setattr(deps, "get_db_for_request", connection)
    monkeypatch.setattr(
        "yinshi.tenant._load_sqlcipher_module",
        lambda: SimpleNamespace(OperationalError=TemporaryOperationalError),
    )
    monkeypatch.setattr(deps, "_TENANT_DB_REQUEST_RETRY_BUDGET_SECONDS", 0.03)
    monkeypatch.setattr(deps, "_TENANT_DB_RETRY_DELAY_SECONDS", 0.015)
    monkeypatch.setattr(deps, "_TENANT_DB_RETRY_DELAY_MAX_SECONDS", 0.015)
    dispatcher = WorkerHttpDispatcher(app=app, principal=principal)
    # Finish execution activation before measuring the foreground retry budget.
    activated = await dispatcher.request(method="GET", path="/api/repos", body=None)
    assert activated.status_code == 200
    started = asyncio.get_running_loop().time()

    response = await dispatcher.request(method="GET", path="/api/storage-budget-test", body=None)

    elapsed = asyncio.get_running_loop().time() - started
    assert response.status_code == 503
    assert elapsed < 0.5
    assert first_calls == 2


@pytest.mark.asyncio
async def test_worker_history_responses_stay_bounded_above_legacy_limit(
    tmp_path: Path,
    db: sqlite3.Connection,
) -> None:
    """Chunked history should keep every worker response below one MiB."""
    from yinshi.main import create_app
    from yinshi.tenant import get_user_db

    principal = _principal(tmp_path)
    Path(principal.tenant.data_dir).mkdir(mode=0o700, parents=True)
    session_id = "a" * 32
    with get_user_db(principal.tenant) as user_db:
        user_db.execute(
            "INSERT INTO repos (id, name, root_path) VALUES (?, 'repo', ?)",
            ("b" * 32, str(tmp_path / "repo")),
        )
        user_db.execute(
            "INSERT INTO workspaces (id, repo_id, name, branch, path) "
            "VALUES (?, ?, 'workspace', 'main', ?)",
            ("c" * 32, "b" * 32, str(tmp_path / "workspace")),
        )
        user_db.execute(
            "INSERT INTO sessions (id, workspace_id) VALUES (?, ?)",
            (session_id, "c" * 32),
        )
        for index in range(36):
            user_db.execute(
                "INSERT INTO messages "
                "(id, session_id, role, content, full_message) "
                "VALUES (?, ?, 'assistant', ?, ?)",
                (
                    f"{index + 1:032x}",
                    session_id,
                    f"done-{index}",
                    "x" * 40_000,
                ),
            )
        user_db.commit()

    dispatcher = WorkerHttpDispatcher(
        app=create_app(mode="worker", worker_principal=principal),
        principal=principal,
    )
    with pytest.raises(RuntimeError, match="response exceeded the size limit"):
        await dispatcher.request(
            method="GET",
            path=f"/api/sessions/{session_id}/messages",
            body=None,
        )

    bundle = await dispatcher.request(
        method="GET",
        path=f"/api/sessions/{session_id}/messages/bundle",
        body=None,
    )
    assert bundle.status_code == 200
    assert len(json.dumps(bundle.body).encode("utf-8")) < 1_048_576
    assert bundle.body["message_count"] == 36
    assert bundle.body["next_cursor"] is None

    page = await dispatcher.request(
        method="GET",
        path=f"/api/sessions/{session_id}/messages/page",
        body=None,
    )
    assert page.status_code == 200
    assert len(json.dumps(page.body).encode("utf-8")) < 1_048_576
    assert len(page.body["messages"]) == 36

    message_id = page.body["messages"][0]["id"]
    offset = 0
    reconstructed: list[str] = []
    while True:
        chunk = await dispatcher.request(
            method="GET",
            path=f"/api/sessions/{session_id}/messages/{message_id}/field",
            query={"name": "full_message", "offset": str(offset)},
            body=None,
        )
        assert chunk.status_code == 200
        assert len(json.dumps(chunk.body).encode("utf-8")) < 1_048_576
        reconstructed.append(chunk.body["value"])
        next_offset = chunk.body["next_offset"]
        if next_offset is None:
            break
        assert next_offset > offset
        offset = next_offset
    assert "".join(reconstructed) == "x" * 40_000

    session = await dispatcher.request(
        method="GET",
        path=f"/api/sessions/{session_id}",
        body=None,
    )
    assert session.status_code == 200
    assert session.body["id"] == session_id


def test_worker_principal_rejects_short_secret(tmp_path: Path) -> None:
    """An internal worker bearer must have enough entropy for process isolation."""
    data_directory = tmp_path / "worker-user"

    try:
        WorkerPrincipal(
            tenant=TenantContext(
                user_id="worker-user-1",
                email="worker-user@runner.invalid",
                data_dir=str(data_directory),
                db_path=str(data_directory / "yinshi.db"),
            ),
            bearer_token="short",
        )
    except ValueError as error:
        assert "at least 32" in str(error)
    else:
        raise AssertionError("short worker bearer was accepted")
