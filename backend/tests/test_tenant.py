"""Tests for tenant context and per-user database management."""

import os
import sqlite3
from pathlib import Path

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

    with pytest.raises(RuntimeError, match=".yinshi-encrypted-storage"):
        init_user_db(db_path, tenant=tenant)

    Path(tenant_env["user_data_dir"]).joinpath(".yinshi-encrypted-storage").write_text(
        "fscrypt managed outside Yinshi\n",
        encoding="utf-8",
    )
    init_user_db(db_path, tenant=tenant)
    assert os.path.exists(db_path)
