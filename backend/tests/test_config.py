"""Tests for application configuration."""

import pytest


def test_default_settings():
    """Settings should have sensible defaults."""
    from yinshi.config import Settings

    settings = Settings()
    assert settings.app_name == "Yinshi"
    assert settings.debug is False
    assert settings.db_path == "yinshi.db"
    assert settings.container_enabled is True
    assert settings.port == 8000


def test_settings_from_env(monkeypatch):
    """Settings should read from environment variables."""
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("DB_PATH", "/tmp/test.db")
    monkeypatch.setenv("PORT", "9000")

    from yinshi.config import Settings

    settings = Settings()
    assert settings.debug is True
    assert settings.db_path == "/tmp/test.db"
    assert settings.port == 9000


def test_get_settings_cached():
    """get_settings should return the same instance."""
    from yinshi.config import get_settings

    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
    get_settings.cache_clear()


def test_auth_enabled_requires_explicit_secret_key(monkeypatch):
    """Auth-enabled settings should fail fast without an explicit secret key."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.delenv("SECRET_KEY", raising=False)

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        get_settings()
    get_settings.cache_clear()


def test_short_encryption_pepper_is_rejected(monkeypatch):
    """ENCRYPTION_PEPPER should fail fast when it is shorter than 32 bytes."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "aa")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        get_settings()
    get_settings.cache_clear()


def test_key_encryption_key_requires_key_id(monkeypatch):
    """Server-managed KEKs should carry a non-empty key id for rotation."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "b" * 64)
    monkeypatch.setenv("KEY_ENCRYPTION_KEY_ID", "   ")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="KEY_ENCRYPTION_KEY_ID"):
        get_settings()
    get_settings.cache_clear()


def test_invalid_security_mode_is_rejected(monkeypatch):
    """Security mode environment values should fail fast when misspelled."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "sometimes")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="TENANT_DB_ENCRYPTION"):
        get_settings()
    get_settings.cache_clear()


def test_auto_tenant_db_encryption_is_required_in_authenticated_production(monkeypatch):
    """Auto mode should fail closed for tenant DB encryption in production auth mode."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "b" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "auto")
    monkeypatch.setenv("DEBUG", "false")

    from yinshi.config import get_settings, tenant_db_encryption_required

    get_settings.cache_clear()
    settings = get_settings()
    assert tenant_db_encryption_required(settings) is True
    get_settings.cache_clear()
