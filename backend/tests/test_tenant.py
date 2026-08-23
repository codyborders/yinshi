"""Tests for tenant context and per-user database management."""

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest


@pytest.fixture
def tenant_env(tmp_path, monkeypatch):
    """Set up environment for tenant tests."""
    control_db = str(tmp_path / "control.db")
    user_data_dir = str(tmp_path / "users")
    monkeypatch.setenv("CONTROL_DB_PATH", control_db)
    monkeypatch.setenv("USER_DATA_DIR", user_data_dir)
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    from yinshi.config import get_settings

    get_settings.cache_clear()
    yield {
        "control_db": control_db,
        "user_data_dir": user_data_dir,
        "tmp_path": tmp_path,
    }
    get_settings.cache_clear()


def test_tenant_context_fields():
    """TenantContext should carry user_id, email, data_dir, db_path."""
    from yinshi.tenant import TenantContext

    ctx = TenantContext(
        user_id="abc123",
        email="user@example.com",
        data_dir="/var/lib/yinshi/users/ab/abc123",
        db_path="/var/lib/yinshi/users/ab/abc123/yinshi.db",
    )
    assert ctx.user_id == "abc123"
    assert ctx.email == "user@example.com"
    assert ctx.data_dir.endswith("abc123")
    assert ctx.db_path.endswith("yinshi.db")


def test_user_data_dir_uses_prefix():
    """user_data_dir should use first two chars of user_id as prefix."""
    from yinshi.tenant import user_data_dir

    result = user_data_dir("/var/lib/yinshi/users", "a1b2c3d4")
    assert result == "/var/lib/yinshi/users/a1/a1b2c3d4"


def test_user_data_dir_short_id():
    """user_data_dir should handle short IDs gracefully."""
    from yinshi.tenant import user_data_dir

    result = user_data_dir("/base", "ab")
    assert result == "/base/ab/ab"


def test_validate_user_path_valid():
    """validate_user_path should pass for paths within data_dir."""
    from yinshi.tenant import TenantContext, validate_user_path

    ctx = TenantContext(
        user_id="abc",
        email="u@e.com",
        data_dir="/data/users/ab/abc",
        db_path="/data/users/ab/abc/yinshi.db",
    )
    # Should not raise
    validate_user_path(ctx, "/data/users/ab/abc/repos/myproject")


def test_validate_user_path_rejects_outside():
    """validate_user_path should reject paths outside data_dir."""
    from yinshi.tenant import TenantContext, validate_user_path

    ctx = TenantContext(
        user_id="abc",
        email="u@e.com",
        data_dir="/data/users/ab/abc",
        db_path="/data/users/ab/abc/yinshi.db",
    )
    with pytest.raises(ValueError, match="outside"):
        validate_user_path(ctx, "/data/users/xx/other/repos/hack")


def test_validate_user_path_rejects_traversal():
    """validate_user_path should reject path traversal."""
    from yinshi.tenant import TenantContext, validate_user_path

    ctx = TenantContext(
        user_id="abc",
        email="u@e.com",
        data_dir="/data/users/ab/abc",
        db_path="/data/users/ab/abc/yinshi.db",
    )
    with pytest.raises(ValueError, match="outside"):
        validate_user_path(ctx, "/data/users/ab/abc/../../etc/passwd")


def test_get_user_db_creates_and_returns_connection(tenant_env):
    """get_user_db should open a working SQLite connection."""
    from yinshi.tenant import TenantContext, get_user_db, init_user_db

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "abc123")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)

    ctx = TenantContext(
        user_id="abc123",
        email="u@e.com",
        data_dir=data_dir,
        db_path=db_path,
    )

    init_user_db(db_path)

    with get_user_db(ctx) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t[0] for t in tables]
        assert "repos" in table_names
        assert "workspaces" in table_names
        assert "sessions" in table_names
        assert "messages" in table_names
        assert "prompt_runs" in table_names
        assert "prompt_events" in table_names


def test_init_user_db_schema_no_owner_email(tenant_env):
    """User DB schema should hide owner metadata and include current repo/runtime fields."""
    from yinshi.tenant import init_user_db

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "abc123")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)

    init_user_db(db_path)

    conn = sqlite3.connect(db_path)
    repo_columns = [r[1] for r in conn.execute("PRAGMA table_info(repos)").fetchall()]
    message_columns = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    conn.close()
    assert "owner_email" not in repo_columns
    assert "agents_md" in repo_columns
    assert "turn_status" in message_columns


def test_get_user_db_migrates_existing_user_db(tenant_env):
    """Opening an existing user DB should apply forward migrations."""
    from yinshi.tenant import TenantContext, get_user_db

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "legacy123")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE repos (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            name TEXT NOT NULL,
            remote_url TEXT,
            root_path TEXT NOT NULL,
            custom_prompt TEXT
        )""")
    conn.execute("""CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            full_message TEXT,
            turn_id TEXT
        )""")
    conn.commit()
    conn.close()

    ctx = TenantContext(
        user_id="legacy123",
        email="legacy@example.com",
        data_dir=data_dir,
        db_path=db_path,
    )

    with get_user_db(ctx) as user_db:
        repo_columns = [row[1] for row in user_db.execute("PRAGMA table_info(repos)").fetchall()]
        message_columns = [
            row[1] for row in user_db.execute("PRAGMA table_info(messages)").fetchall()
        ]

    assert "installation_id" in repo_columns
    assert "agents_md" in repo_columns
    assert "turn_status" in message_columns


def test_current_user_schema_skips_reinitialization_on_ordinary_opens(tenant_env, monkeypatch):
    """Current tenant databases must not rerun schema setup for each request."""
    import yinshi.tenant as tenant_module
    from yinshi.tenant import TenantContext, get_user_db, init_user_db

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "current123")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)
    context = TenantContext(
        user_id="current123",
        email="current@example.com",
        data_dir=data_dir,
        db_path=db_path,
    )
    init_user_db(db_path)

    with sqlite3.connect(db_path) as database:
        schema_version = database.execute("PRAGMA user_version").fetchone()[0]
    assert schema_version > 0

    def unexpected_schema_setup(_database: sqlite3.Connection) -> None:
        raise AssertionError("ordinary opens must not rerun tenant schema setup")

    monkeypatch.setattr(tenant_module, "_ensure_user_db_schema", unexpected_schema_setup)
    for _ in range(2):
        with get_user_db(context) as database:
            assert database.execute("SELECT 1").fetchone()[0] == 1


def test_replaced_user_database_at_same_path_is_initialized(tenant_env) -> None:
    """Replacing a current database must invalidate process-local schema state."""
    from yinshi.tenant import TenantContext, get_user_db, init_user_db

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "replaced123")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)
    context = TenantContext(
        user_id="replaced123",
        email="replaced@example.com",
        data_dir=data_dir,
        db_path=db_path,
    )
    init_user_db(db_path)
    for suffix in ("", "-wal", "-shm"):
        candidate = f"{db_path}{suffix}"
        if os.path.exists(candidate):
            os.unlink(candidate)
    sqlite3.connect(db_path).close()

    with get_user_db(context) as database:
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        schema_version = database.execute("PRAGMA user_version").fetchone()[0]

    assert "prompt_runs" in tables
    assert schema_version > 0


def test_unrecognized_version_zero_user_database_fails_closed(tenant_env) -> None:
    """An unrelated unversioned database must not be adopted as tenant storage."""
    from yinshi.tenant import TenantContext, get_user_db

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "unrelated123")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)
    with sqlite3.connect(db_path) as database:
        database.execute("CREATE TABLE unrelated_data (id INTEGER PRIMARY KEY)")
        database.commit()
    context = TenantContext(
        user_id="unrelated123",
        email="unrelated@example.com",
        data_dir=data_dir,
        db_path=db_path,
    )

    with pytest.raises(RuntimeError, match="legacy schema is not recognized"):
        with get_user_db(context):
            pass


def test_incomplete_legacy_user_schema_fails_before_version_stamp(tenant_env) -> None:
    """Legacy anchor names alone must not authorize an incompatible schema."""
    from yinshi.tenant import TenantContext, get_user_db

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "incomplete123")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)
    with sqlite3.connect(db_path) as database:
        database.execute("CREATE TABLE repos (id TEXT PRIMARY KEY)")
        database.execute("CREATE TABLE messages (id TEXT PRIMARY KEY)")
        database.commit()
    context = TenantContext(
        user_id="incomplete123",
        email="incomplete@example.com",
        data_dir=data_dir,
        db_path=db_path,
    )

    with pytest.raises(RuntimeError, match="legacy schema is not recognized"):
        with get_user_db(context):
            pass
    with sqlite3.connect(db_path) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 0


def test_legacy_user_schema_without_primary_keys_fails_closed(tenant_env) -> None:
    """Matching legacy column names must not replace required primary keys."""
    from yinshi.tenant import TenantContext, get_user_db

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "counterfeit123")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)
    with sqlite3.connect(db_path) as database:
        database.execute("""CREATE TABLE repos (
            id TEXT, created_at TEXT, updated_at TEXT, name TEXT,
            remote_url TEXT, root_path TEXT, custom_prompt TEXT
        )""")
        database.execute("""CREATE TABLE messages (
            id TEXT, created_at TEXT, session_id TEXT, role TEXT,
            content TEXT, full_message TEXT, turn_id TEXT
        )""")
        database.commit()
    context = TenantContext(
        user_id="counterfeit123",
        email="counterfeit@example.com",
        data_dir=data_dir,
        db_path=db_path,
    )

    with pytest.raises(RuntimeError, match="legacy schema is not recognized"):
        with get_user_db(context):
            pass
    with sqlite3.connect(db_path) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 0


def test_user_schema_migration_failure_rolls_back_version_and_ddl(tenant_env, monkeypatch) -> None:
    """A failed version-zero migration must not publish partial schema changes."""
    import yinshi.tenant as tenant_module
    from yinshi.tenant import TenantContext, get_user_db, init_user_db

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "rollback123")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)
    init_user_db(db_path)
    with sqlite3.connect(db_path) as database:
        database.execute("PRAGMA user_version = 0")
        database.commit()
    context = TenantContext(
        user_id="rollback123",
        email="rollback@example.com",
        data_dir=data_dir,
        db_path=db_path,
    )

    def fail_migration(database: sqlite3.Connection) -> None:
        database.execute("ALTER TABLE repos ADD COLUMN partial_change TEXT")
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(tenant_module, "_migrate_user_db", fail_migration)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        with get_user_db(context):
            pass

    with sqlite3.connect(db_path) as database:
        version = database.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in database.execute("PRAGMA table_info(repos)").fetchall()}
        prompt_table = database.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'prompt_runs'"
        ).fetchone()
    assert version == 0
    assert "partial_change" not in columns
    assert prompt_table is not None


def test_sqlcipher_connection_uses_driver_row_factory(tenant_env, monkeypatch):
    """SQLCipher connections must use the driver's Row type, not sqlite3.Row."""
    from types import SimpleNamespace

    from yinshi.tenant import _open_sqlcipher_connection

    class FakeConnection:
        def __init__(self) -> None:
            self.row_factory = None
            self.closed = False
            self.statements: list[str] = []

        def execute(self, statement: str):
            self.statements.append(statement)
            return SimpleNamespace(fetchone=lambda: (0,))

        def close(self) -> None:
            self.closed = True

    fake_connection = FakeConnection()
    fake_row_factory = object()
    fake_module = SimpleNamespace(
        connect=lambda _: fake_connection,
        Row=fake_row_factory,
        DatabaseError=RuntimeError,
    )
    monkeypatch.setattr("yinshi.tenant._load_sqlcipher_module", lambda: fake_module)

    conn = _open_sqlcipher_connection(str(tenant_env["tmp_path"] / "cipher.db"), b"1" * 32)

    assert conn is fake_connection
    assert fake_connection.row_factory is fake_row_factory
    assert any(statement.startswith("PRAGMA key") for statement in fake_connection.statements)
    assert not fake_connection.closed


def test_sqlcipher_connection_retries_transient_disk_io(tenant_env, monkeypatch):
    """A transient Sprite disk error should not fail an otherwise valid key."""
    from yinshi.tenant import _open_sqlcipher_connection

    class FakeOperationalError(Exception):
        pass

    class FakeConnection:
        def __init__(self, *, fail_validation: bool) -> None:
            self.fail_validation = fail_validation
            self.closed = False
            self.row_factory = None

        def execute(self, statement: str):
            if statement.startswith("SELECT count") and self.fail_validation:
                raise FakeOperationalError("disk I/O error")
            return SimpleNamespace(fetchone=lambda: (0,))

        def close(self) -> None:
            self.closed = True

    connections = [
        FakeConnection(fail_validation=True),
        FakeConnection(fail_validation=False),
    ]
    fake_module = SimpleNamespace(
        connect=lambda _: connections.pop(0),
        Row=object(),
        DatabaseError=FakeOperationalError,
        OperationalError=FakeOperationalError,
    )
    sleep = MagicMock()
    monkeypatch.setattr("yinshi.tenant._load_sqlcipher_module", lambda: fake_module)
    monkeypatch.setattr("yinshi.tenant.time.sleep", sleep)

    connection = _open_sqlcipher_connection(
        str(tenant_env["tmp_path"] / "cipher.db"),
        b"1" * 32,
    )

    assert connection.closed is False
    assert sleep.call_args_list == [call(0.05)]


def test_sqlcipher_connection_retries_disk_io_during_connect(tenant_env, monkeypatch):
    """A transient connect failure should receive the same bounded retry."""
    from yinshi.tenant import _open_sqlcipher_connection

    class FakeOperationalError(Exception):
        pass

    class FakeConnection:
        row_factory = None

        def execute(self, _statement: str):
            return SimpleNamespace(fetchone=lambda: (0,))

        def close(self) -> None:
            raise AssertionError("successful connection must remain open")

    connection = FakeConnection()
    connect = MagicMock(side_effect=[FakeOperationalError("disk I/O error"), connection])
    fake_module = SimpleNamespace(
        connect=connect,
        Row=object(),
        DatabaseError=FakeOperationalError,
        OperationalError=FakeOperationalError,
    )
    sleep = MagicMock()
    monkeypatch.setattr("yinshi.tenant._load_sqlcipher_module", lambda: fake_module)
    monkeypatch.setattr("yinshi.tenant.time.sleep", sleep)

    opened = _open_sqlcipher_connection(
        str(tenant_env["tmp_path"] / "cipher.db"),
        b"1" * 32,
    )

    assert opened is connection
    assert connect.call_count == 2
    assert sleep.call_args_list == [call(0.05)]


def test_sqlcipher_connection_reports_temporary_storage_after_connect_disk_io(
    tenant_env,
    monkeypatch,
):
    """Persistent exact disk failures must retain their temporary-storage cause."""
    from yinshi.tenant import (
        TenantDatabaseTemporarilyUnavailable,
        _open_sqlcipher_connection,
    )

    class FakeOperationalError(Exception):
        pass

    connect = MagicMock(side_effect=FakeOperationalError("disk I/O error"))
    fake_module = SimpleNamespace(
        connect=connect,
        Row=object(),
        DatabaseError=FakeOperationalError,
        OperationalError=FakeOperationalError,
    )
    sleep = MagicMock()
    monkeypatch.setattr("yinshi.tenant._load_sqlcipher_module", lambda: fake_module)
    monkeypatch.setattr("yinshi.tenant.time.sleep", sleep)

    with pytest.raises(TenantDatabaseTemporarilyUnavailable) as error:
        _open_sqlcipher_connection(
            str(tenant_env["tmp_path"] / "cipher.db"),
            b"1" * 32,
        )

    assert str(error.value) == "Tenant database storage is temporarily unavailable"
    assert isinstance(error.value.__cause__, FakeOperationalError)
    assert connect.call_count == 3
    assert sleep.call_args_list == [call(0.05), call(0.1)]


def test_sqlcipher_connection_does_not_retry_near_match_disk_error(
    tenant_env,
    monkeypatch,
):
    """Only the exact transient driver message should receive a retry."""
    from yinshi.tenant import _open_sqlcipher_connection

    class FakeOperationalError(Exception):
        pass

    connect = MagicMock(side_effect=FakeOperationalError("DISK I/O error"))
    fake_module = SimpleNamespace(
        connect=connect,
        Row=object(),
        DatabaseError=FakeOperationalError,
        OperationalError=FakeOperationalError,
    )
    sleep = MagicMock()
    monkeypatch.setattr("yinshi.tenant._load_sqlcipher_module", lambda: fake_module)
    monkeypatch.setattr("yinshi.tenant.time.sleep", sleep)

    with pytest.raises(RuntimeError, match="configured key"):
        _open_sqlcipher_connection(
            str(tenant_env["tmp_path"] / "cipher.db"),
            b"1" * 32,
        )

    assert connect.call_count == 1
    sleep.assert_not_called()


def test_sqlcipher_connection_reports_temporary_storage_after_persistent_disk_io(
    tenant_env,
    monkeypatch,
):
    """Repeated validation disk failures must retain their temporary-storage cause."""
    from yinshi.tenant import (
        TenantDatabaseTemporarilyUnavailable,
        _open_sqlcipher_connection,
    )

    class FakeOperationalError(Exception):
        pass

    class FakeConnection:
        row_factory = None

        def __init__(self) -> None:
            self.closed = False

        def execute(self, statement: str):
            if statement.startswith("SELECT count"):
                raise FakeOperationalError("disk I/O error")
            return SimpleNamespace(fetchone=lambda: (0,))

        def close(self) -> None:
            self.closed = True

    connections = [FakeConnection(), FakeConnection(), FakeConnection()]
    pending_connections = list(connections)
    fake_module = SimpleNamespace(
        connect=lambda _: pending_connections.pop(0),
        Row=object(),
        DatabaseError=FakeOperationalError,
        OperationalError=FakeOperationalError,
    )
    sleep = MagicMock()
    monkeypatch.setattr("yinshi.tenant._load_sqlcipher_module", lambda: fake_module)
    monkeypatch.setattr("yinshi.tenant.time.sleep", sleep)

    with pytest.raises(TenantDatabaseTemporarilyUnavailable) as error:
        _open_sqlcipher_connection(
            str(tenant_env["tmp_path"] / "cipher.db"),
            b"1" * 32,
        )

    assert isinstance(error.value.__cause__, FakeOperationalError)
    assert all(connection.closed for connection in connections)
    assert sleep.call_args_list == [call(0.05), call(0.1)]


def test_plaintext_migration_preserves_prompt_journal_and_schema(
    tenant_env,
    monkeypatch,
) -> None:
    """SQLCipher migration should retain every schema object and journal row."""
    from yinshi.tenant import _migrate_plaintext_user_database, init_user_db

    database_path = Path(tenant_env["tmp_path"]) / "complete-migration.db"
    init_user_db(str(database_path))
    source = sqlite3.connect(database_path)
    source.execute("INSERT INTO repos (id, name, root_path) VALUES ('repo-1', 'repo', '/repo')")
    source.execute("""INSERT INTO workspaces (id, repo_id, name, branch, path)
           VALUES ('workspace-1', 'repo-1', 'workspace', 'main', '/workspace')""")
    source.execute("INSERT INTO sessions (id, workspace_id) VALUES ('session-1', 'workspace-1')")
    source.execute("""INSERT INTO prompt_runs (id, session_id, idempotency_key, status)
           VALUES ('run-1', 'session-1', 'key-1', 'completed')""")
    source.execute("""INSERT INTO prompt_events (run_id, sequence, event_json)
           VALUES ('run-1', 1, '{"type":"done"}')""")
    source.execute("""INSERT INTO prompt_events (run_id, sequence, event_json)
           VALUES ('run-1', 0, '{"type":"start"}')""")
    source.commit()
    expected_schema = source.execute("""SELECT type, name, tbl_name, sql FROM sqlite_master
           WHERE type IN ('table', 'index', 'trigger') AND name NOT LIKE 'sqlite_%'
           ORDER BY type, name""").fetchall()
    expected_counts = {
        row[0]: source.execute(f'SELECT count(*) FROM "{row[0]}"').fetchone()[0]
        for row in source.execute("""SELECT name FROM sqlite_master
               WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name""").fetchall()
    }
    source.close()

    class ExportConnection:
        def __init__(self, path: str) -> None:
            self.connection = sqlite3.connect(path)
            self.target_path: str | None = None

        def execute(self, statement: str, parameters=()):
            if statement.startswith("ATTACH DATABASE"):
                self.target_path = parameters[0]
                return self.connection.execute("SELECT 1")
            if statement.startswith("SELECT sqlcipher_export"):
                assert self.target_path is not None
                target = sqlite3.connect(self.target_path)
                try:
                    self.connection.backup(target)
                finally:
                    target.close()
                return self.connection.execute("SELECT 1")
            if statement.startswith("DETACH DATABASE"):
                return self.connection.execute("SELECT 1")
            return self.connection.execute(statement, parameters)

        def close(self) -> None:
            self.connection.close()

    fake_sqlcipher = SimpleNamespace(
        connect=lambda path: ExportConnection(path),
        Row=sqlite3.Row,
        DatabaseError=sqlite3.DatabaseError,
    )
    monkeypatch.setattr("yinshi.tenant._load_sqlcipher_module", lambda: fake_sqlcipher)
    monkeypatch.setattr(
        "yinshi.tenant._open_sqlcipher_connection",
        lambda path, _key: sqlite3.connect(path),
    )

    _migrate_plaintext_user_database(str(database_path), b"k" * 32)

    migrated = sqlite3.connect(database_path)
    actual_schema = migrated.execute("""SELECT type, name, tbl_name, sql FROM sqlite_master
           WHERE type IN ('table', 'index', 'trigger') AND name NOT LIKE 'sqlite_%'
           ORDER BY type, name""").fetchall()
    actual_counts = {
        row[0]: migrated.execute(f'SELECT count(*) FROM "{row[0]}"').fetchone()[0]
        for row in migrated.execute("""SELECT name FROM sqlite_master
               WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name""").fetchall()
    }
    events = migrated.execute(
        "SELECT sequence, event_json FROM prompt_events ORDER BY run_id, sequence"
    ).fetchall()
    migrated.close()

    assert actual_schema == expected_schema
    assert actual_counts == expected_counts
    assert events == [(0, '{"type":"start"}'), (1, '{"type":"done"}')]


def test_plaintext_migration_validates_export_before_replacement(
    tenant_env,
    monkeypatch,
) -> None:
    """A mismatched encrypted export should leave the plaintext primary intact."""
    from yinshi.tenant import _migrate_plaintext_user_database

    database_path = Path(tenant_env["tmp_path"]) / "mismatched-migration.db"
    source = sqlite3.connect(database_path)
    source.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    source.execute("INSERT INTO marker VALUES ('original')")
    source.commit()
    source.close()

    def fake_copy(_source_path: str, target_path: str, _sqlcipher_key: bytes) -> None:
        target = sqlite3.connect(target_path)
        target.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        target.commit()
        target.close()

    monkeypatch.setattr("yinshi.tenant._copy_plaintext_user_database", fake_copy)
    monkeypatch.setattr(
        "yinshi.tenant._open_sqlcipher_connection",
        lambda path, _key: sqlite3.connect(path),
    )

    with pytest.raises(RuntimeError, match="does not match"):
        _migrate_plaintext_user_database(str(database_path), b"k" * 32)

    primary = sqlite3.connect(database_path)
    assert primary.execute("SELECT value FROM marker").fetchone()[0] == "original"
    primary.close()


def test_plaintext_migration_durability_order(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Migration should durably prepare rollback before atomic replacement."""
    import shutil

    import yinshi.tenant as tenant_module
    from yinshi.tenant import _migrate_plaintext_user_database

    database_path = Path(tenant_env["tmp_path"]) / "ordered-migration.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('original')")

    def fake_copy(source_path: str, target_path: str, _key: bytes) -> None:
        shutil.copyfile(source_path, target_path)

    events: list[str] = []
    original_replace = os.replace
    original_unlink = os.unlink

    def create_rollback(source_path: str, rollback_path: str) -> None:
        events.append("copy:rollback")
        shutil.copyfile(source_path, rollback_path)
        os.chmod(rollback_path, 0o600)

    def replace(source_path: str, target_path: str) -> None:
        events.append(f"replace:{Path(source_path).name}->{Path(target_path).name}")
        original_replace(source_path, target_path)

    def unlink(path: str) -> None:
        events.append(f"unlink:{Path(path).name}")
        original_unlink(path)

    monkeypatch.setattr(tenant_module, "_copy_plaintext_user_database", fake_copy)
    monkeypatch.setattr(
        tenant_module,
        "_open_sqlcipher_connection",
        lambda path, _key: sqlite3.connect(path),
    )
    monkeypatch.setattr(
        tenant_module, "_create_private_rollback_copy", create_rollback, raising=False
    )
    monkeypatch.setattr(
        tenant_module,
        "_fsync_file",
        lambda path: events.append(f"fsync:{Path(path).name}"),
        raising=False,
    )
    monkeypatch.setattr(
        tenant_module,
        "_fsync_parent_directory",
        lambda path: events.append(f"sync-parent:{Path(path).name}"),
        raising=False,
    )
    monkeypatch.setattr(tenant_module.os, "replace", replace)
    monkeypatch.setattr(tenant_module.os, "unlink", unlink)

    _migrate_plaintext_user_database(str(database_path), b"k" * 32)

    assert events == [
        "fsync:ordered-migration.db.encrypted.tmp",
        "copy:rollback",
        "fsync:ordered-migration.db.plaintext.rollback",
        "sync-parent:ordered-migration.db",
        "replace:ordered-migration.db.encrypted.tmp->ordered-migration.db",
        "sync-parent:ordered-migration.db",
        "fsync:ordered-migration.db",
        "sync-parent:ordered-migration.db",
        "unlink:ordered-migration.db.plaintext.rollback",
        "sync-parent:ordered-migration.db",
    ]


def test_init_user_db_preserves_plaintext_database_when_wal_checkpoint_is_busy(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Busy WAL checkpoint must stop tenant initialization without replacing data."""
    import yinshi.tenant as tenant_module
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, init_user_db

    data_dir = Path(tenant_env["user_data_dir"]) / "ab" / "abcdef"
    data_dir.mkdir(parents=True)
    database_path = data_dir / "yinshi.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('original')")
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(data_dir),
        db_path=str(database_path),
    )

    class Result:
        def fetchone(self) -> tuple[int, int, int]:
            return (0, 4, 2)

    class SourceConnection:
        def __init__(self, path: str) -> None:
            self.connection = sqlite3.connect(path)

        def execute(self, statement: str, parameters=()):
            if statement.startswith("PRAGMA wal_checkpoint"):
                return Result()
            return self.connection.execute(statement, parameters)

        def close(self) -> None:
            self.connection.close()

    fake_sqlcipher = SimpleNamespace(
        connect=lambda path: SourceConnection(path),
        DatabaseError=sqlite3.DatabaseError,
    )
    events: list[str] = []
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()
    monkeypatch.setattr(tenant_module, "_load_sqlcipher_module", lambda: fake_sqlcipher)
    monkeypatch.setattr(tenant_module, "_tenant_database_key", lambda _tenant: b"k" * 32)
    monkeypatch.setattr(
        tenant_module,
        "_open_sqlcipher_connection",
        lambda _path, _key: (_ for _ in ()).throw(
            tenant_module._TenantDatabaseKeyOrFormatError("plaintext database")
        ),
    )
    monkeypatch.setattr(
        tenant_module,
        "_remove_sqlite_sidecars",
        lambda _path: events.append("remove-sidecars"),
    )
    monkeypatch.setattr(
        tenant_module.os,
        "replace",
        lambda _source, _target: events.append("replace"),
    )

    with pytest.raises(RuntimeError, match="WAL checkpoint"):
        init_user_db(str(database_path), tenant=tenant)

    assert events == []
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "original"


def test_plaintext_migration_failure_durably_restores_original(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-replacement validation failure should atomically restore plaintext."""
    import shutil

    import yinshi.tenant as tenant_module
    from yinshi.tenant import _migrate_plaintext_user_database

    database_path = Path(tenant_env["tmp_path"]) / "restore-migration.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('original')")

    def fake_copy(source_path: str, target_path: str, _key: bytes) -> None:
        shutil.copyfile(source_path, target_path)

    validation_count = 0

    def validate(_path: str, _key: bytes) -> None:
        nonlocal validation_count
        validation_count += 1
        if validation_count == 2:
            raise RuntimeError("replacement invalid")

    events: list[str] = []
    original_replace = os.replace

    def replace(source_path: str, target_path: str) -> None:
        events.append(f"replace:{Path(source_path).name}->{Path(target_path).name}")
        original_replace(source_path, target_path)

    monkeypatch.setattr(tenant_module, "_copy_plaintext_user_database", fake_copy)
    monkeypatch.setattr(tenant_module, "_validate_encrypted_user_database", validate)
    monkeypatch.setattr(tenant_module, "_validate_export_matches_source", lambda *_args: None)
    monkeypatch.setattr(
        tenant_module,
        "_create_private_rollback_copy",
        lambda source, rollback: shutil.copyfile(source, rollback),
        raising=False,
    )
    monkeypatch.setattr(
        tenant_module,
        "_fsync_file",
        lambda path: events.append(f"fsync:{Path(path).name}"),
        raising=False,
    )
    monkeypatch.setattr(
        tenant_module,
        "_fsync_parent_directory",
        lambda path: events.append(f"sync-parent:{Path(path).name}"),
        raising=False,
    )
    monkeypatch.setattr(tenant_module.os, "replace", replace)

    with pytest.raises(RuntimeError, match="replacement invalid"):
        _migrate_plaintext_user_database(str(database_path), b"k" * 32)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "original"
    assert "replace:restore-migration.db.plaintext.rollback->restore-migration.db" in events
    restore_index = events.index(
        "replace:restore-migration.db.plaintext.rollback->restore-migration.db"
    )
    assert events[restore_index + 1 :] == [
        "fsync:restore-migration.db",
        "sync-parent:restore-migration.db",
    ]


def test_init_user_db_recovers_rollback_before_optional_sqlcipher_fallback(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing SQLCipher must not create an empty primary beside a valid rollback."""
    import yinshi.tenant as tenant_module
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, init_user_db

    data_dir = Path(tenant_env["user_data_dir"]) / "ab" / "abcdef"
    data_dir.mkdir(parents=True)
    database_path = data_dir / "yinshi.db"
    rollback_path = Path(f"{database_path}.plaintext.rollback")
    init_user_db(str(rollback_path))
    with sqlite3.connect(rollback_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('durable')")
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
        connection.execute("PRAGMA journal_mode = DELETE")
    os.chmod(rollback_path, 0o600)
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(data_dir),
        db_path=str(database_path),
    )
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "enabled")
    get_settings.cache_clear()
    monkeypatch.setattr(
        tenant_module,
        "_load_sqlcipher_module",
        lambda: (_ for _ in ()).throw(RuntimeError("SQLCipher unavailable")),
    )

    init_user_db(str(database_path), tenant=tenant)

    assert database_path.exists()
    assert not rollback_path.exists()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "durable"


def test_restart_recovers_tenant_rollback_when_primary_is_absent(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart recovery should never discard the only valid tenant database."""
    import yinshi.tenant as tenant_module

    database_path = Path(tenant_env["tmp_path"]) / "restart.db"
    rollback_path = Path(f"{database_path}.plaintext.rollback")
    with sqlite3.connect(rollback_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('durable')")
    os.chmod(rollback_path, 0o600)

    events: list[str] = []
    monkeypatch.setattr(
        tenant_module,
        "_fsync_file",
        lambda path: events.append(f"fsync:{Path(path).name}"),
        raising=False,
    )
    monkeypatch.setattr(
        tenant_module,
        "_fsync_parent_directory",
        lambda path: events.append(f"sync-parent:{Path(path).name}"),
        raising=False,
    )

    tenant_module._recover_plaintext_migration_rollback(str(database_path))

    assert database_path.exists()
    assert not rollback_path.exists()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "durable"
    assert events == ["fsync:restart.db", "sync-parent:restart.db"]


def test_init_user_db_rejects_symlink_migration_rollback(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant initialization must not recover through symlink indirection."""
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, init_user_db

    data_dir = Path(tenant_env["user_data_dir"]) / "ab" / "abcdef"
    data_dir.mkdir(parents=True)
    database_path = data_dir / "yinshi.db"
    target_path = data_dir / "target.db"
    rollback_path = Path(f"{database_path}.plaintext.rollback")
    with sqlite3.connect(target_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('target')")
    os.chmod(target_path, 0o600)
    rollback_path.symlink_to(target_path)
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(data_dir),
        db_path=str(database_path),
    )
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="trusted regular file"):
        init_user_db(str(database_path), tenant=tenant)

    assert not database_path.exists()
    assert rollback_path.is_symlink()
    with sqlite3.connect(target_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "target"


def test_init_user_db_rejects_changed_migration_rollback_inode(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant initialization must reject a changed rollback path inode."""
    from types import SimpleNamespace

    import yinshi.tenant as tenant_module
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, init_user_db

    data_dir = Path(tenant_env["user_data_dir"]) / "ab" / "abcdef"
    data_dir.mkdir(parents=True)
    database_path = data_dir / "yinshi.db"
    rollback_path = Path(f"{database_path}.plaintext.rollback")
    with sqlite3.connect(rollback_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    os.chmod(rollback_path, 0o600)
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(data_dir),
        db_path=str(database_path),
    )
    original_lstat = os.lstat

    def changed_lstat(path: str):
        result = original_lstat(path)
        if os.fspath(path) == os.fspath(rollback_path):
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_uid=result.st_uid,
                st_dev=result.st_dev,
                st_ino=result.st_ino + 1,
                st_nlink=result.st_nlink,
            )
        return result

    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    get_settings.cache_clear()
    monkeypatch.setattr(tenant_module.os, "lstat", changed_lstat)

    with pytest.raises(RuntimeError, match="trusted regular file"):
        init_user_db(str(database_path), tenant=tenant)

    assert not database_path.exists()
    assert rollback_path.exists()


def test_init_user_db_rejects_nonprivate_migration_rollback(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant initialization must reject a rollback accessible by other users."""
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, init_user_db

    data_dir = Path(tenant_env["user_data_dir"]) / "ab" / "abcdef"
    data_dir.mkdir(parents=True)
    database_path = data_dir / "yinshi.db"
    rollback_path = Path(f"{database_path}.plaintext.rollback")
    with sqlite3.connect(rollback_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    os.chmod(rollback_path, 0o644)
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(data_dir),
        db_path=str(database_path),
    )
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="trusted regular file"):
        init_user_db(str(database_path), tenant=tenant)

    assert not database_path.exists()
    assert rollback_path.exists()


def test_plaintext_migration_removes_unencrypted_backup(tenant_env, monkeypatch):
    """Successful SQLCipher migration must not retain a plaintext database copy."""
    from yinshi.tenant import _migrate_plaintext_user_database

    db_path = Path(tenant_env["tmp_path"]) / "migration.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    connection.execute("INSERT INTO marker (value) VALUES ('private-marker')")
    connection.commit()
    connection.close()

    def fake_copy(source_path: str, target_path: str, sqlcipher_key: bytes) -> None:
        assert source_path == str(db_path)
        assert sqlcipher_key == b"k" * 32
        encrypted_connection = sqlite3.connect(target_path)
        encrypted_connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        encrypted_connection.execute("INSERT INTO marker VALUES ('private-marker')")
        encrypted_connection.commit()
        encrypted_connection.close()

    monkeypatch.setattr("yinshi.tenant._copy_plaintext_user_database", fake_copy)
    monkeypatch.setattr(
        "yinshi.tenant._open_sqlcipher_connection",
        lambda path, _key: sqlite3.connect(path),
    )

    _migrate_plaintext_user_database(str(db_path), b"k" * 32)

    migrated_connection = sqlite3.connect(db_path)
    assert migrated_connection.execute("SELECT value FROM marker").fetchone()[0] == "private-marker"
    migrated_connection.close()
    assert list(db_path.parent.glob("migration.db.plaintext.*.bak")) == []


def test_encrypted_initialization_rejects_symlink_migration_lock(
    tenant_env,
    monkeypatch,
) -> None:
    """Encrypted initialization should reject a symlink migration lock path."""
    import importlib

    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, init_user_db

    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()
    database_path = Path(tenant_env["tmp_path"]) / "symlink-lock.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    connection.commit()
    connection.close()
    lock_target = Path(tenant_env["tmp_path"]) / "lock-target"
    lock_target.write_text("target", encoding="utf-8")
    Path(f"{database_path}.migration.lock").symlink_to(lock_target)
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(database_path.parent),
        db_path=str(database_path),
    )
    fake_sqlcipher = SimpleNamespace(
        connect=sqlite3.connect,
        Row=sqlite3.Row,
        DatabaseError=sqlite3.DatabaseError,
    )
    original_import_module = importlib.import_module

    def load_module(name: str):
        if name == "sqlcipher3.dbapi2":
            return fake_sqlcipher
        return original_import_module(name)

    monkeypatch.setattr(importlib, "import_module", load_module)
    monkeypatch.setattr("yinshi.tenant._tenant_database_key", lambda _tenant: b"k" * 32)

    with pytest.raises(RuntimeError, match="migration lock"):
        init_user_db(str(database_path), tenant)

    assert lock_target.read_text(encoding="utf-8") == "target"


def test_concurrent_initializers_migrate_plaintext_once(
    tenant_env,
    monkeypatch,
) -> None:
    """Concurrent initializers should leave one complete encrypted database."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import yinshi.tenant as tenant_module
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, init_user_db

    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()
    database_path = Path(tenant_env["tmp_path"]) / "concurrent.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    connection.commit()
    connection.close()
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(database_path.parent),
        db_path=str(database_path),
    )
    migration_started = threading.Event()
    allow_migration = threading.Event()
    migration_count = 0
    count_lock = threading.Lock()

    def migrate(path: str, _key: bytes) -> None:
        nonlocal migration_count
        if Path(path).read_bytes() == b"encrypted-primary":
            return
        with count_lock:
            migration_count += 1
        migration_started.set()
        assert allow_migration.wait(timeout=5)
        Path(path).write_bytes(b"encrypted-primary")

    def open_sqlcipher(path: str, _key: bytes):
        if Path(path).read_bytes() != b"encrypted-primary":
            raise tenant_module._TenantDatabaseKeyOrFormatError("plaintext database")
        return sqlite3.connect(":memory:")

    monkeypatch.setattr("yinshi.tenant._tenant_database_key", lambda _tenant: b"k" * 32)
    monkeypatch.setattr("yinshi.tenant._load_sqlcipher_module", lambda: object())
    monkeypatch.setattr("yinshi.tenant._migrate_plaintext_user_database", migrate)
    monkeypatch.setattr("yinshi.tenant._open_sqlcipher_connection", open_sqlcipher)
    monkeypatch.setattr(
        "yinshi.tenant._ensure_current_user_db_schema",
        lambda _connection: None,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(init_user_db, str(database_path), tenant)
        assert migration_started.wait(timeout=5)
        second = executor.submit(init_user_db, str(database_path), tenant)
        assert not second.done()
        with count_lock:
            assert migration_count == 1
        allow_migration.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert migration_count == 1
    assert database_path.read_bytes() == b"encrypted-primary"
    assert list(database_path.parent.glob("concurrent.db.plaintext.*.bak")) == []
    assert not Path(f"{database_path}.encrypted.tmp").exists()
    assert not Path(f"{database_path}-wal").exists()
    assert not Path(f"{database_path}-shm").exists()


def test_forked_child_resets_inherited_migration_locks(tenant_env) -> None:
    """A child process must not inherit a permanently held migration guard."""
    import multiprocessing

    import yinshi.tenant as tenant_module
    from yinshi.tenant import init_user_db

    process_context = multiprocessing.get_context("fork")
    database_path = Path(tenant_env["tmp_path"]) / "fork-reset.db"
    tenant_module._MIGRATION_THREAD_LOCKS_GUARD.acquire()
    try:
        child = process_context.Process(target=init_user_db, args=(str(database_path),))
        child.start()
    finally:
        tenant_module._MIGRATION_THREAD_LOCKS_GUARD.release()
    child.join(timeout=5)
    if child.is_alive():
        child.terminate()
        child.join(timeout=5)

    assert child.exitcode == 0


def test_independent_processes_serialize_plaintext_migration(
    tenant_env,
    monkeypatch,
) -> None:
    """Independent processes should wait for one plaintext migration owner."""
    import multiprocessing

    import yinshi.tenant as tenant_module
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, init_user_db

    process_context = multiprocessing.get_context("fork")
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()
    database_path = Path(tenant_env["tmp_path"]) / "process-concurrent.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    connection.commit()
    connection.close()
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(database_path.parent),
        db_path=str(database_path),
    )
    first_migration_started = process_context.Event()
    second_migration_started = process_context.Event()
    allow_migration = process_context.Event()
    migration_count = process_context.Value("i", 0)

    def migrate(path: str, _key: bytes) -> None:
        if Path(path).read_bytes() == b"encrypted-primary":
            return
        with migration_count.get_lock():
            migration_count.value += 1
            current_count = migration_count.value
        if current_count == 1:
            first_migration_started.set()
        else:
            second_migration_started.set()
        assert allow_migration.wait(timeout=5)
        Path(path).write_bytes(b"encrypted-primary")

    def open_sqlcipher(path: str, _key: bytes):
        if Path(path).read_bytes() != b"encrypted-primary":
            raise tenant_module._TenantDatabaseKeyOrFormatError("plaintext database")
        return sqlite3.connect(":memory:")

    monkeypatch.setattr("yinshi.tenant._tenant_database_key", lambda _tenant: b"k" * 32)
    monkeypatch.setattr("yinshi.tenant._load_sqlcipher_module", lambda: object())
    monkeypatch.setattr("yinshi.tenant._migrate_plaintext_user_database", migrate)
    monkeypatch.setattr("yinshi.tenant._open_sqlcipher_connection", open_sqlcipher)
    monkeypatch.setattr(
        "yinshi.tenant._ensure_current_user_db_schema",
        lambda _connection: None,
    )

    first = process_context.Process(
        target=init_user_db,
        args=(str(database_path), tenant),
    )
    second = process_context.Process(
        target=init_user_db,
        args=(str(database_path), tenant),
    )
    first.start()
    assert first_migration_started.wait(timeout=5)
    second.start()
    assert not second_migration_started.wait(timeout=0.5)
    allow_migration.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert migration_count.value == 1
    assert database_path.read_bytes() == b"encrypted-primary"


def test_real_encrypted_database_opens_before_plaintext_detection(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid keyed database should bypass every plaintext detector."""
    import yinshi.tenant as tenant_module
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, _open_sqlcipher_connection, get_user_db

    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()
    data_directory = Path(tenant_env["user_data_dir"]) / "ab" / "abcdef"
    data_directory.mkdir(parents=True)
    database_path = data_directory / "yinshi.db"
    key = b"k" * 32
    encrypted_connection = _open_sqlcipher_connection(str(database_path), key)
    tenant_module._ensure_current_user_db_schema(encrypted_connection)
    encrypted_connection.close()
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(data_directory),
        db_path=str(database_path),
    )

    def reject_plaintext_detection(*_args):
        raise AssertionError("valid encrypted database must bypass plaintext detection")

    monkeypatch.setattr(tenant_module, "_tenant_database_key", lambda _tenant: key)
    monkeypatch.setattr(
        tenant_module,
        "_plaintext_database_readable",
        reject_plaintext_detection,
    )
    monkeypatch.setattr(
        tenant_module,
        "_read_database_header",
        reject_plaintext_detection,
    )

    with get_user_db(tenant) as connection:
        assert connection.execute("SELECT count(*) FROM sqlite_master").fetchone() is not None


def test_plaintext_header_inspection_rejects_symlink_before_sqlite(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Header inspection must reject symlinks before any SQLite API."""
    import yinshi.tenant as tenant_module

    target_path = Path(tenant_env["tmp_path"]) / "target.db"
    with sqlite3.connect(target_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    database_path = Path(tenant_env["tmp_path"]) / "symlink.db"
    database_path.symlink_to(target_path)

    def reject_stdlib_open(_path: str):
        raise AssertionError("stdlib SQLite must not open a symlink")

    monkeypatch.setattr(tenant_module, "_open_connection", reject_stdlib_open)

    with pytest.raises(RuntimeError, match="trusted regular file"):
        tenant_module._plaintext_database_readable(str(database_path))


def test_database_header_file_error_is_sanitized(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Header I/O failures should propagate without exposing database paths."""
    import yinshi.tenant as tenant_module

    database_path = Path(tenant_env["tmp_path"]) / "unreadable.db"
    database_path.write_bytes(b"SQLite format 3\x00")

    def reject_open(_path: str, _flags: int):
        raise PermissionError("private provider detail")

    monkeypatch.setattr(tenant_module.os, "open", reject_open)

    with pytest.raises(RuntimeError, match="header could not be inspected") as raised:
        tenant_module._read_database_header(str(database_path))

    assert str(database_path) not in str(raised.value)
    assert isinstance(raised.value.__cause__, PermissionError)


def test_database_header_close_error_is_sanitized(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Header descriptor close failures should become sanitized runtime errors."""
    import yinshi.tenant as tenant_module

    database_path = Path(tenant_env["tmp_path"]) / "close-error.db"
    database_path.write_bytes(b"SQLite format 3\x00")
    original_close = tenant_module.os.close

    def close_then_fail(descriptor: int) -> None:
        original_close(descriptor)
        raise OSError("private provider detail")

    monkeypatch.setattr(tenant_module.os, "close", close_then_fail)

    with pytest.raises(RuntimeError, match="header could not be inspected") as raised:
        tenant_module._read_database_header(str(database_path))

    assert str(database_path) not in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError)


def test_plaintext_header_opens_stdlib_sqlite_for_migration_probe(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical plaintext databases should retain stdlib validation."""
    import yinshi.tenant as tenant_module

    database_path = Path(tenant_env["tmp_path"]) / "plaintext-header.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    assert database_path.read_bytes()[:16] == b"SQLite format 3\x00"
    opened_paths: list[str] = []
    original_open_connection = tenant_module._open_connection

    def track_stdlib_open(path: str):
        opened_paths.append(path)
        return original_open_connection(path)

    monkeypatch.setattr(tenant_module, "_open_connection", track_stdlib_open)

    assert tenant_module._plaintext_database_readable(str(database_path)) is True
    assert opened_paths == [str(database_path)]


def test_get_user_db_encrypted_header_only_uses_sqlcipher(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An encrypted primary should only be opened through SQLCipher."""
    import yinshi.tenant as tenant_module
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, get_user_db

    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()
    data_directory = Path(tenant_env["user_data_dir"]) / "ab" / "abcdef"
    data_directory.mkdir(parents=True)
    database_path = data_directory / "yinshi.db"
    encrypted_contents = b"encrypted-primary" * 2
    database_path.write_bytes(encrypted_contents)
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(data_directory),
        db_path=str(database_path),
    )
    sqlcipher_connection = sqlite3.connect(":memory:")
    sqlcipher_opens: list[tuple[str, bytes]] = []

    def reject_stdlib_open(_path: str):
        raise AssertionError("stdlib SQLite must not open a non-plaintext primary")

    def reject_plaintext_migration(*_args):
        raise AssertionError("non-plaintext primary must not enter plaintext migration")

    def open_sqlcipher(path: str, key: bytes):
        sqlcipher_opens.append((path, key))
        return sqlcipher_connection

    monkeypatch.setattr(tenant_module, "_open_connection", reject_stdlib_open)
    monkeypatch.setattr(
        tenant_module,
        "_copy_plaintext_user_database",
        reject_plaintext_migration,
    )
    monkeypatch.setattr(
        tenant_module,
        "_remove_sqlite_sidecars",
        reject_plaintext_migration,
    )
    monkeypatch.setattr(tenant_module, "_tenant_database_key", lambda _tenant: b"k" * 32)
    monkeypatch.setattr(tenant_module, "_load_sqlcipher_module", lambda: object())
    monkeypatch.setattr(tenant_module, "_open_sqlcipher_connection", open_sqlcipher)
    monkeypatch.setattr(
        tenant_module,
        "_ensure_current_user_db_schema",
        lambda _connection: None,
    )

    with get_user_db(tenant) as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    assert sqlcipher_opens == [(str(database_path), b"k" * 32)]
    assert database_path.read_bytes() == encrypted_contents


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param(b"SQLite format 3", id="truncated"),
        pytest.param(b"SQLite format 3X", id="near-match"),
        pytest.param(b"\x00" * 32, id="random"),
    ],
)
def test_get_user_db_malformed_headers_fail_closed(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
    contents: bytes,
) -> None:
    """Malformed primaries must fail keyed validation without stdlib fallback."""
    import yinshi.tenant as tenant_module
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, get_user_db

    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()
    data_directory = Path(tenant_env["user_data_dir"]) / "ab" / "abcdef"
    data_directory.mkdir(parents=True)
    database_path = data_directory / "yinshi.db"
    database_path.write_bytes(contents)
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(data_directory),
        db_path=str(database_path),
    )

    def reject_stdlib_open(_path: str):
        raise AssertionError("stdlib SQLite must not open a malformed primary")

    def reject_keyed_open(_path: str, _key: bytes):
        raise tenant_module._TenantDatabaseKeyOrFormatError("invalid keyed database")

    monkeypatch.setattr(tenant_module, "_open_connection", reject_stdlib_open)
    monkeypatch.setattr(tenant_module, "_tenant_database_key", lambda _tenant: b"k" * 32)
    monkeypatch.setattr(tenant_module, "_load_sqlcipher_module", lambda: object())
    monkeypatch.setattr(tenant_module, "_open_sqlcipher_connection", reject_keyed_open)

    with pytest.raises(RuntimeError, match="invalid keyed database"):
        with get_user_db(tenant):
            pass

    assert database_path.read_bytes() == contents


def test_transient_keyed_open_failure_bypasses_plaintext_detection(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Temporary keyed-open failures should propagate without migration checks."""
    import yinshi.tenant as tenant_module
    from yinshi.config import get_settings
    from yinshi.tenant import (
        TenantContext,
        TenantDatabaseTemporarilyUnavailable,
        get_user_db,
    )

    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()
    data_directory = Path(tenant_env["user_data_dir"]) / "ab" / "abcdef"
    data_directory.mkdir(parents=True)
    database_path = data_directory / "yinshi.db"
    database_path.write_bytes(b"SQLite format 3\x00" + b"x" * 32)
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(data_directory),
        db_path=str(database_path),
    )
    temporary_error = TenantDatabaseTemporarilyUnavailable("temporary storage failure")

    def reject_plaintext_header(_path: str):
        raise AssertionError("temporary failure must bypass plaintext detection")

    monkeypatch.setattr(tenant_module, "_tenant_database_key", lambda _tenant: b"k" * 32)
    monkeypatch.setattr(tenant_module, "_load_sqlcipher_module", lambda: object())
    monkeypatch.setattr(
        tenant_module,
        "_open_sqlcipher_connection",
        lambda _path, _key: (_ for _ in ()).throw(temporary_error),
    )
    monkeypatch.setattr(
        tenant_module,
        "_database_has_plaintext_header",
        reject_plaintext_header,
    )

    with pytest.raises(TenantDatabaseTemporarilyUnavailable) as raised:
        with get_user_db(tenant):
            pass

    assert raised.value is temporary_error


def test_wrong_key_encrypted_database_fails_without_stdlib_fallback(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrong-key validation should fail without a plaintext SQLite open."""
    import yinshi.tenant as tenant_module
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, _open_sqlcipher_connection, get_user_db

    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()
    data_directory = Path(tenant_env["user_data_dir"]) / "ab" / "abcdef"
    data_directory.mkdir(parents=True)
    database_path = data_directory / "yinshi.db"
    encrypted_connection = _open_sqlcipher_connection(str(database_path), b"a" * 32)
    tenant_module._ensure_current_user_db_schema(encrypted_connection)
    encrypted_connection.close()
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(data_directory),
        db_path=str(database_path),
    )

    def reject_stdlib_open(_path: str):
        raise AssertionError("wrong-key database must not open with stdlib SQLite")

    monkeypatch.setattr(tenant_module, "_open_connection", reject_stdlib_open)
    monkeypatch.setattr(tenant_module, "_tenant_database_key", lambda _tenant: b"b" * 32)

    with pytest.raises(RuntimeError, match="configured key"):
        with get_user_db(tenant):
            pass


def test_concurrent_encrypted_open_avoids_stdlib_during_active_operation(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live encrypted operation must not overlap any stdlib database open."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import yinshi.tenant as tenant_module
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, get_user_db

    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()
    data_directory = Path(tenant_env["user_data_dir"]) / "ab" / "abcdef"
    data_directory.mkdir(parents=True)
    database_path = data_directory / "yinshi.db"
    database_path.write_bytes(b"encrypted-primary" * 32)
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(data_directory),
        db_path=str(database_path),
    )
    operation_active = threading.Event()
    release_operation = threading.Event()

    class FakeSqlcipherConnection:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, statement: str):
            if statement == "HOLD":
                operation_active.set()
                assert release_operation.wait(timeout=5)
            return SimpleNamespace(fetchone=lambda: (1,))

        def close(self) -> None:
            self.closed = True

    opened_connections: list[FakeSqlcipherConnection] = []

    def reject_stdlib_open(_path: str):
        raise AssertionError("stdlib SQLite must not open an encrypted primary")

    def open_sqlcipher(_path: str, _key: bytes) -> FakeSqlcipherConnection:
        connection = FakeSqlcipherConnection()
        opened_connections.append(connection)
        return connection

    def open_and_close() -> bool:
        with get_user_db(tenant) as connection:
            return connection.execute("SELECT 1").fetchone()[0] == 1

    monkeypatch.setattr(tenant_module, "_open_connection", reject_stdlib_open)
    monkeypatch.setattr(tenant_module, "_tenant_database_key", lambda _tenant: b"k" * 32)
    monkeypatch.setattr(tenant_module, "_load_sqlcipher_module", lambda: object())
    monkeypatch.setattr(tenant_module, "_open_sqlcipher_connection", open_sqlcipher)
    monkeypatch.setattr(
        tenant_module,
        "_ensure_current_user_db_schema",
        lambda _connection: None,
    )

    with get_user_db(tenant) as first_connection:
        with ThreadPoolExecutor(max_workers=2) as executor:
            active_operation = executor.submit(first_connection.execute, "HOLD")
            assert operation_active.wait(timeout=5)
            try:
                concurrent_open = executor.submit(open_and_close)
                assert concurrent_open.result(timeout=5) is True
            finally:
                release_operation.set()
            active_operation.result(timeout=5)

    assert len(opened_connections) == 2
    assert all(connection.closed for connection in opened_connections)


def test_open_encrypted_database_removes_legacy_plaintext_backup(
    tenant_env,
    monkeypatch,
) -> None:
    """Validated encrypted primaries should trigger cleanup of migration residue."""
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, _open_user_connection

    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()
    data_directory = Path(tenant_env["user_data_dir"]) / "ab" / "abcdef"
    data_directory.mkdir(parents=True)
    database_path = data_directory / "yinshi.db"
    database_path.write_bytes(b"encrypted-primary")
    backup_path = data_directory / "yinshi.db.plaintext.123.bak"
    backup_path.write_text("private-legacy-data", encoding="utf-8")
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(data_directory),
        db_path=str(database_path),
    )
    fake_connection = object()
    monkeypatch.setattr("yinshi.tenant._tenant_database_key", lambda _tenant: b"k" * 32)
    monkeypatch.setattr("yinshi.tenant._load_sqlcipher_module", lambda: object())
    monkeypatch.setattr(
        "yinshi.tenant._open_sqlcipher_connection",
        lambda _path, _key: fake_connection,
    )
    monkeypatch.setattr(
        "yinshi.tenant._ensure_current_user_db_schema",
        lambda _connection: None,
    )

    connection = _open_user_connection(str(database_path), tenant)

    assert connection is fake_connection
    assert not backup_path.exists()


def test_optional_sqlcipher_unavailable_rejects_existing_encrypted_database(
    tenant_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional mode must not open an encrypted primary with stdlib SQLite."""
    import yinshi.tenant as tenant_module
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, get_user_db

    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "enabled")
    get_settings.cache_clear()
    data_directory = Path(tenant_env["user_data_dir"]) / "ab" / "abcdef"
    data_directory.mkdir(parents=True)
    database_path = data_directory / "yinshi.db"
    database_path.write_bytes(b"encrypted-primary" * 2)
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=str(data_directory),
        db_path=str(database_path),
    )

    def reject_stdlib_open(_path: str):
        raise AssertionError("stdlib SQLite must not open an encrypted primary")

    monkeypatch.setattr(
        tenant_module,
        "_load_sqlcipher_module",
        lambda: (_ for _ in ()).throw(RuntimeError("SQLCipher unavailable")),
    )
    monkeypatch.setattr(tenant_module, "_open_connection", reject_stdlib_open)

    with pytest.raises(RuntimeError, match="cannot be opened without SQLCipher"):
        with get_user_db(tenant):
            pass


def test_required_tenant_db_encryption_fails_without_sqlcipher(tenant_env, monkeypatch):
    """Required SQLCipher mode should fail closed when no SQLCipher driver is installed."""
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, init_user_db

    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    get_settings.cache_clear()

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "abcdef")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=data_dir,
        db_path=db_path,
    )

    def missing_sqlcipher(name: str):
        raise ImportError(f"missing {name}")

    monkeypatch.setattr("yinshi.tenant._tenant_database_key", lambda _: b"1" * 32)
    monkeypatch.setattr("importlib.import_module", missing_sqlcipher)

    with pytest.raises(RuntimeError, match="requires sqlcipher3 or pysqlcipher3"):
        init_user_db(db_path, tenant=tenant)


def test_user_data_encryption_required_checks_marker(tenant_env, monkeypatch):
    """Required filesystem encryption should fail closed without the marker file."""
    from yinshi.config import get_settings
    from yinshi.tenant import TenantContext, init_user_db

    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    monkeypatch.setenv("USER_DATA_ENCRYPTION", "required")
    get_settings.cache_clear()

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "abcdef")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)
    tenant = TenantContext(
        user_id="abcdef",
        email="user@example.com",
        data_dir=data_dir,
        db_path=db_path,
    )

    with pytest.raises(RuntimeError, match=".yinshi-encrypted-storage") as raised:
        init_user_db(db_path, tenant=tenant)

    assert data_dir not in str(raised.value)
    assert tenant.user_id not in str(raised.value)
    Path(tenant_env["user_data_dir"]).joinpath(".yinshi-encrypted-storage").write_text(
        "fscrypt managed outside Yinshi\n",
        encoding="utf-8",
    )
    init_user_db(db_path, tenant=tenant)
    assert os.path.exists(db_path)
