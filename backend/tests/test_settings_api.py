"""Tests for API key management endpoints."""

import os
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def tenant_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    """Create a test client with multi-tenant setup and a provisioned user."""
    control_db = str(tmp_path / "control.db")
    user_data_dir = str(tmp_path / "users")
    db_path = str(tmp_path / "legacy.db")

    monkeypatch.setenv("CONTROL_DB_PATH", control_db)
    monkeypatch.setenv("USER_DATA_DIR", user_data_dir)
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")

    from yinshi.config import get_settings
    get_settings.cache_clear()

    from yinshi.db import init_control_db, init_db
    init_db()
    init_control_db()

    # Provision a test user
    from yinshi.services.accounts import resolve_or_create_user
    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="test-google-id",
        email="test@example.com",
        display_name="Test User",
    )

    from yinshi.auth import create_session_token
    token = create_session_token(tenant.user_id)

    from yinshi.main import app
    with TestClient(app) as c:
        # Set the session cookie for all requests
        c.cookies.set("yinshi_session", token)
        yield c

    get_settings.cache_clear()


def test_list_keys_empty(tenant_client):
    """GET /api/settings/keys should return empty list initially."""
    resp = tenant_client.get("/api/settings/keys")
    assert resp.status_code == 200
    assert resp.json() == []


def test_add_and_list_key(tenant_client):
    """POST /api/settings/keys should store a key and list it."""
    resp = tenant_client.post(
        "/api/settings/keys",
        json={"provider": "anthropic", "key": "sk-ant-test-key", "label": "My Key"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider"] == "anthropic"
    assert data["label"] == "My Key"
    assert "key" not in data  # Key value should never be returned

    # List should show it
    resp = tenant_client.get("/api/settings/keys")
    assert len(resp.json()) == 1


def test_delete_key(tenant_client):
    """DELETE /api/settings/keys/:id should remove the key."""
    create_resp = tenant_client.post(
        "/api/settings/keys",
        json={"provider": "minimax", "key": "sk-minimax-test", "label": ""},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    key_id = create_resp.json()["id"]

    resp = tenant_client.delete(
        f"/api/settings/keys/{key_id}",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 204

    # Should be gone
    resp = tenant_client.get("/api/settings/keys")
    assert len(resp.json()) == 0


def test_delete_key_not_found(tenant_client):
    """DELETE /api/settings/keys/:id with bad ID should 404."""
    resp = tenant_client.delete(
        "/api/settings/keys/nonexistent",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 404
