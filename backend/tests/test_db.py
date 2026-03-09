"""Tests for database initialization and operations."""

import sqlite3

import pytest


def test_init_db_creates_tables(db_path, monkeypatch):
    """init_db should create all required tables."""
    monkeypatch.setenv("DB_PATH", db_path)
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db, get_db

    init_db()
    with get_db() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [t["name"] for t in tables]

        assert "repos" in table_names
        assert "workspaces" in table_names
        assert "sessions" in table_names
        assert "messages" in table_names

    get_settings.cache_clear()


def test_init_db_creates_indexes(db_path, monkeypatch):
    """init_db should create indexes."""
    monkeypatch.setenv("DB_PATH", db_path)
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db, get_db

    init_db()
    with get_db() as conn:
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
        index_names = [i["name"] for i in indexes]

        assert "idx_messages_session" in index_names
        assert "idx_sessions_workspace" in index_names
        assert "idx_workspaces_repo" in index_names

    get_settings.cache_clear()


def test_db_foreign_keys(db_path, monkeypatch):
    """Database should enforce foreign keys."""
    monkeypatch.setenv("DB_PATH", db_path)
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db, get_db

    init_db()
    with get_db() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO workspaces (repo_id, name, branch, path) VALUES (?, ?, ?, ?)",
                ("nonexistent", "test", "branch", "/tmp"),
            )

    get_settings.cache_clear()


def test_db_wal_mode(db_path, monkeypatch):
    """Database should use WAL journal mode."""
    monkeypatch.setenv("DB_PATH", db_path)
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db, get_db

    init_db()
    with get_db() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode[0] == "wal"

    get_settings.cache_clear()
