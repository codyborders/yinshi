"""Thread orchestration schema migration and integrity tests."""

from __future__ import annotations

import os
import sqlite3

import pytest


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] if not isinstance(row, sqlite3.Row) else row["name"] for row in rows}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()  # noqa: S608
    return {row[1] for row in rows}


@pytest.fixture
def tenant_env(tmp_path, monkeypatch):
    """Isolated tenant environment matching test_tenant.py conventions."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    from yinshi.config import get_settings

    get_settings.cache_clear()
    yield {"user_data_dir": str(tmp_path / "users")}
    get_settings.cache_clear()


def test_legacy_database_migration_adds_thread_schema(tmp_path, monkeypatch):
    """A pre-thread shared database gains thread tables and columns without data loss."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    from yinshi.config import get_settings
    from yinshi.db import get_db, init_db

    get_settings.cache_clear()

    # Simulate a schema-version-5 database: no session titles, no workspace
    # kinds, and no thread tables, holding one existing workspace and session.
    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    conn.executescript("""
        CREATE TABLE repos (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            name TEXT NOT NULL,
            remote_url TEXT,
            root_path TEXT NOT NULL,
            custom_prompt TEXT,
            agents_md TEXT,
            owner_email TEXT,
            installation_id INTEGER
        );
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            repo_id TEXT NOT NULL,
            name TEXT NOT NULL,
            branch TEXT NOT NULL,
            path TEXT NOT NULL,
            state TEXT DEFAULT 'ready' NOT NULL
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            workspace_id TEXT NOT NULL,
            status TEXT DEFAULT 'idle' NOT NULL,
            model TEXT,
            pi_context_version INTEGER DEFAULT 1 NOT NULL
        );
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (5);
        INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'r', '/tmp/r');
        INSERT INTO workspaces (id, repo_id, name, branch, path)
            VALUES ('ws1', 'repo1', 'w', 'branch', '/tmp/r/w');
        INSERT INTO sessions (id, workspace_id) VALUES ('sess1', 'ws1');
    """)
    conn.commit()
    conn.close()

    init_db()

    with get_db() as migrated:
        assert "thread_delegations" in _table_names(migrated)
        assert "thread_results" in _table_names(migrated)
        session_columns = _column_names(migrated, "sessions")
        workspace_columns = _column_names(migrated, "workspaces")
        version_row = migrated.execute("SELECT version FROM schema_version").fetchone()
        session_row = migrated.execute("SELECT title FROM sessions WHERE id = 'sess1'").fetchone()
        workspace_row = migrated.execute(
            "SELECT kind, parent_workspace_id FROM workspaces WHERE id = 'ws1'"
        ).fetchone()

    assert "title" in session_columns
    assert "kind" in workspace_columns
    assert "parent_workspace_id" in workspace_columns
    assert version_row is not None and version_row[0] >= 6
    assert session_row is not None and session_row[0] is None
    assert workspace_row is not None and workspace_row[0] == "user"
    assert workspace_row[1] is None
    get_settings.cache_clear()


def _create_version_one_tenant_database(db_path: str) -> None:
    """Create a tenant database that stops at the pre-thread schema version."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE repos (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            name TEXT NOT NULL,
            remote_url TEXT,
            root_path TEXT NOT NULL,
            custom_prompt TEXT,
            agents_md TEXT,
            installation_id INTEGER
        );
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            repo_id TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            branch TEXT NOT NULL,
            path TEXT NOT NULL,
            state TEXT DEFAULT 'ready' NOT NULL
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            status TEXT DEFAULT 'idle' NOT NULL,
            model TEXT,
            pi_context_version INTEGER DEFAULT 1 NOT NULL
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT,
            full_message TEXT,
            turn_id TEXT,
            turn_status TEXT
        );
        CREATE TABLE prompt_runs (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL,
            UNIQUE(session_id, idempotency_key)
        );
        CREATE TABLE prompt_events (
            run_id TEXT NOT NULL REFERENCES prompt_runs(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            event_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            PRIMARY KEY(run_id, sequence)
        );
        PRAGMA user_version = 1;
    """)
    conn.commit()
    conn.close()


def test_tenant_database_migration_adds_thread_schema(tenant_env):
    """A version-1 tenant database migrates to the thread schema on open."""
    import yinshi.tenant as tenant_module
    from yinshi.tenant import TenantContext, get_user_db

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "threads1")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)
    _create_version_one_tenant_database(db_path)

    context = TenantContext(
        user_id="threads1",
        email="threads1@example.com",
        data_dir=data_dir,
        db_path=db_path,
    )

    with get_user_db(context) as conn:
        assert "thread_delegations" in _table_names(conn)
        assert "thread_results" in _table_names(conn)
        assert "title" in _column_names(conn, "sessions")
        assert "kind" in _column_names(conn, "workspaces")
        assert "parent_workspace_id" in _column_names(conn, "workspaces")
        assert tenant_module._USER_SCHEMA_VERSION == 2
        version_row = conn.execute("PRAGMA user_version").fetchone()
        assert version_row is not None and version_row[0] == 2

    tenant_module._MIGRATION_THREAD_LOCKS.clear()


def test_sqlcipher_tenant_database_migration_adds_thread_schema(tenant_env, monkeypatch):
    """An encrypted version-1 tenant database migrates through the same path."""
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    import yinshi.tenant as tenant_module
    from yinshi.config import get_settings, tenant_db_encryption_required
    from yinshi.tenant import (
        TenantContext,
        _open_sqlcipher_connection,
        get_user_db,
    )

    assert tenant_db_encryption_required(get_settings())
    key = b"thread-cipher-test-key-0000000001"[:32]
    monkeypatch.setattr(tenant_module, "_tenant_database_key", lambda _tenant: key)

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "cipherth")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)
    context = TenantContext(
        user_id="cipherth",
        email="cipherth@example.com",
        data_dir=data_dir,
        db_path=db_path,
    )

    # Build the version-1 schema in plaintext, then encrypt it in place using
    # the same export routine the production migration path relies on.
    plaintext_path = f"{db_path}.seed"
    _create_version_one_tenant_database(plaintext_path)
    encrypted_seed = _open_sqlcipher_connection(f"{db_path}.enc", key)
    try:
        encrypted_seed.execute(
            "ATTACH DATABASE ? AS seed KEY ''",
            (plaintext_path,),
        )
        encrypted_seed.execute("SELECT sqlcipher_export('seed')").fetchone()
        encrypted_seed.execute("DETACH DATABASE seed")
        encrypted_seed.commit()
    finally:
        encrypted_seed.close()
    os.replace(f"{db_path}.enc", db_path)
    os.chmod(db_path, 0o600)
    os.unlink(plaintext_path)

    with get_user_db(context) as conn:
        assert "thread_delegations" in _table_names(conn)
        assert "thread_results" in _table_names(conn)
        assert "title" in _column_names(conn, "sessions")
        assert "kind" in _column_names(conn, "workspaces")
        version_row = conn.execute("PRAGMA user_version").fetchone()
        assert version_row is not None and version_row[0] == 2

    import yinshi.tenant as tenant_module

    tenant_module._MIGRATION_THREAD_LOCKS.clear()


def test_tenant_database_migration_is_idempotent(tenant_env):
    """Reopening a migrated tenant database keeps the schema stable."""
    from yinshi.tenant import TenantContext, get_user_db, init_user_db

    data_dir = os.path.join(tenant_env["user_data_dir"], "ab", "threadsidem")
    db_path = os.path.join(data_dir, "yinshi.db")
    os.makedirs(data_dir, exist_ok=True)
    context = TenantContext(
        user_id="threadsidem",
        email="threadsidem@example.com",
        data_dir=data_dir,
        db_path=db_path,
    )
    init_user_db(db_path, tenant=context)
    for _ in range(3):
        with get_user_db(context) as conn:
            conn.execute("SELECT count(*) FROM thread_delegations").fetchone()

    with sqlite3.connect(db_path) as plain:
        version_row = plain.execute("PRAGMA user_version").fetchone()
        delegation_tables = plain.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('thread_delegations', 'thread_results')"
        ).fetchone()
        duplicated_indexes = plain.execute(
            "SELECT name, count(*) FROM sqlite_master WHERE type = 'index' "
            "AND name LIKE 'idx_thread_%' GROUP BY name HAVING count(*) > 1"
        ).fetchall()
    assert version_row is not None and version_row[0] == 2
    assert delegation_tables is not None and delegation_tables[0] == 2
    assert duplicated_indexes == []


def test_legacy_database_migration_is_idempotent(tmp_path, monkeypatch):
    """Running init_db twice must not fail or duplicate thread schema objects."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    from yinshi.config import get_settings
    from yinshi.db import get_db, init_db

    get_settings.cache_clear()
    init_db()
    init_db()

    with get_db() as conn:
        version_rows = conn.execute("SELECT version FROM schema_version").fetchall()
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('thread_delegations', 'thread_results')"
        ).fetchall()
        duplicated_indexes = conn.execute(
            "SELECT name, count(*) FROM sqlite_master WHERE type = 'index' "
            "AND name LIKE 'idx_thread_%' GROUP BY name HAVING count(*) > 1"
        ).fetchall()

    assert len(version_rows) == 1
    assert len(table_rows) == 2
    assert duplicated_indexes == []
    get_settings.cache_clear()


def test_legacy_database_enforces_delegation_integrity(db):
    """Delegation foreign keys must reject self-parents and duplicate children."""
    db.execute("INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'r', '/tmp/r')")
    db.execute(
        "INSERT INTO workspaces (id, repo_id, name, branch, path) "
        "VALUES ('ws1', 'repo1', 'w', 'branch', '/tmp/r/w')"
    )
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('parent1', 'ws1')")
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('child1', 'ws1')")
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'del1', 'parent1', 'child1', 'key1',
               'user', 't', 'task', 'model', 'completed'
           )""")
    db.commit()

    # Duplicate child relationship is rejected.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("""INSERT INTO thread_delegations (
                   id, parent_session_id, child_session_id, idempotency_key,
                   initiator, title, task, requested_model, status
               ) VALUES (
                   'del2', 'parent1', 'child1', 'key2',
                   'user', 't', 'task', 'model', 'completed'
               )""")

    # A session cannot delegate to itself.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("""INSERT INTO thread_delegations (
                   id, parent_session_id, child_session_id, idempotency_key,
                   initiator, title, task, requested_model, status
               ) VALUES (
                   'del3', 'parent1', 'parent1', 'key3',
                   'user', 't', 'task', 'model', 'completed'
               )""")

    # Deleting the parent session while a delegation exists is restricted.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM sessions WHERE id = 'parent1'")

    # The child-session cascade removes the delegation and its result.
    db.execute("INSERT INTO thread_results (delegation_id, source) VALUES ('del1', 'reported')")
    db.execute("DELETE FROM sessions WHERE id = 'child1'")
    db.commit()
    remaining = db.execute("SELECT count(*) FROM thread_delegations WHERE id = 'del1'").fetchone()
    results = db.execute("SELECT count(*) FROM thread_results").fetchone()
    assert remaining is not None and remaining[0] == 0
    assert results is not None and results[0] == 0


def test_thread_results_constraints(db):
    """thread_results must reference a delegation and use a known source."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO thread_results (delegation_id, source) " "VALUES ('missing', 'reported')"
        )
    db.rollback()

    db.execute("INSERT INTO repos (id, name, root_path) VALUES ('repo1', 'r', '/tmp/r')")
    db.execute(
        "INSERT INTO workspaces (id, repo_id, name, branch, path) "
        "VALUES ('ws1', 'repo1', 'w', 'branch', '/tmp/r/w')"
    )
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('p1', 'ws1')")
    db.execute("INSERT INTO sessions (id, workspace_id) VALUES ('c1', 'ws1')")
    db.execute("""INSERT INTO thread_delegations (
               id, parent_session_id, child_session_id, idempotency_key,
               initiator, title, task, requested_model, status
           ) VALUES (
               'del1', 'p1', 'c1', 'key1',
               'agent', 't', 'task', 'model', 'running'
           )""")
    db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO thread_results (delegation_id, source) " "VALUES ('del1', 'fabricated')"
        )
    db.rollback()

    db.execute(
        "INSERT INTO thread_results (delegation_id, source, sealed) "
        "VALUES ('del1', 'reported', 0)"
    )
    db.commit()
    row = db.execute(
        "SELECT version, sealed FROM thread_results WHERE delegation_id = 'del1'"
    ).fetchone()
    assert row is not None
    assert row["version"] == 1
    assert row["sealed"] == 0
