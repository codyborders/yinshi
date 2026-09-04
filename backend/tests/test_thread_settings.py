"""Thread limit settings validation tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def settings_env(monkeypatch):
    """Provide a minimal valid settings environment for thread limit tests."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("DB_PATH", "/tmp/yinshi-thread-settings.db")
    from yinshi.config import get_settings

    get_settings.cache_clear()
    yield get_settings
    get_settings.cache_clear()


def test_thread_limit_settings_reject_nonpositive_values(settings_env, monkeypatch):
    """Zero or negative thread limit settings fail settings construction."""
    get_settings = settings_env

    cases = (
        "THREAD_MAX_DEPTH",
        "THREAD_MAX_DIRECT_CHILDREN",
        "THREAD_MAX_ACTIVE_DESCENDANTS",
        "THREAD_MAX_TOTAL",
    )
    for env_name in cases:
        monkeypatch.setenv(env_name, "0")
        get_settings.cache_clear()
        with pytest.raises(Exception, match="thread"):
            get_settings()
        monkeypatch.delenv(env_name)
        get_settings.cache_clear()


def test_thread_limit_settings_enforce_coherent_hard_bounds(settings_env, monkeypatch):
    """Total-thread and depth limits stay inside coherent hard bounds."""
    get_settings = settings_env

    monkeypatch.setenv("THREAD_MAX_ACTIVE_DESCENDANTS", "4")
    monkeypatch.setenv("THREAD_MAX_TOTAL", "2")
    get_settings.cache_clear()
    with pytest.raises(Exception, match="thread_max_total"):
        get_settings()

    monkeypatch.delenv("THREAD_MAX_TOTAL")
    monkeypatch.setenv("THREAD_MAX_DEPTH", "33")
    get_settings.cache_clear()
    with pytest.raises(Exception, match="thread_max_depth"):
        get_settings()
    get_settings.cache_clear()
