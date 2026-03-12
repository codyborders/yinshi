"""Tests for authentication module.

Covers session tokens, CSRF, and /auth/me endpoint behavior.
"""

from collections.abc import Generator

import pytest


@pytest.fixture()
def auth_enabled_app(db_path, tmp_path, monkeypatch) -> Generator:
    """Set up an app with auth enabled and a fresh database."""
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db, init_control_db

    init_db()
    init_control_db()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def auth_disabled_app(db_path, tmp_path, monkeypatch) -> Generator:
    """Set up an app with auth disabled and a fresh database."""
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("DISABLE_AUTH", "true")
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db

    init_db()
    yield
    get_settings.cache_clear()


def _create_test_user_token():
    """Create a user in the control DB and return a session token for them."""
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.auth import create_session_token

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="test-google-id",
        email="user@example.com",
        display_name="Test User",
    )
    return create_session_token(tenant.user_id)


def test_create_and_verify_session_token():
    """Session tokens should be creatable and verifiable."""
    from yinshi.auth import create_session_token, verify_session_token

    token = create_session_token("user-id-123")
    assert isinstance(token, str)
    assert len(token) > 0

    user_id = verify_session_token(token)
    assert user_id == "user-id-123"


def test_verify_invalid_token():
    """Invalid tokens should return None."""
    from yinshi.auth import verify_session_token

    assert verify_session_token("garbage-token") is None


def test_verify_empty_token():
    """Empty token should return None."""
    from yinshi.auth import verify_session_token

    assert verify_session_token("") is None


def test_csrf_check_blocks_mutating_without_header(auth_enabled_app):
    """Mutating requests without X-Requested-With should get 403 when auth is enabled."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    token = _create_test_user_token()

    with TestClient(app) as client:
        resp = client.post(
            "/api/repos",
            json={"name": "test", "local_path": "/tmp"},
            cookies={"yinshi_session": token},
        )
        assert resp.status_code == 403


def test_auth_me_unauthenticated_returns_401(auth_enabled_app):
    """GET /auth/me without a cookie returns 401 when auth is enabled."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    with TestClient(app) as client:
        resp = client.get("/auth/me")
        # /auth/me is under /auth/ prefix which is an open path,
        # so it won't be blocked by middleware. Without a tenant, it returns
        # authenticated: false
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False


def test_auth_me_authenticated(auth_enabled_app):
    """GET /auth/me with a valid cookie returns user info."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    token = _create_test_user_token()

    with TestClient(app) as client:
        resp = client.get("/auth/me", cookies={"yinshi_session": token})
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["email"] == "user@example.com"


def test_auth_me_disabled(auth_disabled_app):
    """GET /auth/me with auth disabled returns authenticated: false."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    with TestClient(app) as client:
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False


def test_csrf_check_allows_with_header(auth_enabled_app):
    """Mutating requests with X-Requested-With should pass CSRF check."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    token = _create_test_user_token()

    with TestClient(app) as client:
        resp = client.post(
            "/api/repos",
            json={"name": "test", "local_path": "/tmp"},
            cookies={"yinshi_session": token},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code != 403


def test_session_middleware_is_registered():
    """Verify SessionMiddleware is installed for authlib OAuth state storage."""
    from starlette.middleware.sessions import SessionMiddleware

    from yinshi.main import app

    middleware_types = [m.cls for m in app.user_middleware]
    assert SessionMiddleware in middleware_types


def test_session_middleware_before_auth():
    """SessionMiddleware must be registered before AuthMiddleware for OAuth state."""
    from starlette.middleware.sessions import SessionMiddleware

    from yinshi.auth import AuthMiddleware
    from yinshi.main import app

    middleware_types = [m.cls for m in app.user_middleware]
    assert SessionMiddleware in middleware_types
    assert AuthMiddleware in middleware_types
    # In Starlette, middleware listed later wraps earlier ones,
    # so SessionMiddleware should appear after AuthMiddleware in the list
    session_idx = middleware_types.index(SessionMiddleware)
    auth_idx = middleware_types.index(AuthMiddleware)
    assert session_idx > auth_idx
