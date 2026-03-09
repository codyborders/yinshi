"""Tests for authentication module."""


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

    result = verify_session_token("garbage-token")
    assert result is None


def test_verify_empty_token():
    """Empty token should return None."""
    from yinshi.auth import verify_session_token

    result = verify_session_token("")
    assert result is None


def test_csrf_check_blocks_mutating_without_header(db_path, monkeypatch):
    """Mutating requests without X-Requested-With should get 403 when auth is enabled."""
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db

    init_db()

    from yinshi.auth import create_session_token
    from yinshi.main import app
    from fastapi.testclient import TestClient

    token = create_session_token("user@example.com")

    with TestClient(app) as client:
        # POST without X-Requested-With header should be blocked
        resp = client.post(
            "/api/repos",
            json={"name": "test", "local_path": "/tmp"},
            cookies={"yinshi_session": token},
        )
        assert resp.status_code == 403

    get_settings.cache_clear()


def test_csrf_check_allows_with_header(db_path, monkeypatch):
    """Mutating requests with X-Requested-With should pass CSRF check."""
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db

    init_db()

    from yinshi.auth import create_session_token
    from yinshi.main import app
    from fastapi.testclient import TestClient

    token = create_session_token("user@example.com")

    with TestClient(app) as client:
        # POST with X-Requested-With header should pass CSRF (may fail for other reasons)
        resp = client.post(
            "/api/repos",
            json={"name": "test", "local_path": "/tmp"},
            cookies={"yinshi_session": token},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        # Should not be 403 (CSRF) - may be 400 for invalid path
        assert resp.status_code != 403

    get_settings.cache_clear()
