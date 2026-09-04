"""Root-inclusive thread capacity validation."""

from __future__ import annotations

import pytest


def test_total_thread_capacity_exceeds_descendant_limits(monkeypatch):
    """Root-inclusive total capacity must exceed each descendant limit."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("DB_PATH", "/tmp/yinshi-thread-setting-capacity.db")
    monkeypatch.setenv("THREAD_MAX_ACTIVE_DESCENDANTS", "4")
    monkeypatch.setenv("THREAD_MAX_TOTAL", "4")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(Exception, match="thread_max_total"):
            get_settings()
    finally:
        get_settings.cache_clear()
