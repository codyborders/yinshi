"""Tests for application configuration."""


def test_default_settings():
    """Settings should have sensible defaults."""
    from yinshi.config import Settings

    settings = Settings()
    assert settings.app_name == "Yinshi"
    assert settings.debug is False
    assert settings.db_path == "yinshi.db"
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
