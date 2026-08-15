"""Tests for authentication module.

Covers session tokens, CSRF, /auth/me endpoint behavior, and OAuth
callback error handling. The callback tests verify that OAuth errors,
network failures, and provisioning errors redirect to /login with an
error code instead of returning a bare 500 Internal Server Error.
"""

import asyncio
import sqlite3
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from authlib.integrations.starlette_client import OAuthError

from tests.conftest import _configure_test_env, reset_rate_limiter


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
    from yinshi.auth import create_session_token
    from yinshi.services.accounts import resolve_or_create_user

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="test-google-id",
        email="user@example.com",
        display_name="Test User",
    )
    return create_session_token(tenant.user_id)


def _create_test_user():
    """Create and return a tenant user in the control DB."""
    from yinshi.services.accounts import resolve_or_create_user

    return resolve_or_create_user(
        provider="google",
        provider_user_id="test-google-id",
        email="user@example.com",
        display_name="Test User",
    )


def _configure_github_app_settings(tmp_path, monkeypatch) -> None:
    """Configure minimal GitHub App settings for route tests."""
    private_key_path = tmp_path / "github-app.pem"
    private_key_path.write_text("test-private-key", encoding="utf-8")
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(private_key_path))
    monkeypatch.setenv("GITHUB_APP_SLUG", "yinshi-dev")
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "github-app-client-id")
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "github-app-client-secret")
    monkeypatch.setenv(
        "GITHUB_APP_USER_CALLBACK_URL",
        "http://testserver/auth/github/install/verify",
    )

    from yinshi.config import get_settings

    get_settings.cache_clear()


def test_legacy_login_prefers_configured_github_oauth(
    auth_enabled_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider-neutral login should prefer configured GitHub OAuth."""
    monkeypatch.setenv("GITHUB_CLIENT_ID", "github-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "github-client-secret")
    monkeypatch.setenv(
        "GITHUB_REDIRECT_URI",
        "http://testserver/auth/callback/github",
    )

    from fastapi.testclient import TestClient

    from yinshi.config import get_settings
    from yinshi.main import app

    get_settings.cache_clear()
    with TestClient(app) as client:
        response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/auth/login/github"


def test_legacy_login_falls_back_to_configured_google_oauth(
    auth_enabled_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider-neutral login should retain configured Google OAuth."""
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_CLIENT_SECRET", raising=False)

    from fastapi.testclient import TestClient

    from yinshi.config import get_settings
    from yinshi.main import app

    get_settings.cache_clear()
    with TestClient(app) as client:
        response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/auth/login/google"


def test_legacy_login_fails_closed_without_oauth_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider-neutral login should fail with a fixed configuration response."""
    from types import SimpleNamespace

    from yinshi.api import auth_routes

    monkeypatch.setattr(
        auth_routes,
        "get_settings",
        lambda: SimpleNamespace(
            github_client_id=None,
            github_client_secret=None,
            google_client_id=None,
            google_client_secret=None,
        ),
    )

    response = asyncio.run(auth_routes.login_redirect(MagicMock()))

    assert response.status_code == 503
    assert response.body == b'{"error":"OAuth is not configured"}'


def test_create_and_verify_session_token(tmp_path, monkeypatch):
    """Session tokens should be creatable and verifiable."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=True)

    from yinshi.auth import create_session_token, verify_session_token
    from yinshi.db import init_control_db, init_db

    init_db()
    init_control_db()
    tenant = _create_test_user()
    token = create_session_token(tenant.user_id)
    assert isinstance(token, str)
    assert len(token) > 0

    user_id = verify_session_token(token)
    assert user_id == tenant.user_id


def test_verify_invalid_token(tmp_path, monkeypatch):
    """Invalid tokens should return None."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=True)

    from yinshi.auth import verify_session_token

    assert verify_session_token("garbage-token") is None


def test_auth_disabled_requires_explicit_disable_flag(monkeypatch):
    """Missing OAuth settings must not silently disable request authentication."""
    from yinshi.auth import auth_disabled
    from yinshi.config import Settings

    settings = Settings(disable_auth=False, google_client_id="", github_client_id="")
    monkeypatch.setattr("yinshi.auth.get_settings", lambda: settings)

    assert auth_disabled() is False


def test_disabled_user_session_is_rejected(auth_enabled_app) -> None:
    """An account status change should invalidate every existing session."""
    from yinshi.auth import create_session_token, verify_session_token
    from yinshi.db import get_control_db
    from yinshi.services.accounts import resolve_or_create_user

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="disabled-session-user",
        email="disabled-session@example.com",
        display_name="Disabled Session User",
    )
    token = create_session_token(tenant.user_id)
    assert verify_session_token(token) == tenant.user_id

    with get_control_db() as db:
        db.execute("UPDATE users SET status = 'disabled' WHERE id = ?", (tenant.user_id,))
        db.commit()

    assert verify_session_token(token) is None


def test_verify_empty_token(tmp_path, monkeypatch):
    """Empty token should return None."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=True)

    from yinshi.auth import verify_session_token

    assert verify_session_token("") is None


def test_old_format_session_token_is_rejected(tmp_path, monkeypatch):
    """Legacy string-only session tokens should be invalid after the hard cutover."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=True)

    from itsdangerous import URLSafeTimedSerializer

    from yinshi.auth import verify_session_token
    from yinshi.config import get_settings
    from yinshi.db import init_control_db, init_db

    init_db()
    init_control_db()
    tenant = _create_test_user()
    serializer = URLSafeTimedSerializer(get_settings().secret_key)
    legacy_token = serializer.dumps(tenant.user_id, salt="yinshi-session")

    assert verify_session_token(legacy_token) is None


def test_csrf_check_blocks_mutating_without_header(auth_enabled_app):
    """Mutating requests without X-Requested-With should get 403 when auth is enabled."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    token = _create_test_user_token()

    with TestClient(app) as client:
        client.cookies.set("yinshi_session", token)
        resp = client.post(
            "/api/repos",
            json={"name": "test", "local_path": "/tmp"},
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
        assert "auth_disabled" not in data


def test_auth_me_authenticated(auth_enabled_app):
    """GET /auth/me with a valid cookie returns user info."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    token = _create_test_user_token()

    with TestClient(app) as client:
        client.cookies.set("yinshi_session", token)
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["email"] == "user@example.com"


def test_auth_me_disabled(auth_disabled_app):
    """GET /auth/me reports explicit no-auth mode."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    with TestClient(app) as client:
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False
        assert data["auth_disabled"] is True


def test_csrf_check_allows_with_header(auth_enabled_app):
    """Mutating requests with X-Requested-With should pass CSRF check."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    token = _create_test_user_token()

    with TestClient(app) as client:
        client.cookies.set("yinshi_session", token)
        resp = client.post(
            "/api/repos",
            json={"name": "test", "local_path": "/tmp"},
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


def test_callback_google_oauth_error_redirects(auth_enabled_app, caplog):
    """Google OAuth error (user denied consent, expired code) redirects to /login, not 500."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    mock_token_exchange = AsyncMock(
        side_effect=OAuthError(
            error="access_denied",
            description="upstream-body-secret-marker",
        ),
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

    assert "upstream-body-secret-marker" not in caplog.text


def test_callback_google_network_error_redirects(auth_enabled_app, caplog):
    """Network error during Google token exchange redirects to /login, not 500."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    mock_token_exchange = AsyncMock(
        side_effect=ConnectionError("upstream-response-secret-marker"),
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

    assert "upstream-response-secret-marker" not in caplog.text


def test_callback_google_provisioning_error_redirects(auth_enabled_app):
    """Database error during user provisioning redirects to /login, not 500."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    mock_token = {
        "userinfo": {
            "sub": "google-123",
            "email": "newuser@example.com",
            "email_verified": True,
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
    mock_github.authorize_access_token = AsyncMock(return_value={"access_token": "fake-token"})

    mock_http_client = AsyncMock()
    mock_http_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
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
    mock_github.authorize_access_token = AsyncMock(return_value={"access_token": "fake-token"})

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
    mock_http_client.get = AsyncMock(side_effect=[mock_user_response, mock_emails_response])
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


def test_github_install_redirects_authenticated_user(
    auth_enabled_app,
    tmp_path,
    monkeypatch,
):
    """GET /auth/github/install should redirect authenticated users to GitHub."""
    from fastapi.testclient import TestClient

    _configure_github_app_settings(tmp_path, monkeypatch)

    from yinshi.main import app

    token = _create_test_user_token()

    with TestClient(app) as client:
        client.cookies.set("yinshi_session", token)
        resp = client.get("/auth/github/install", follow_redirects=False)

    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith("https://github.com/apps/yinshi-dev/installations/new?state=")


def test_github_install_callback_stores_installation(
    auth_enabled_app,
    tmp_path,
    monkeypatch,
):
    """GitHub install callback should upsert the installation into the control DB."""
    from fastapi.testclient import TestClient

    _configure_github_app_settings(tmp_path, monkeypatch)

    from yinshi.api.auth_routes import _create_github_install_state
    from yinshi.auth import create_session_token
    from yinshi.db import get_control_db
    from yinshi.main import app
    from yinshi.services.accounts import resolve_or_create_user

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="github-install-user",
        email="install@example.com",
        display_name="Install User",
    )
    state = _create_github_install_state(tenant.user_id)

    with (
        TestClient(app) as client,
        patch(
            "yinshi.api.auth_routes.get_installation_details",
            new=AsyncMock(
                return_value={
                    "account": {"login": "acme", "type": "Organization"},
                    "html_url": "https://github.com/organizations/acme/settings/installations/42",
                    "suspended_at": None,
                }
            ),
        ),
        patch(
            "yinshi.api.auth_routes.verify_github_user_installation",
            new=AsyncMock(return_value=True),
        ),
    ):
        client.cookies.set("yinshi_session", create_session_token(tenant.user_id))
        install_resp = client.get(
            f"/auth/github/install/callback?state={state}&installation_id=42&setup_action=install",
            follow_redirects=False,
        )
        verification_state = parse_qs(urlparse(install_resp.headers["location"]).query)["state"][0]
        resp = client.get(
            "/auth/github/install/verify",
            params={"state": verification_state, "code": "one-time-user-code"},
            follow_redirects=False,
        )
        replay_resp = client.get(
            "/auth/github/install/verify",
            params={"state": verification_state, "code": "replayed-user-code"},
            follow_redirects=False,
        )

    assert install_resp.status_code == 307
    assert resp.status_code == 307
    assert resp.headers["location"] == "/app?github_connected=1"
    assert replay_resp.headers["location"] == "/app?github_connect_error=invalid_state"
    with get_control_db() as db:
        row = db.execute(
            "SELECT installation_id, account_login, account_type, html_url "
            "FROM github_installations WHERE user_id = ?",
            (tenant.user_id,),
        ).fetchone()
    assert row is not None
    assert row["installation_id"] == 42
    assert row["account_login"] == "acme"
    assert row["account_type"] == "Organization"


def test_github_install_callback_rejects_state_user_mismatch(
    auth_enabled_app,
    tmp_path,
    monkeypatch,
):
    """GitHub install callback should reject state tokens from a different user session."""
    from fastapi.testclient import TestClient

    _configure_github_app_settings(tmp_path, monkeypatch)

    from yinshi.api.auth_routes import _create_github_install_state
    from yinshi.auth import create_session_token
    from yinshi.db import get_control_db
    from yinshi.main import app
    from yinshi.services.accounts import resolve_or_create_user

    attacker = resolve_or_create_user(
        provider="google",
        provider_user_id="github-install-attacker",
        email="attacker@example.com",
        display_name="Attacker",
    )
    victim = resolve_or_create_user(
        provider="google",
        provider_user_id="github-install-victim",
        email="victim@example.com",
        display_name="Victim",
    )
    attacker_state = _create_github_install_state(attacker.user_id)

    with TestClient(app) as client:
        client.cookies.set("yinshi_session", create_session_token(victim.user_id))
        response = client.get(
            f"/auth/github/install/callback?state={attacker_state}&installation_id=42&setup_action=install",
            follow_redirects=False,
        )

    assert response.status_code == 307
    assert response.headers["location"] == "/app?github_connect_error=invalid_state"
    with get_control_db() as db:
        rows = db.execute("SELECT * FROM github_installations").fetchall()
    assert rows == []


def test_github_install_callback_relinks_existing_user_repos(
    auth_enabled_app,
    tmp_path,
    monkeypatch,
) -> None:
    """GitHub connect should backfill existing tenant repos for that owner.

    Repos imported before the GitHub App was connected can already have the
    correct canonical URL stored in the user DB while still missing their
    installation id. The callback should refresh those rows immediately so the
    next prompt session can mint runtime git auth without manual re-import.
    """
    from fastapi.testclient import TestClient

    _configure_github_app_settings(tmp_path, monkeypatch)

    from yinshi.api.auth_routes import _create_github_install_state
    from yinshi.auth import create_session_token
    from yinshi.db import get_control_db
    from yinshi.main import app
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.services.github_app import GitHubCloneAccess
    from yinshi.tenant import get_user_db

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="github-install-user-relink",
        email="relink@example.com",
        display_name="Relink User",
    )
    with get_user_db(tenant) as db:
        db.execute(
            """
            INSERT INTO repos (name, remote_url, root_path, installation_id)
            VALUES (?, ?, ?, ?)
            """,
            (
                "devtoolscrape",
                "https://github.com/acme/devtoolscrape",
                str(tmp_path / "users" / "repo-placeholder"),
                None,
            ),
        )
        db.commit()

    state = _create_github_install_state(tenant.user_id)

    with (
        TestClient(app) as client,
        patch(
            "yinshi.api.auth_routes.get_installation_details",
            new=AsyncMock(
                return_value={
                    "account": {"login": "acme", "type": "Organization"},
                    "html_url": "https://github.com/organizations/acme/settings/installations/42",
                    "suspended_at": None,
                }
            ),
        ),
        patch(
            "yinshi.services.workspace.resolve_github_clone_access",
            new=AsyncMock(
                return_value=GitHubCloneAccess(
                    clone_url="https://github.com/acme/devtoolscrape.git",
                    repository_installation_id=42,
                    installation_id=42,
                    access_token="runtime-token",
                    manage_url="https://github.com/organizations/acme/settings/installations/42",
                )
            ),
        ),
        patch(
            "yinshi.api.auth_routes.verify_github_user_installation",
            new=AsyncMock(return_value=True),
        ),
    ):
        client.cookies.set("yinshi_session", create_session_token(tenant.user_id))
        install_resp = client.get(
            f"/auth/github/install/callback?state={state}&installation_id=42&setup_action=install",
            follow_redirects=False,
        )
        verification_state = parse_qs(urlparse(install_resp.headers["location"]).query)["state"][0]
        resp = client.get(
            "/auth/github/install/verify",
            params={"state": verification_state, "code": "one-time-user-code"},
            follow_redirects=False,
        )

    assert install_resp.status_code == 307
    assert resp.status_code == 307
    assert resp.headers["location"] == "/app?github_connected=1"
    with get_control_db() as db:
        installation_row = db.execute(
            "SELECT installation_id FROM github_installations WHERE user_id = ?",
            (tenant.user_id,),
        ).fetchone()
    assert installation_row is not None
    assert installation_row["installation_id"] == 42

    with get_user_db(tenant) as db:
        repo_row = db.execute(
            "SELECT remote_url, installation_id FROM repos WHERE name = ?",
            ("devtoolscrape",),
        ).fetchone()
    assert repo_row is not None
    assert repo_row["remote_url"] == "https://github.com/acme/devtoolscrape.git"
    assert repo_row["installation_id"] == 42


def test_list_github_installations_returns_current_user_rows(
    auth_client,
) -> None:
    """GET /api/github/installations should return only the current user's rows."""
    from yinshi.db import get_control_db

    tenant = getattr(auth_client, "yinshi_tenant")
    with get_control_db() as db:
        db.execute(
            """
            INSERT INTO github_installations (
                user_id, installation_id, account_login, account_type, html_url
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                tenant.user_id,
                9,
                "octo-org",
                "Organization",
                "https://github.com/organizations/octo-org/settings/installations/9",
            ),
        )
        db.commit()

    resp = auth_client.get("/api/github/installations")
    assert resp.status_code == 200
    assert resp.json() == [
        {
            "installation_id": 9,
            "account_login": "octo-org",
            "account_type": "Organization",
            "html_url": "https://github.com/organizations/octo-org/settings/installations/9",
        }
    ]


@pytest.mark.asyncio
async def test_auth_session_revocation_signals_live_connections(auth_enabled_app) -> None:
    """Durable revocation should immediately wake local long-lived connections."""
    from yinshi.auth import create_session_token, get_session_identity, revoke_auth_session
    from yinshi.services.live_auth_sessions import register_live_auth_session

    tenant = _create_test_user()
    token = create_session_token(tenant.user_id)
    identity = get_session_identity(token)
    assert identity is not None
    user_id, auth_session_id = identity
    registration = register_live_auth_session(
        user_id=user_id,
        auth_session_id=auth_session_id,
    )

    try:
        revoke_auth_session(user_id=user_id, auth_session_id=auth_session_id)
        await asyncio.wait_for(registration.event.wait(), timeout=0.2)
    finally:
        registration.close()

    assert registration.event.is_set()


def test_logout_all_revokes_all_sessions_and_clears_cookie(auth_enabled_app) -> None:
    """POST /auth/logout-all should revoke every session for the current user."""
    from fastapi.testclient import TestClient

    from yinshi.auth import create_session_token, verify_session_token
    from yinshi.db import get_control_db
    from yinshi.main import app

    tenant = _create_test_user()
    current_token = create_session_token(tenant.user_id)
    second_token = create_session_token(tenant.user_id)

    with TestClient(app) as client:
        client.cookies.set("yinshi_session", current_token)
        response = client.post("/auth/logout-all", headers={"X-Requested-With": "XMLHttpRequest"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert verify_session_token(current_token) is None
    assert verify_session_token(second_token) is None
    set_cookie = response.headers.get("set-cookie", "")
    assert "yinshi_session=" in set_cookie
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()

    with get_control_db() as db:
        rows = db.execute(
            "SELECT revoked_at FROM auth_sessions WHERE user_id = ? ORDER BY created_at",
            (tenant.user_id,),
        ).fetchall()
    assert len(rows) == 2
    assert all(row["revoked_at"] is not None for row in rows)


def test_logout_revokes_only_current_session_and_clears_cookie(auth_enabled_app) -> None:
    """POST /auth/logout should revoke only the current auth session."""
    from fastapi.testclient import TestClient

    from yinshi.auth import create_session_token, verify_session_token
    from yinshi.db import get_control_db
    from yinshi.main import app

    tenant = _create_test_user()
    current_token = create_session_token(tenant.user_id)
    second_token = create_session_token(tenant.user_id)

    with TestClient(app) as client:
        client.cookies.set("yinshi_session", current_token)
        response = client.post(
            "/auth/logout",
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )

    assert response.status_code == 307
    assert response.headers["location"] == "/"
    assert verify_session_token(current_token) is None
    assert verify_session_token(second_token) == tenant.user_id

    with get_control_db() as db:
        rows = db.execute(
            "SELECT id, revoked_at FROM auth_sessions WHERE user_id = ? ORDER BY created_at",
            (tenant.user_id,),
        ).fetchall()

    assert len(rows) == 2
    revoked_rows = [row for row in rows if row["revoked_at"] is not None]
    active_rows = [row for row in rows if row["revoked_at"] is None]
    assert len(revoked_rows) == 1
    assert len(active_rows) == 1


def test_google_callback_rate_limit_returns_429(auth_enabled_app) -> None:
    """OAuth callbacks should be limited by client IP."""
    from fastapi.testclient import TestClient

    from yinshi.main import app

    reset_rate_limiter()
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
        for _ in range(10):
            response = client.get("/auth/callback/google", follow_redirects=False)
            assert response.status_code == 307
        limited_response = client.get("/auth/callback/google", follow_redirects=False)

    assert limited_response.status_code == 429
    reset_rate_limiter()
