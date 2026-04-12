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


def test_db_busy_timeout(db):
    """Database connections should have busy_timeout set."""
    timeout = db.execute("PRAGMA busy_timeout").fetchone()
    assert timeout[0] == 5000


def test_init_db_creates_schema_version(db):
    """init_db should create the schema_version table."""
    tables = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchall()
    assert len(tables) == 1

    version = db.execute("SELECT version FROM schema_version").fetchone()
    assert version[0] >= 1


def test_init_db_migrates_owner_email_column(db_path, monkeypatch):
    """init_db should add missing repo metadata columns to an older repos table."""
    monkeypatch.setenv("DB_PATH", db_path)
    from yinshi.config import get_settings

    get_settings.cache_clear()

    try:
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
            assert "installation_id" in columns
            assert "agents_md" in columns
            # Existing data should be preserved
            row = db.execute("SELECT * FROM repos WHERE id = 'test1'").fetchone()
            assert row["name"] == "myrepo"
            assert row["owner_email"] is None
            assert row["installation_id"] is None
            assert row["agents_md"] is None

            # schema_version should be set
            version = db.execute("SELECT version FROM schema_version").fetchone()
            assert version[0] >= 1
    finally:
        get_settings.cache_clear()


def test_init_db_migration_idempotent(db_path, monkeypatch):
    """Running init_db twice should not fail or duplicate schema_version rows."""
    monkeypatch.setenv("DB_PATH", db_path)
    from yinshi.config import get_settings

    get_settings.cache_clear()

    try:
        from yinshi.db import init_db, get_db

        init_db()
        init_db()

        with get_db() as db:
            rows = db.execute("SELECT version FROM schema_version").fetchall()
            assert len(rows) == 1
            assert rows[0][0] >= 1
    finally:
        get_settings.cache_clear()


def test_migrate_updates_existing_version(db_path, monkeypatch):
    """_migrate should replace existing version, never leaving duplicate rows."""
    monkeypatch.setenv("DB_PATH", db_path)
    from yinshi.config import get_settings

    get_settings.cache_clear()

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("""CREATE TABLE repos (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            name TEXT NOT NULL,
            remote_url TEXT,
            root_path TEXT NOT NULL,
            custom_prompt TEXT,
            owner_email TEXT
        )""")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        conn.commit()
        conn.close()

        from yinshi.db import init_db, get_db

        init_db()

        with get_db() as db:
            rows = db.execute("SELECT version FROM schema_version").fetchall()
            assert len(rows) == 1
            assert rows[0][0] >= 1
    finally:
        get_settings.cache_clear()


def test_repos_table_has_owner_email_column(db):
    """Repos table should have all current metadata columns."""
    cursor = db.execute("PRAGMA table_info(repos)")
    columns = [row[1] for row in cursor.fetchall()]
    assert "owner_email" in columns
    assert "installation_id" in columns
    assert "agents_md" in columns


def test_init_control_db_creates_pi_config_tables(tmp_path, monkeypatch):
    """init_control_db should create pi_configs and user_settings tables."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)

    from yinshi.config import get_settings

    get_settings.cache_clear()
    try:
        from yinshi.db import get_control_db, init_control_db

        init_control_db()
        with get_control_db() as db:
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = [row["name"] for row in tables]
            assert "pi_configs" in table_names
            assert "user_settings" in table_names
    finally:
        get_settings.cache_clear()
