"""Tests for database initialization and operations."""

import sqlite3

import pytest


@pytest.fixture(autouse=True)
def disable_auth_for_database_tests(monkeypatch):
    """Database-only tests should use explicit local no-auth mode."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")


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

        from yinshi.db import get_db, init_db

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
        from yinshi.db import get_db, init_db

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

        from yinshi.db import get_db, init_db

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


def test_sessions_table_has_pi_context_version(db):
    """Sessions table should track durable Pi context compatibility."""
    cursor = db.execute("PRAGMA table_info(sessions)")
    columns = [row[1] for row in cursor.fetchall()]
    assert "pi_context_version" in columns


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


def test_required_database_encryption_uses_sqlcipher_and_rejects_wrong_key(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Required mode should encrypt every database and fail closed on key mismatch."""
    from tests.conftest import _configure_test_env
    from yinshi.config import get_settings
    from yinshi.db import (
        DatabaseEncryptionError,
        get_control_db,
        get_db,
        init_control_db,
        init_db,
    )

    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()
    get_settings()

    try:
        init_db()
        init_control_db()
        assert not (tmp_path / "legacy.db").read_bytes().startswith(b"SQLite format 3")
        assert not (tmp_path / "control.db").read_bytes().startswith(b"SQLite format 3")
        with get_db() as database:
            assert database.execute("PRAGMA cipher_version").fetchone()[0]
            assert database.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0
        with get_control_db() as database:
            assert database.execute("PRAGMA cipher_version").fetchone()[0]
            assert database.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0

        monkeypatch.setenv("ENCRYPTION_PEPPER", "bb" * 32)
        get_settings.cache_clear()
        with pytest.raises(DatabaseEncryptionError, match="unlock"):
            with get_db() as database:
                database.execute("SELECT COUNT(*) FROM repos").fetchone()
    finally:
        get_settings.cache_clear()


def test_required_database_encryption_migrates_plaintext_without_data_loss(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Required mode should atomically replace existing plaintext application databases."""
    from tests.conftest import _configure_test_env
    from yinshi.config import get_settings
    from yinshi.db import get_control_db, get_db, init_control_db, init_db

    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    get_settings.cache_clear()
    try:
        init_db()
        init_control_db()
        with get_db() as database:
            database.execute(
                "INSERT INTO repos (id, name, root_path) VALUES (?, ?, ?)",
                ("repo-id", "Retained Repo", "/tmp/retained"),
            )
            database.commit()
        with get_control_db() as database:
            database.execute(
                "INSERT INTO users (id, email) VALUES (?, ?)",
                ("user-id", "retained@example.com"),
            )
            database.commit()
        assert (tmp_path / "legacy.db").read_bytes().startswith(b"SQLite format 3")
        assert (tmp_path / "control.db").read_bytes().startswith(b"SQLite format 3")

        monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
        get_settings.cache_clear()
        init_db()
        init_control_db()

        assert not (tmp_path / "legacy.db").read_bytes().startswith(b"SQLite format 3")
        assert not (tmp_path / "control.db").read_bytes().startswith(b"SQLite format 3")
        with get_db() as database:
            assert (
                database.execute("SELECT name FROM repos WHERE id = ?", ("repo-id",)).fetchone()[0]
                == "Retained Repo"
            )
        with get_control_db() as database:
            assert (
                database.execute("SELECT email FROM users WHERE id = ?", ("user-id",)).fetchone()[0]
                == "retained@example.com"
            )
        assert not list(tmp_path.glob("*.plaintext.*"))
        assert not list(tmp_path.glob("*.encrypted.tmp"))
    finally:
        get_settings.cache_clear()


def test_control_db_migrates_existing_desktop_authorization_requests(
    tmp_path,
    monkeypatch,
):
    """Desktop auth migration should be additive, idempotent, and preserve pending rows."""
    control_path = tmp_path / "control.db"
    monkeypatch.setenv("CONTROL_DB_PATH", str(control_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)

    connection = sqlite3.connect(control_path)
    connection.execute("""
        CREATE TABLE desktop_devices (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            refresh_token_hash TEXT NOT NULL UNIQUE,
            refresh_token_expires_at INTEGER NOT NULL
        )
        """)
    connection.execute("""
        CREATE TABLE desktop_authorization_requests (
            request_id_hash TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            redirect_uri TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            state TEXT NOT NULL
        )
        """)
    connection.execute(
        """
        INSERT INTO desktop_authorization_requests
        (request_id_hash, created_at, expires_at, redirect_uri, code_challenge, state)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("digest", 100, 200, "http://127.0.0.1:43123/auth/desktop/callback", "c" * 43, "state"),
    )
    connection.commit()
    connection.close()

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        init_control_db()
        with get_control_db() as database:
            columns = {
                row[1]
                for row in database.execute("PRAGMA table_info(desktop_authorization_requests)")
            }
            device_columns = {
                row[1] for row in database.execute("PRAGMA table_info(desktop_devices)")
            }
            row = database.execute(
                "SELECT * FROM desktop_authorization_requests WHERE request_id_hash = 'digest'"
            ).fetchone()
            indexes = {
                index[1]
                for index in database.execute("PRAGMA index_list(desktop_authorization_requests)")
            }
        assert {"user_id", "authorization_code_hash", "approved_at", "consumed_at"} <= columns
        assert {"revoked_at", "last_seen_at"} <= device_columns
        assert row is not None
        assert row["redirect_uri"] == "http://127.0.0.1:43123/auth/desktop/callback"
        assert row["user_id"] is None
        assert "idx_desktop_authorization_code_hash" in indexes
    finally:
        get_settings.cache_clear()


def test_control_field_encryption_migrates_existing_pi_settings(tmp_path, monkeypatch):
    """Control DB migration should encrypt existing sensitive settings payloads."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "b" * 64)
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "enabled")
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    try:
        from yinshi.db import get_control_db, init_control_db
        from yinshi.services.user_settings import get_pi_settings

        init_control_db()
        with get_control_db() as db:
            db.execute(
                "INSERT INTO users (id, email) VALUES (?, ?)",
                ("user-1", "user@example.com"),
            )
            db.execute(
                "INSERT INTO user_settings (user_id, pi_settings_json, pi_settings_enabled) "
                "VALUES (?, ?, ?)",
                ("user-1", '{"provider":{"baseUrl":"https://api.example.com"}}', 1),
            )
            db.execute(
                "INSERT INTO pi_configs "
                "(user_id, source_type, source_label, repo_url, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "user-1",
                    "github",
                    "owner/private-config",
                    "https://github.com/owner/private-config.git",
                    "ready",
                ),
            )
            db.commit()

        init_control_db()
        with get_control_db() as db:
            row = db.execute(
                "SELECT pi_settings_json FROM user_settings WHERE user_id = ?",
                ("user-1",),
            ).fetchone()
            pi_config_row = db.execute(
                "SELECT source_label, repo_url FROM pi_configs WHERE user_id = ?",
                ("user-1",),
            ).fetchone()

        assert row is not None
        assert row["pi_settings_json"].startswith("enc:v1:")
        assert "api.example.com" not in row["pi_settings_json"]
        assert get_pi_settings("user-1") == {"provider": {"baseUrl": "https://api.example.com"}}

        from yinshi.services.pi_config import get_pi_config

        assert pi_config_row is not None
        assert pi_config_row["source_label"].startswith("enc:v1:")
        assert "private-config" not in pi_config_row["repo_url"]
        assert get_pi_config("user-1")["repo_url"] == "https://github.com/owner/private-config.git"
    finally:
        get_settings.cache_clear()
