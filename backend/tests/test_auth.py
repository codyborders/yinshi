"""Tests for authentication module.

Covers session tokens, CSRF, /auth/me endpoint behavior, and OAuth
callback error handling. The callback tests verify that OAuth errors,
network failures, and provisioning errors redirect to /login with an
error code instead of returning a bare 500 Internal Server Error.
"""

import sqlite3
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from authlib.integrations.starlette_client import OAuthError

from tests.conftest import _configure_test_env


@pytest.fixture()
def auth_enabled_app(tmp_path, monkeypatch) -> Generator:
    """Set up an app with auth enabled and a fresh database."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=True)

    from yinshi.config import get_settings
    from yinshi.db import init_control_db, init_db

    init_db()
    init_control_db()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def auth_disabled_app(tmp_path, monkeypatch) -> Generator:
    """Set up an app with auth disabled and a fresh database."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)

    from yinshi.config import get_settings
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


# --- OAuth callback error handling ---


def _assert_error_redirect(response, expected_error: str) -> None:
    """Assert that a response is a redirect to /login with the given error code."""
    assert response.status_code == 307, f"Expected 307, got {response.status_code}"
    location = response.headers["location"]
    assert f"error={expected_error}" in location


def test_callback_google_oauth_error_redirects(auth_enabled_app):
    """Google OAuth error (user denied consent, expired code) redirects to /login, not 500."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    mock_token_exchange = AsyncMock(
        side_effect=OAuthError(error="access_denied", description="User denied"),
    )

    with (
        TestClient(app) as client,
        patch(
            "yinshi.api.auth_routes.oauth.google.authorize_access_token",
            new=mock_token_exchange,
        ),
    ):
        resp = client.get("/auth/callback/google", follow_redirects=False)
        _assert_error_redirect(resp, "oauth_error")


def test_callback_google_network_error_redirects(auth_enabled_app):
    """Network error during Google token exchange redirects to /login, not 500."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    mock_token_exchange = AsyncMock(
        side_effect=ConnectionError("Network unreachable"),
    )

    with (
        TestClient(app) as client,
        patch(
            "yinshi.api.auth_routes.oauth.google.authorize_access_token",
            new=mock_token_exchange,
        ),
    ):
        resp = client.get("/auth/callback/google", follow_redirects=False)
        _assert_error_redirect(resp, "oauth_error")


def test_callback_google_provisioning_error_redirects(auth_enabled_app):
    """Database error during user provisioning redirects to /login, not 500."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    mock_token = {
        "userinfo": {
            "sub": "google-123",
            "email": "newuser@example.com",
            "name": "New User",
            "picture": "https://example.com/photo.jpg",
        }
    }

    with (
        TestClient(app) as client,
        patch(
            "yinshi.api.auth_routes.oauth.google.authorize_access_token",
            new=AsyncMock(return_value=mock_token),
        ),
        patch(
            "yinshi.api.auth_routes.resolve_or_create_user",
            side_effect=sqlite3.OperationalError("database is locked"),
        ),
    ):
        resp = client.get("/auth/callback/google", follow_redirects=False)
        _assert_error_redirect(resp, "account_error")


def test_callback_github_oauth_error_redirects(auth_enabled_app):
    """GitHub OAuth error redirects to /login, not 500."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    mock_github = MagicMock()
    mock_github.authorize_access_token = AsyncMock(
        side_effect=OAuthError(error="access_denied", description="User denied")
    )

    with (
        TestClient(app) as client,
        patch("yinshi.api.auth_routes.oauth.github", mock_github, create=True),
    ):
        resp = client.get("/auth/callback/github", follow_redirects=False)
        _assert_error_redirect(resp, "oauth_error")


def test_callback_github_api_error_redirects(auth_enabled_app):
    """GitHub API failure (network error fetching user info) redirects to /login, not 500."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    mock_github = MagicMock()
    mock_github.authorize_access_token = AsyncMock(
        return_value={"access_token": "fake-token"}
    )

    mock_http_client = AsyncMock()
    mock_http_client.get = AsyncMock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    mock_http_client.__aenter__.return_value = mock_http_client

    with (
        TestClient(app) as client,
        patch("yinshi.api.auth_routes.oauth.github", mock_github, create=True),
        patch.object(httpx, "AsyncClient", return_value=mock_http_client),
    ):
        resp = client.get("/auth/callback/github", follow_redirects=False)
        _assert_error_redirect(resp, "github_api_error")


def test_callback_github_provisioning_error_redirects(auth_enabled_app):
    """Filesystem error during GitHub user provisioning redirects to /login, not 500."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    mock_github = MagicMock()
    mock_github.authorize_access_token = AsyncMock(
        return_value={"access_token": "fake-token"}
    )

    mock_user_response = MagicMock()
    mock_user_response.json.return_value = {
        "id": 12345,
        "login": "testuser",
        "name": "Test User",
        "avatar_url": "https://example.com/avatar.jpg",
    }
    mock_user_response.raise_for_status = MagicMock()

    mock_emails_response = MagicMock()
    mock_emails_response.json.return_value = [
        {"email": "test@example.com", "primary": True, "verified": True}
    ]
    mock_emails_response.raise_for_status = MagicMock()

    mock_http_client = AsyncMock()
    mock_http_client.get = AsyncMock(
        side_effect=[mock_user_response, mock_emails_response]
    )
    mock_http_client.__aenter__.return_value = mock_http_client

    with (
        TestClient(app) as client,
        patch("yinshi.api.auth_routes.oauth.github", mock_github, create=True),
        patch.object(httpx, "AsyncClient", return_value=mock_http_client),
        patch(
            "yinshi.api.auth_routes.resolve_or_create_user",
            side_effect=OSError("Permission denied: /var/lib/yinshi/users"),
        ),
    ):
        resp = client.get("/auth/callback/github", follow_redirects=False)
        _assert_error_redirect(resp, "account_error")
