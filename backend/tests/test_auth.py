"""Tests for authentication module.

Covers session tokens, CSRF, and /auth/me endpoint behavior.
"""

from collections.abc import Generator

import pytest


@pytest.fixture()
def auth_enabled_app(db_path, monkeypatch) -> Generator:
    """Set up an app with auth enabled and a fresh database."""
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db

    init_db()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def auth_disabled_app(db_path, monkeypatch) -> Generator:
    """Set up an app with auth disabled and a fresh database."""
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DISABLE_AUTH", "true")
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db

    init_db()
    yield
    get_settings.cache_clear()


def test_create_and_verify_session_token():
    """Session tokens should be creatable and verifiable."""
    from yinshi.auth import create_session_token, verify_session_token

    token = create_session_token("user@example.com")
    assert isinstance(token, str)
    assert len(token) > 0

    email = verify_session_token(token)
    assert email == "user@example.com"


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

    from yinshi.auth import create_session_token
    from yinshi.main import app

    token = create_session_token("user@example.com")

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
        assert resp.status_code == 401


def test_auth_me_authenticated(auth_enabled_app):
    """GET /auth/me with a valid cookie returns user info."""
    from fastapi.testclient import TestClient

    from yinshi.auth import create_session_token
    from yinshi.main import app

    token = create_session_token("user@example.com")

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

    from yinshi.auth import create_session_token
    from yinshi.main import app

    token = create_session_token("user@example.com")

    with TestClient(app) as client:
        resp = client.post(
            "/api/repos",
            json={"name": "test", "local_path": "/tmp"},
            cookies={"yinshi_session": token},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code != 403
