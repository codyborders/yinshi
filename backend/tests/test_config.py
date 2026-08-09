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


def test_settings_ignores_unknown_dotenv_secrets(tmp_path):
    """Sidecar-only dotenv keys should not leak through settings validation errors."""
    from yinshi.config import Settings

    dotenv_path = tmp_path / ".env"
    secret_marker = "synthetic-provider-secret-marker"
    dotenv_path.write_text(f"UNRECOGNIZED_PROVIDER_KEY={secret_marker}\n", encoding="utf-8")

    settings = Settings(_env_file=dotenv_path)

    assert settings.app_name == "Yinshi"
    assert secret_marker not in repr(settings)


def test_no_auth_mode_rejects_non_loopback_bind(monkeypatch):
    """Anonymous development mode must not listen on a remote interface."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("HOST", "0.0.0.0")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="loopback"):
        get_settings()
    get_settings.cache_clear()


def test_no_auth_mode_rejects_container_posture(monkeypatch):
    """Anonymous mode must use the explicit local host-side development posture."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("CONTAINER_ENABLED", "true")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="CONTAINER_ENABLED=false"):
        get_settings()
    get_settings.cache_clear()


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


def test_get_settings_cached(monkeypatch):
    """get_settings should return the same instance."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
    get_settings.cache_clear()


def test_authenticated_mode_requires_an_oauth_provider(monkeypatch):
    """Explicit authenticated mode should fail closed without an OAuth provider."""
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="OAuth provider"):
        get_settings()
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


def test_authenticated_mode_rejects_short_session_secret(monkeypatch):
    """Authenticated deployments should require a 32-character session secret."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.setenv("SECRET_KEY", "s" * 31)
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "b" * 64)

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="at least 32"):
        get_settings()
    get_settings.cache_clear()


def test_authenticated_mode_rejects_low_diversity_session_secret(monkeypatch):
    """A long repeated character should not qualify as a session secret."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.setenv("SECRET_KEY", "s" * 64)
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "b" * 64)

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="distinct characters"):
        get_settings()
    get_settings.cache_clear()


def test_short_encryption_pepper_is_rejected(monkeypatch):
    """ENCRYPTION_PEPPER should fail fast when it is shorter than 32 bytes."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "aa")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        get_settings()
    get_settings.cache_clear()


def test_key_encryption_key_requires_key_id(monkeypatch):
    """Server-managed KEKs should carry a non-empty key id for rotation."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
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
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
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
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "b" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "auto")
    monkeypatch.setenv("DEBUG", "false")

    from yinshi.config import get_settings, tenant_db_encryption_required

    get_settings.cache_clear()
    settings = get_settings()
    assert tenant_db_encryption_required(settings) is True
    get_settings.cache_clear()
