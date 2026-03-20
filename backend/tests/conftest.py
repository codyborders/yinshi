"""Shared pytest fixtures for Yinshi tests."""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DEFAULT_TEST_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
DEFAULT_TEST_SECRET = "test-secret-key"
DEFAULT_TEST_PEPPER = "a" * 64


def _configure_test_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    auth_enabled: bool,
) -> None:
    """Configure a fully isolated test environment for a single test."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", DEFAULT_TEST_PEPPER)
    monkeypatch.setenv("SECRET_KEY", DEFAULT_TEST_SECRET)
    monkeypatch.setenv("ALLOWED_REPO_BASE", str(tmp_path))
    if auth_enabled:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
        monkeypatch.setenv("DISABLE_AUTH", "false")
    else:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")
        monkeypatch.setenv("DISABLE_AUTH", "true")

    from yinshi.config import get_settings

    get_settings.cache_clear()


def _make_auth_client(
    app,
    stack: ExitStack,
    *,
    email: str,
    provider_user_id: str,
) -> TestClient:
    """Provision a tenant user and return a TestClient with cookie + CSRF headers."""
    from yinshi.auth import create_session_token
    from yinshi.services.accounts import resolve_or_create_user

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id=provider_user_id,
        email=email,
        display_name="Test User",
    )

    client = stack.enter_context(TestClient(app))
    client.cookies.set("yinshi_session", create_session_token(tenant.user_id))
    client.headers.update(DEFAULT_TEST_HEADERS)
    setattr(client, "yinshi_tenant", tenant)
    setattr(client, "yinshi_email", email)
    return client


def reset_rate_limiter() -> None:
    """Clear in-memory rate-limit state between targeted tests."""
    from yinshi.main import app

    limiter = getattr(app.state, "limiter", None)
    if limiter is None:
        return
    limiter.reset()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Provide a temporary database path."""
    return str(tmp_path / "test.db")


@pytest.fixture
def db(
    db_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[sqlite3.Connection]:
    """Provide an initialized test database."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("DB_PATH", db_path)

    from yinshi.config import get_settings
    from yinshi.db import get_db, init_db

    init_db()
    with get_db() as conn:
        yield conn
    get_settings.cache_clear()


@pytest.fixture
def noauth_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Create a test client with auth disabled."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)

    from yinshi.config import get_settings
    from yinshi.db import init_db
    from yinshi.main import app

    init_db()
    reset_rate_limiter()
    with TestClient(app) as client:
        yield client

    reset_rate_limiter()
    get_settings.cache_clear()


@pytest.fixture
def client(noauth_client: TestClient) -> TestClient:
    """Backward-compatible alias for the shared no-auth client."""
    return noauth_client


@pytest.fixture
def auth_client_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., TestClient]]:
    """Provision isolated authenticated clients for one or more tenant users."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=True)

    from yinshi.config import get_settings
    from yinshi.db import init_control_db, init_db
    from yinshi.main import app

    init_db()
    init_control_db()
    reset_rate_limiter()

    stack = ExitStack()
    counter = 0

    def build_client(
        email: str | None = None,
        provider_user_id: str | None = None,
    ) -> TestClient:
        nonlocal counter
        counter += 1
        client_email = email or f"user{counter}@example.com"
        identity = provider_user_id or f"test-google-id-{counter}"
        return _make_auth_client(
            app,
            stack,
            email=client_email,
            provider_user_id=identity,
        )

    yield build_client

    stack.close()
    reset_rate_limiter()
    get_settings.cache_clear()


@pytest.fixture
def auth_client(auth_client_factory: Callable[..., TestClient]) -> TestClient:
    """Authenticated TestClient with cookie + CSRF headers preconfigured."""
    return auth_client_factory(email="test@example.com", provider_user_id="test-google-id")


@pytest.fixture
def git_repo(tmp_path: Path) -> str:
    """Create a temporary git repository for testing."""
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()
    subprocess.run(["git", "init", str(repo_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    (repo_path / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    return str(repo_path)
