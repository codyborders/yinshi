"""Tests for shared API dependency helpers."""

import asyncio
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


def test_github_clone_access_resolver_rejects_noncallable_state() -> None:
    """Configured clone access resolvers must be callable."""
    from yinshi.api.deps import get_github_clone_access_resolver

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(github_clone_access_resolver="invalid"))
    )

    with pytest.raises(RuntimeError, match="github_clone_access_resolver must be callable"):
        get_github_clone_access_resolver(request)


@pytest.mark.asyncio
async def test_run_db_operation_retries_fresh_connection_without_blocking(
    monkeypatch,
) -> None:
    """Temporary SQLCipher I/O retries on a fresh thread-backed connection."""
    from yinshi.api import deps

    class TemporaryOperationalError(Exception):
        pass

    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False
            self.rolled_back = False

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    first = FakeConnection()
    second = FakeConnection()
    pending = [first, second]

    @contextmanager
    def connections(_request):
        connection = pending.pop(0)
        try:
            yield connection
        finally:
            connection.close()

    operation_started = threading.Event()
    release_operation = threading.Event()
    calls = 0

    def operation(connection):
        nonlocal calls
        calls += 1
        if connection is first:
            operation_started.set()
            release_operation.wait(timeout=1)
            raise TemporaryOperationalError("disk I/O error")
        return "recovered"

    monkeypatch.setattr(deps, "get_db_for_request", connections)
    monkeypatch.setattr(
        "yinshi.tenant._load_sqlcipher_module",
        lambda: SimpleNamespace(OperationalError=TemporaryOperationalError),
    )
    monkeypatch.setattr(deps, "_TENANT_DB_RETRY_DELAY_SECONDS", 0.0)

    task = asyncio.create_task(deps.run_db_operation_for_request(object(), operation))
    assert await asyncio.to_thread(operation_started.wait, 1)
    ticked = False

    async def tick() -> None:
        nonlocal ticked
        await asyncio.sleep(0)
        ticked = True

    await tick()
    release_operation.set()

    assert await task == "recovered"
    assert ticked is True
    assert calls == 2
    assert first.rolled_back is True
    assert first.closed is True
    assert second.closed is True


@pytest.mark.asyncio
async def test_run_db_operation_exhaustion_preserves_temporary_cause(
    monkeypatch,
) -> None:
    """A bounded exact disk outage becomes one retryable tenant-storage error."""
    from yinshi.api import deps
    from yinshi.tenant import TenantDatabaseTemporarilyUnavailable

    class TemporaryOperationalError(Exception):
        pass

    class FakeConnection:
        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    @contextmanager
    def connection(_request):
        yield FakeConnection()

    error = TemporaryOperationalError("disk I/O error")
    monkeypatch.setattr(deps, "get_db_for_request", connection)
    monkeypatch.setattr(
        "yinshi.tenant._load_sqlcipher_module",
        lambda: SimpleNamespace(OperationalError=TemporaryOperationalError),
    )
    monkeypatch.setattr(deps, "_TENANT_DB_RETRY_DEADLINE_SECONDS", 0.0)

    with pytest.raises(TenantDatabaseTemporarilyUnavailable) as raised:
        await deps.run_db_operation_for_request(
            object(),
            lambda _database: (_ for _ in ()).throw(error),
        )

    assert raised.value.__cause__ is error


@pytest.mark.asyncio
async def test_run_db_operation_cancellation_interrupts_retry_backoff(
    monkeypatch,
) -> None:
    """Caller cancellation stops asynchronous retry backoff promptly."""
    from yinshi.api import deps

    class TemporaryOperationalError(Exception):
        pass

    @contextmanager
    def connection(_request):
        yield MagicMock()

    backoff_started = asyncio.Event()
    release_backoff = asyncio.Event()

    async def blocked_sleep(_delay: float) -> None:
        backoff_started.set()
        await release_backoff.wait()

    monkeypatch.setattr(deps, "get_db_for_request", connection)
    monkeypatch.setattr(
        "yinshi.tenant._load_sqlcipher_module",
        lambda: SimpleNamespace(OperationalError=TemporaryOperationalError),
    )
    monkeypatch.setattr(deps.asyncio, "sleep", blocked_sleep)
    task = asyncio.create_task(
        deps.run_db_operation_for_request(
            object(),
            lambda _database: (_ for _ in ()).throw(TemporaryOperationalError("disk I/O error")),
        )
    )
    await asyncio.wait_for(backoff_started.wait(), timeout=1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_run_db_operation_cancellation_waits_for_active_attempt(
    monkeypatch,
) -> None:
    """Cancellation does not release callers while a thread can still commit."""
    from yinshi.api import deps

    @contextmanager
    def connection(_request):
        yield MagicMock()

    operation_started = threading.Event()
    release_operation = threading.Event()

    def operation(_database):
        operation_started.set()
        release_operation.wait(timeout=1)
        return "finished"

    monkeypatch.setattr(deps, "get_db_for_request", connection)
    task = asyncio.create_task(deps.run_db_operation_for_request(object(), operation))
    assert await asyncio.to_thread(operation_started.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False

    release_operation.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_run_db_operation_cancellation_consumes_active_attempt_failure(
    monkeypatch,
) -> None:
    """Caller cancellation wins when the active thread later raises."""
    from yinshi.api import deps

    @contextmanager
    def connection(_request):
        yield MagicMock()

    operation_started = threading.Event()
    release_operation = threading.Event()

    def operation(_database):
        operation_started.set()
        release_operation.wait(timeout=1)
        raise RuntimeError("active attempt failed")

    monkeypatch.setattr(deps, "get_db_for_request", connection)
    task = asyncio.create_task(deps.run_db_operation_for_request(object(), operation))
    assert await asyncio.to_thread(operation_started.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False

    release_operation.set()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_get_user_email_returns_email():
    """Should return user_email from request state when present."""
    from yinshi.api.deps import get_user_email

    request = MagicMock()
    request.state.user_email = "user@example.com"
    assert get_user_email(request) == "user@example.com"


def test_get_user_email_returns_none_when_missing():
    """Should return None when user_email is not set on request state."""
    from yinshi.api.deps import get_user_email

    request = MagicMock(spec=[])
    request.state = MagicMock(spec=[])
    assert get_user_email(request) is None


def test_check_owner_allows_matching_emails():
    """Should not raise when owner and user emails match."""
    from yinshi.api.deps import check_owner

    check_owner("user@example.com", "user@example.com")


def test_check_owner_raises_on_mismatch():
    """Should raise 403 when owner and user emails differ."""
    from yinshi.api.deps import check_owner

    with pytest.raises(HTTPException) as exc_info:
        check_owner("owner@example.com", "other@example.com")
    assert exc_info.value.status_code == 403


def test_check_owner_allows_none_user():
    """Should not raise when user_email is None (auth disabled)."""
    from yinshi.api.deps import check_owner

    check_owner("owner@example.com", None)


def test_check_owner_allows_none_owner():
    """Should not raise when owner_email is None."""
    from yinshi.api.deps import check_owner

    check_owner(None, "user@example.com")


def test_check_workspace_owner_404_when_missing():
    """Missing workspaces should raise 404 instead of silently passing."""
    from yinshi.api.deps import check_workspace_owner

    request = SimpleNamespace(state=SimpleNamespace())
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        check_workspace_owner(db, "missing-workspace", request)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Workspace not found"


def test_check_session_owner_404_when_missing():
    """Missing sessions should raise 404 instead of silently passing."""
    from yinshi.api.deps import check_session_owner

    request = SimpleNamespace(state=SimpleNamespace())
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        check_session_owner(db, "missing-session", request)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Session not found"
