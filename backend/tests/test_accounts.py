"""Tests for account provisioning and resolution."""

import os
import sqlite3

import pytest


@pytest.fixture
def account_env(tmp_path, monkeypatch):
    """Set up environment for account tests."""
    control_db = str(tmp_path / "control.db")
    user_data_dir = str(tmp_path / "users")
    monkeypatch.setenv("CONTROL_DB_PATH", control_db)
    monkeypatch.setenv("USER_DATA_DIR", user_data_dir)
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("DISABLE_AUTH", "true")
    from yinshi.config import get_settings
    get_settings.cache_clear()

    from yinshi.db import init_control_db
    init_control_db()

    yield {
        "control_db": control_db,
        "user_data_dir": user_data_dir,
        "tmp_path": tmp_path,
    }
    get_settings.cache_clear()


def test_provision_user_creates_directory_and_db(account_env):
    """provision_user should create user data dir and initialize DB."""
    from yinshi.services.accounts import provision_user

    tenant = provision_user("user123", "user@example.com")

    assert os.path.isdir(tenant.data_dir)
    assert os.path.isfile(tenant.db_path)
    assert tenant.user_id == "user123"
    assert tenant.email == "user@example.com"


def test_provision_user_db_has_tables(account_env):
    """Provisioned user DB should have all required tables."""
    from yinshi.services.accounts import provision_user

    tenant = provision_user("user456", "user@example.com")

    conn = sqlite3.connect(tenant.db_path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    conn.close()

    assert "repos" in tables
    assert "workspaces" in tables
    assert "sessions" in tables
    assert "messages" in tables


def test_resolve_or_create_new_user(account_env):
    """resolve_or_create_user should create a new user on first login."""
    from yinshi.services.accounts import resolve_or_create_user

    tenant = resolve_or_create_user(
        provider="google",
        provider_user_id="google-123",
        email="new@example.com",
        display_name="New User",
        avatar_url=None,
        provider_data=None,
    )

    assert tenant.email == "new@example.com"
    assert os.path.isfile(tenant.db_path)


def test_resolve_or_create_existing_user(account_env):
    """resolve_or_create_user should return existing user on second login."""
    from yinshi.services.accounts import resolve_or_create_user

    tenant1 = resolve_or_create_user(
        provider="google",
        provider_user_id="google-456",
        email="existing@example.com",
        display_name="Existing",
    )

    tenant2 = resolve_or_create_user(
        provider="google",
        provider_user_id="google-456",
        email="existing@example.com",
        display_name="Existing",
    )

    assert tenant1.user_id == tenant2.user_id
    assert tenant1.db_path == tenant2.db_path


def test_resolve_links_new_provider_same_email(account_env):
    """resolve_or_create_user should link new provider to existing account by email."""
    from yinshi.services.accounts import resolve_or_create_user
    from yinshi.db import get_control_db

    # First login via Google
    tenant1 = resolve_or_create_user(
        provider="google",
        provider_user_id="google-789",
        email="shared@example.com",
        display_name="Shared User",
    )

    # Second login via GitHub with same email
    tenant2 = resolve_or_create_user(
        provider="github",
        provider_user_id="github-101",
        email="shared@example.com",
        display_name="Shared User",
    )

    assert tenant1.user_id == tenant2.user_id

    # Should have two oauth_identities
    with get_control_db() as db:
        identities = db.execute(
            "SELECT * FROM oauth_identities WHERE user_id = ?",
            (tenant1.user_id,),
        ).fetchall()
        assert len(identities) == 2
        providers = {r["provider"] for r in identities}
        assert providers == {"google", "github"}


def test_control_db_has_users_table(account_env):
    """Control DB should have users and oauth_identities tables."""
    from yinshi.db import get_control_db

    with get_control_db() as db:
        tables = [r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]

    assert "users" in tables
    assert "oauth_identities" in tables
    assert "api_keys" in tables
