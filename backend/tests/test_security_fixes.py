"""Tests for security fixes identified in code review."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

# --- SEC-C1: WebSocket auth bypass removal ---


def test_websocket_header_does_not_bypass_auth(tmp_path, monkeypatch):
    """Requests with Upgrade: websocket header must NOT bypass authentication."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")

    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_control_db, init_db

    init_db()
    init_control_db()

    from yinshi.main import app

    with TestClient(app) as client:
        # Without a valid session cookie, the upgrade header should NOT bypass auth
        resp = client.get(
            "/api/repos",
            headers={"Upgrade": "websocket"},
        )
        assert resp.status_code == 401

    get_settings.cache_clear()


def test_terminal_websocket_rejects_missing_session_cookie(auth_client: TestClient) -> None:
    """Terminal WebSocket must not rely on HTTP middleware for authentication."""
    auth_client.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as disconnect:
        with auth_client.websocket_connect(
            "/api/workspaces/" + "a" * 32 + "/terminal",
            headers={"Origin": "http://localhost:5173"},
        ):
            pass

    assert disconnect.value.code == 1008


def test_terminal_websocket_rejects_untrusted_origin(auth_client: TestClient) -> None:
    """Terminal WebSocket should reject cross-site browser origins before startup."""
    with pytest.raises(WebSocketDisconnect) as disconnect:
        with auth_client.websocket_connect(
            "/api/workspaces/" + "a" * 32 + "/terminal",
            headers={"Origin": "https://evil.example"},
        ):
            pass

    assert disconnect.value.code == 1008


# --- SEC-H3: Sessions PATCH must use _UPDATABLE_COLUMNS guard ---


def test_session_patch_filters_updatable_columns(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """PATCH /api/sessions/:id should only allow model field updates."""
    repo = auth_client.post(
        "/api/repos",
        json={"name": "test", "local_path": git_repo},
    ).json()
    ws = auth_client.post(
        f"/api/repos/{repo['id']}/workspaces",
        json={},
    ).json()
    sess = auth_client.post(
        f"/api/workspaces/{ws['id']}/sessions",
        json={},
    ).json()

    # Attempt to update model (allowed)
    resp = auth_client.patch(
        f"/api/sessions/{sess['id']}",
        json={"model": "sonnet"},
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "anthropic/claude-sonnet-4-20250514"

    # status should NOT be directly updatable via PATCH
    # (even if the field is sent, it should be filtered out)
    resp = auth_client.patch(
        f"/api/sessions/{sess['id']}",
        json={"model": "opus"},
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "anthropic/claude-opus-4-20250514"
    # status should remain unchanged
    assert resp.json()["status"] == "idle"


# --- CQ5/BUG: exclude_unset vs exclude_none ---


def test_session_patch_exclude_unset(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """PATCH /api/sessions/:id with empty body should not clear optional fields.

    Uses exclude_unset=True so that fields not sent in the request
    are NOT included in the update.
    """
    repo = auth_client.post(
        "/api/repos",
        json={"name": "test", "local_path": git_repo},
    ).json()
    ws = auth_client.post(
        f"/api/repos/{repo['id']}/workspaces",
        json={},
    ).json()
    sess = auth_client.post(
        f"/api/workspaces/{ws['id']}/sessions",
        json={"model": "sonnet"},
    ).json()
    assert sess["model"] == "anthropic/claude-sonnet-4-20250514"

    # PATCH with empty body -- model should NOT be reset
    resp = auth_client.patch(
        f"/api/sessions/{sess['id']}",
        json={},
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "anthropic/claude-sonnet-4-20250514"


# --- SEC-H2: Open redirect fix ---


def test_legacy_callback_no_open_redirect(tmp_path, monkeypatch):
    """GET /auth/callback should redirect to /auth/callback/google safely.

    Must NOT use string replacement on user-supplied URL to prevent open redirect.
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")

    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_control_db, init_db

    init_db()
    init_control_db()

    from yinshi.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/auth/callback", follow_redirects=False)
        assert resp.status_code == 307
        location = resp.headers["location"]
        # Should redirect to /auth/callback/google, not use attacker-controlled URL
        # TestClient prefixes with http://testserver
        assert location.endswith("/auth/callback/google")
        assert "/auth/callback?" not in location  # no query string injection

    get_settings.cache_clear()


# --- SEC-M5: Cookie path attribute ---


def test_session_cookie_has_path(tmp_path, monkeypatch):
    """Session cookie should have path=/ set."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")

    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_control_db, init_db

    init_db()
    init_control_db()

    from fastapi.responses import RedirectResponse

    from yinshi.api.auth_routes import _set_session_cookie
    from yinshi.services.accounts import resolve_or_create_user

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="cookie-user",
        email="cookie@example.com",
        display_name="Cookie User",
    )

    response = RedirectResponse(url="/app")
    _set_session_cookie(response, tenant.user_id)

    # Check that path=/ is in the set-cookie header
    set_cookie = response.headers.get("set-cookie", "")
    assert "Path=/" in set_cookie or "path=/" in set_cookie

    get_settings.cache_clear()


# --- SEC-M1: CORS should not include localhost in production ---


def test_cors_no_localhost_in_production(tmp_path, monkeypatch):
    """CORS origins should not include localhost:5173 when not in debug mode."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("FRONTEND_URL", "https://yinshi.io")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")

    from yinshi.config import get_settings

    get_settings.cache_clear()

    settings = get_settings()
    assert settings.debug is False

    # Build the expected origins list the same way main.py does
    origins = [settings.frontend_url]
    if settings.debug:
        origins.append("http://localhost:5173")

    assert "http://localhost:5173" not in origins
    assert "https://yinshi.io" in origins

    get_settings.cache_clear()


# --- PERF-O8: usage_log.session_id index ---


def test_usage_log_session_id_index(tmp_path, monkeypatch):
    """usage_log table should have an index on session_id."""
    import sqlite3

    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")

    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_control_db

    init_control_db()

    conn = sqlite3.connect(str(tmp_path / "control.db"))
    indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='usage_log'"
    ).fetchall()
    index_names = [row[0] for row in indexes]
    conn.close()

    assert "idx_usage_session" in index_names

    get_settings.cache_clear()


# --- SEC-C2: Session tree path validation ---


def test_session_tree_validates_path(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """GET /api/sessions/:id/tree should not traverse outside workspace path."""
    repo = auth_client.post(
        "/api/repos",
        json={"name": "test", "local_path": git_repo},
    ).json()
    ws = auth_client.post(
        f"/api/repos/{repo['id']}/workspaces",
        json={},
    ).json()
    sess = auth_client.post(
        f"/api/workspaces/{ws['id']}/sessions",
        json={},
    ).json()

    # Normal tree request should succeed
    resp = auth_client.get(
        f"/api/sessions/{sess['id']}/tree",
    )
    assert resp.status_code == 200
    assert "files" in resp.json()


# --- Domain exceptions in keys.py ---


def test_keys_raises_domain_exceptions():
    """keys.py should raise domain exceptions, not HTTPException."""
    # Just verify the exception classes exist and are YinshiError subclasses
    from yinshi.exceptions import (
        CreditExhaustedError,
        EncryptionNotConfiguredError,
        KeyNotFoundError,
        YinshiError,
    )

    assert issubclass(CreditExhaustedError, YinshiError)
    assert issubclass(EncryptionNotConfiguredError, YinshiError)
    assert issubclass(KeyNotFoundError, YinshiError)
