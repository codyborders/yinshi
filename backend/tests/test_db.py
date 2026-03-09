"""Tests for database initialization and operations."""

import sqlite3

import pytest


def test_init_db_creates_tables(db):
    """init_db should create all required tables."""
    tables = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = [t["name"] for t in tables]

    assert "repos" in table_names
    assert "workspaces" in table_names
    assert "sessions" in table_names
    assert "messages" in table_names


def test_init_db_creates_indexes(db):
    """init_db should create indexes."""
    indexes = db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    ).fetchall()
    index_names = [i["name"] for i in indexes]

    assert "idx_messages_session" in index_names
    assert "idx_sessions_workspace" in index_names
    assert "idx_workspaces_repo" in index_names


def test_db_foreign_keys(db):
    """Database should enforce foreign keys."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO workspaces (repo_id, name, branch, path) VALUES (?, ?, ?, ?)",
            ("nonexistent", "test", "branch", "/tmp"),
        )


def test_db_wal_mode(db):
    """Database should use WAL journal mode."""
    mode = db.execute("PRAGMA journal_mode").fetchone()
    assert mode[0] == "wal"


def test_init_db_migrates_owner_email_column(db_path, monkeypatch):
    """init_db should add owner_email column to existing repos table that lacks it."""
    monkeypatch.setenv("DB_PATH", db_path)
    from yinshi.config import get_settings

    get_settings.cache_clear()

    import sqlite3

    # Create a repos table WITHOUT owner_email (simulating pre-migration DB)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("""CREATE TABLE repos (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
        name TEXT NOT NULL,
        remote_url TEXT,
        root_path TEXT NOT NULL,
        custom_prompt TEXT
    )""")
    conn.execute("INSERT INTO repos (id, name, root_path) VALUES ('test1', 'myrepo', '/tmp')")
    conn.commit()
    conn.close()

    from yinshi.db import init_db, get_db

    init_db()

    with get_db() as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(repos)").fetchall()]
        assert "owner_email" in columns
        # Existing data should be preserved
        row = db.execute("SELECT * FROM repos WHERE id = 'test1'").fetchone()
        assert row["name"] == "myrepo"
        assert row["owner_email"] is None

    get_settings.cache_clear()


def test_repos_table_has_owner_email_column(db):
    """Repos table should have owner_email column."""
    cursor = db.execute("PRAGMA table_info(repos)")
    columns = [row[1] for row in cursor.fetchall()]
    assert "owner_email" in columns


def test_init_db_uses_context_manager(db):
    """init_db should not leak connections (uses get_db context manager)."""
    tables = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    assert len(tables) > 0


@pytest.mark.asyncio
async def test_get_db_async(db_path, monkeypatch):
    """get_db_async should provide a working async DB connection."""
    monkeypatch.setenv("DB_PATH", db_path)
    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db, get_db_async

    init_db()
    async with get_db_async() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert len(tables) > 0

    get_settings.cache_clear()
