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


def test_init_control_db_adds_typed_managed_operation_failure_class(tmp_path, monkeypatch):
    """Managed operation failures must retain their semantic alert class."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "disabled")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    init_control_db()
    with get_control_db() as database:
        columns = {
            row["name"]
            for row in database.execute("PRAGMA table_info(managed_backup_operations)").fetchall()
        }

    assert "failure_class" in columns
    get_settings.cache_clear()


def test_init_control_db_backfills_durable_managed_sprite_ownership(tmp_path, monkeypatch):
    """Migration must register existing runtime ownership without claiming inventory."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "disabled")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    init_control_db()
    with get_control_db() as database:
        database.execute(
            "INSERT INTO users (id, email, display_name) VALUES (?, ?, ?)",
            ("user-1", "user@example.com", "User"),
        )
        database.execute(
            """INSERT INTO user_runners
               (id, user_id, kind, name, cloud_provider, region, status)
               VALUES (?, ?, 'managed', ?, 'fly_sprites', 'ord', 'online')""",
            ("runner-1", "user-1", "Managed"),
        )
        database.execute(
            """INSERT INTO managed_runtimes
               (user_id, runner_id, provider_name, sprite_external_id,
                lifecycle_status, generation, artifact_version)
               VALUES (?, ?, 'fly_sprites', ?, 'ready', 1, 'version')""",
            ("user-1", "runner-1", "yinshi-existing"),
        )
        database.commit()
    init_control_db()

    with get_control_db() as database:
        rows = database.execute(
            "SELECT sprite_name, lifecycle_status FROM managed_sprite_identities"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("yinshi-existing", "active")]
    get_settings.cache_clear()


def test_init_control_db_creates_managed_sprite_identity_registry(tmp_path, monkeypatch):
    """Control initialization must persist deployment-owned Sprite identities."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "disabled")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    init_control_db()
    with get_control_db() as database:
        columns = {
            row["name"]
            for row in database.execute("PRAGMA table_info(managed_sprite_identities)").fetchall()
        }

    assert columns == {
        "sprite_name",
        "provider_name",
        "identity_kind",
        "user_id",
        "job_id",
        "lifecycle_status",
        "created_at",
        "updated_at",
    }
    get_settings.cache_clear()


def test_init_control_db_creates_managed_backup_catalog(tmp_path, monkeypatch):
    """Control initialization should create durable managed backup tables."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        init_control_db()
        with get_control_db() as database:
            table_names = {
                row["name"]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        assert "managed_backup_archives" in table_names
        assert "managed_backup_operations" in table_names
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


def test_application_plaintext_migration_durability_order(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Application migration should sync rollback and replacement in durable order."""
    import os
    import shutil
    from pathlib import Path
    from types import SimpleNamespace

    import yinshi.db as db_module

    database_path = tmp_path / "ordered-application.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('original')")

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
                shutil.copyfile(database_path, self.target_path)
                return self.connection.execute("SELECT 1")
            if statement.startswith("DETACH DATABASE"):
                return self.connection.execute("SELECT 1")
            return self.connection.execute(statement, parameters)

        def close(self) -> None:
            self.connection.close()

    fake_sqlcipher = SimpleNamespace(
        connect=lambda path: ExportConnection(path),
        DatabaseError=sqlite3.DatabaseError,
    )
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

    monkeypatch.setattr(db_module, "_validate_encrypted_database", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(db_module, "_create_private_rollback_copy", create_rollback, raising=False)
    monkeypatch.setattr(
        db_module,
        "_fsync_file",
        lambda path: events.append(f"fsync:{Path(path).name}"),
        raising=False,
    )
    monkeypatch.setattr(
        db_module,
        "_fsync_parent_directory",
        lambda path: events.append(f"sync-parent:{Path(path).name}"),
        raising=False,
    )
    monkeypatch.setattr(db_module.os, "replace", replace)
    monkeypatch.setattr(db_module.os, "unlink", unlink)

    db_module._migrate_plaintext_application_database(
        str(database_path),
        sqlcipher_module=fake_sqlcipher,
        database_key=b"k" * 32,
    )

    assert events == [
        "fsync:ordered-application.db.encrypted.tmp",
        "copy:rollback",
        "fsync:ordered-application.db.plaintext.rollback",
        "sync-parent:ordered-application.db",
        "replace:ordered-application.db.encrypted.tmp->ordered-application.db",
        "sync-parent:ordered-application.db",
        "fsync:ordered-application.db",
        "sync-parent:ordered-application.db",
        "unlink:ordered-application.db.plaintext.rollback",
        "sync-parent:ordered-application.db",
    ]


def test_init_db_preserves_plaintext_database_when_wal_checkpoint_is_busy(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Busy WAL checkpoint must stop initialization without replacing existing data."""
    from types import SimpleNamespace

    import yinshi.db as db_module
    from yinshi.config import get_settings

    database_path = tmp_path / "busy-application.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('original')")

    class Result:
        def fetchone(self) -> tuple[int, int, int]:
            return (1, 4, 2)

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
    monkeypatch.setenv("DB_PATH", str(database_path))
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    get_settings.cache_clear()
    monkeypatch.setattr(db_module, "_load_sqlcipher_module", lambda: fake_sqlcipher)
    monkeypatch.setattr(
        db_module,
        "_open_keyed_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            db_module.DatabaseEncryptionError("plaintext")
        ),
    )
    monkeypatch.setattr(
        db_module,
        "_remove_sqlite_sidecars",
        lambda _path: events.append("remove-sidecars"),
    )
    monkeypatch.setattr(
        db_module.os,
        "replace",
        lambda _source, _target: events.append("replace"),
    )

    with pytest.raises(db_module.DatabaseEncryptionError, match="WAL checkpoint"):
        db_module.init_db()

    assert events == []
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "original"


def test_application_migration_failure_durably_restores_original(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Application migration failure should restore and sync plaintext primary."""
    import os
    import shutil
    from pathlib import Path
    from types import SimpleNamespace

    import yinshi.db as db_module

    database_path = tmp_path / "restore-application.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('original')")

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
                shutil.copyfile(database_path, self.target_path)
                return self.connection.execute("SELECT 1")
            if statement.startswith("DETACH DATABASE"):
                return self.connection.execute("SELECT 1")
            return self.connection.execute(statement, parameters)

        def close(self) -> None:
            self.connection.close()

    fake_sqlcipher = SimpleNamespace(
        connect=lambda path: ExportConnection(path),
        DatabaseError=sqlite3.DatabaseError,
    )
    validation_count = 0

    def validate(*_args, **_kwargs) -> None:
        nonlocal validation_count
        validation_count += 1
        if validation_count == 2:
            raise db_module.DatabaseEncryptionError("replacement invalid")

    events: list[str] = []
    original_replace = os.replace

    def replace(source_path: str, target_path: str) -> None:
        events.append(f"replace:{Path(source_path).name}->{Path(target_path).name}")
        original_replace(source_path, target_path)

    monkeypatch.setattr(db_module, "_validate_encrypted_database", validate)
    monkeypatch.setattr(
        db_module,
        "_create_private_rollback_copy",
        lambda source, rollback: shutil.copyfile(source, rollback),
        raising=False,
    )
    monkeypatch.setattr(
        db_module,
        "_fsync_file",
        lambda path: events.append(f"fsync:{Path(path).name}"),
        raising=False,
    )
    monkeypatch.setattr(
        db_module,
        "_fsync_parent_directory",
        lambda path: events.append(f"sync-parent:{Path(path).name}"),
        raising=False,
    )
    monkeypatch.setattr(db_module.os, "replace", replace)

    with pytest.raises(db_module.DatabaseEncryptionError, match="replacement invalid"):
        db_module._migrate_plaintext_application_database(
            str(database_path),
            sqlcipher_module=fake_sqlcipher,
            database_key=b"k" * 32,
        )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "original"
    restore_index = events.index(
        "replace:restore-application.db.plaintext.rollback->restore-application.db"
    )
    assert events[restore_index + 1 :] == [
        "fsync:restore-application.db",
        "sync-parent:restore-application.db",
    ]


def test_init_db_recovers_rollback_when_encryption_policy_is_disabled(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy changes must not create an empty primary beside the only valid rollback."""
    import os
    from pathlib import Path

    import yinshi.db as db_module
    from yinshi.config import get_settings

    database_path = tmp_path / "policy-change.db"
    rollback_path = Path(f"{database_path}.plaintext.rollback")
    with sqlite3.connect(rollback_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('durable')")
    os.chmod(rollback_path, 0o600)
    monkeypatch.setenv("DB_PATH", str(database_path))
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    get_settings.cache_clear()

    db_module.init_db()

    assert database_path.exists()
    assert not rollback_path.exists()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "durable"


def test_restart_recovers_application_rollback_when_primary_is_absent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart recovery should restore the only valid application database."""
    import os
    from pathlib import Path

    import yinshi.db as db_module

    database_path = tmp_path / "restart-application.db"
    rollback_path = Path(f"{database_path}.plaintext.rollback")
    with sqlite3.connect(rollback_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('durable')")
    os.chmod(rollback_path, 0o600)

    events: list[str] = []
    monkeypatch.setattr(
        db_module,
        "_fsync_file",
        lambda path: events.append(f"fsync:{Path(path).name}"),
        raising=False,
    )
    monkeypatch.setattr(
        db_module,
        "_fsync_parent_directory",
        lambda path: events.append(f"sync-parent:{Path(path).name}"),
        raising=False,
    )

    db_module._recover_plaintext_migration_rollback(str(database_path))

    assert database_path.exists()
    assert not rollback_path.exists()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "durable"
    assert events == [
        "fsync:restart-application.db",
        "sync-parent:restart-application.db",
    ]


def test_init_db_rejects_symlink_migration_rollback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initialization must not recover a rollback through symlink indirection."""
    import os

    import yinshi.db as db_module
    from yinshi.config import get_settings

    database_path = tmp_path / "symlink-primary.db"
    target_path = tmp_path / "symlink-target.db"
    rollback_path = tmp_path / "symlink-primary.db.plaintext.rollback"
    with sqlite3.connect(target_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('target')")
    os.chmod(target_path, 0o600)
    rollback_path.symlink_to(target_path)
    monkeypatch.setenv("DB_PATH", str(database_path))
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    get_settings.cache_clear()

    with pytest.raises(db_module.DatabaseEncryptionError, match="trusted regular file"):
        db_module.init_db()

    assert not database_path.exists()
    assert rollback_path.is_symlink()
    with sqlite3.connect(target_path) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "target"


def test_init_db_rejects_changed_migration_rollback_inode(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initialization must reject a rollback path that differs from its open file."""
    import os
    from types import SimpleNamespace

    import yinshi.db as db_module
    from yinshi.config import get_settings

    database_path = tmp_path / "changed-primary.db"
    rollback_path = tmp_path / "changed-primary.db.plaintext.rollback"
    with sqlite3.connect(rollback_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    os.chmod(rollback_path, 0o600)
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

    monkeypatch.setenv("DB_PATH", str(database_path))
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    get_settings.cache_clear()
    monkeypatch.setattr(db_module.os, "lstat", changed_lstat)

    with pytest.raises(db_module.DatabaseEncryptionError, match="trusted regular file"):
        db_module.init_db()

    assert not database_path.exists()
    assert rollback_path.exists()


def test_init_db_rejects_nonprivate_migration_rollback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initialization must reject a rollback writable or readable by other users."""
    import os

    import yinshi.db as db_module
    from yinshi.config import get_settings

    database_path = tmp_path / "nonprivate-primary.db"
    rollback_path = tmp_path / "nonprivate-primary.db.plaintext.rollback"
    with sqlite3.connect(rollback_path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    os.chmod(rollback_path, 0o644)
    monkeypatch.setenv("DB_PATH", str(database_path))
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    get_settings.cache_clear()

    with pytest.raises(db_module.DatabaseEncryptionError, match="trusted regular file"):
        db_module.init_db()

    assert not database_path.exists()
    assert rollback_path.exists()


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


def test_control_db_migrates_runner_kind_without_losing_grants(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing runner rows become BYOC while transfer grant foreign keys remain valid."""
    control_path = tmp_path / "control.db"
    monkeypatch.setenv("CONTROL_DB_PATH", str(control_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")

    connection = sqlite3.connect(control_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            email TEXT NOT NULL UNIQUE,
            credit_used_cents INTEGER DEFAULT 0,
            credit_limit_cents INTEGER DEFAULT 500
        )
        """)
    connection.execute("""
        CREATE TABLE user_runners (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            cloud_provider TEXT NOT NULL,
            region TEXT NOT NULL,
            status TEXT DEFAULT 'pending' NOT NULL,
            registration_token_hash TEXT,
            registration_token_expires_at TEXT,
            runner_token_hash TEXT,
            registered_at TEXT,
            last_heartbeat_at TEXT,
            runner_version TEXT,
            capabilities_json TEXT DEFAULT '{}' NOT NULL,
            data_dir TEXT,
            revoked_at TEXT,
            noise_public_key TEXT,
            noise_public_key_confirmed_at TEXT
        )
        """)
    connection.execute("""
        CREATE TABLE runner_transfer_grants (
            transfer_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            runner_id TEXT NOT NULL REFERENCES user_runners(id) ON DELETE CASCADE,
            capability_hash TEXT UNIQUE NOT NULL,
            expires_at INTEGER NOT NULL,
            max_session_bytes INTEGER NOT NULL,
            claimed_at INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
        """)
    connection.execute(
        "INSERT INTO users (id, email) VALUES (?, ?)",
        ("user-1", "runner@example.com"),
    )
    connection.execute(
        """
        INSERT INTO user_runners (
            id, user_id, name, cloud_provider, region, status,
            runner_token_hash, runner_version, capabilities_json, data_dir
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "runner-1",
            "user-1",
            "Existing runner",
            "aws",
            "us-west-2",
            "online",
            "runner-token-digest",
            "1.2.3",
            '{"sqlite":true}',
            "/var/lib/yinshi",
        ),
    )
    connection.execute(
        """
        INSERT INTO runner_transfer_grants (
            transfer_id, user_id, runner_id, capability_hash,
            expires_at, max_session_bytes
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("transfer-1", "user-1", "runner-1", "capability-digest", 200, 4096),
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
            runner = database.execute(
                "SELECT * FROM user_runners WHERE id = ?",
                ("runner-1",),
            ).fetchone()
            grant = database.execute(
                "SELECT * FROM runner_transfer_grants WHERE transfer_id = ?",
                ("transfer-1",),
            ).fetchone()
            foreign_key_issues = database.execute("PRAGMA foreign_key_check").fetchall()

        assert runner is not None
        assert runner["kind"] == "byoc"
        assert runner["name"] == "Existing runner"
        assert runner["runner_token_hash"] == "runner-token-digest"
        assert runner["capabilities_json"] == '{"sqlite":true}'
        assert grant is not None
        assert grant["runner_id"] == "runner-1"
        assert foreign_key_issues == []
    finally:
        get_settings.cache_clear()


def test_control_db_migrates_pre_kind_runner_before_restore_job_binding(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old runner schemas must gain kind and restore-job columns without data loss."""
    control_path = tmp_path / "control.db"
    monkeypatch.setenv("CONTROL_DB_PATH", str(control_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    connection = sqlite3.connect(control_path)
    connection.executescript("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            credit_used_cents INTEGER DEFAULT 0, credit_limit_cents INTEGER DEFAULT 500
        );
        CREATE TABLE user_runners (
            id TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL, cloud_provider TEXT NOT NULL, region TEXT NOT NULL,
            status TEXT DEFAULT 'pending' NOT NULL, registration_token_hash TEXT,
            registration_token_expires_at TEXT, runner_token_hash TEXT,
            registered_at TEXT, last_heartbeat_at TEXT, runner_version TEXT,
            capabilities_json TEXT DEFAULT '{}' NOT NULL, data_dir TEXT,
            revoked_at TEXT, noise_public_key TEXT, noise_public_key_confirmed_at TEXT
        );
        INSERT INTO users (id, email) VALUES ('user-1', 'runner@example.com');
        INSERT INTO user_runners (
            id, user_id, name, cloud_provider, region
        ) VALUES ('runner-1', 'user-1', 'Existing', 'aws', 'us-west-2');
        """)
    connection.commit()
    connection.close()

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            columns = {row[1] for row in database.execute("PRAGMA table_info(user_runners)")}
            runner = database.execute(
                "SELECT kind, restore_job_id FROM user_runners WHERE id = 'runner-1'"
            ).fetchone()
        assert {"kind", "restore_job_id"} <= columns
        assert runner is not None
        assert runner["kind"] == "byoc"
        assert runner["restore_job_id"] is None
    finally:
        get_settings.cache_clear()


def test_control_db_migrates_runner_kinds_with_existing_managed_runtime_triggers(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployed control database should preserve its user and GitHub installation."""
    control_path = tmp_path / "control.db"
    monkeypatch.setenv("CONTROL_DB_PATH", str(control_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "b" * 64)
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "enabled")
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "required")

    from yinshi.config import get_settings
    from yinshi.db import (
        _application_database_key,
        _load_sqlcipher_module,
        _open_keyed_connection,
    )

    get_settings.cache_clear()
    connection = _open_keyed_connection(
        str(control_path),
        sqlcipher_module=_load_sqlcipher_module(),
        database_key=_application_database_key(context="control"),
    )
    connection.executescript("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            credit_used_cents INTEGER DEFAULT 0, credit_limit_cents INTEGER DEFAULT 500
        );
        CREATE TABLE user_runners (
            id TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL, cloud_provider TEXT NOT NULL, region TEXT NOT NULL,
            status TEXT DEFAULT 'pending' NOT NULL, registration_token_hash TEXT,
            registration_token_expires_at TEXT, runner_token_hash TEXT,
            registered_at TEXT, last_heartbeat_at TEXT, runner_version TEXT,
            capabilities_json TEXT DEFAULT '{}' NOT NULL, data_dir TEXT,
            revoked_at TEXT, noise_public_key TEXT, noise_public_key_confirmed_at TEXT
        );
        CREATE TABLE managed_runtimes (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            runner_id TEXT NOT NULL UNIQUE REFERENCES user_runners(id) ON DELETE CASCADE,
            provider_name TEXT NOT NULL,
            sprite_external_id TEXT NOT NULL UNIQUE,
            lifecycle_status TEXT NOT NULL,
            generation INTEGER DEFAULT 1 NOT NULL,
            artifact_version TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            last_error TEXT
        );
        CREATE TRIGGER validate_managed_runtime_runner_insert
        BEFORE INSERT ON managed_runtimes
        WHEN NOT EXISTS (
            SELECT 1 FROM user_runners
            WHERE id = NEW.runner_id AND user_id = NEW.user_id AND kind = 'managed'
        )
        BEGIN
            SELECT RAISE(ABORT, 'managed runtime must reference matching managed runner');
        END;
        CREATE TRIGGER validate_managed_runtime_runner_update
        BEFORE UPDATE OF user_id, runner_id ON managed_runtimes
        WHEN NOT EXISTS (
            SELECT 1 FROM user_runners
            WHERE id = NEW.runner_id AND user_id = NEW.user_id AND kind = 'managed'
        )
        BEGIN
            SELECT RAISE(ABORT, 'managed runtime must reference matching managed runner');
        END;
        CREATE TRIGGER protect_linked_managed_runner_update
        BEFORE UPDATE OF user_id ON user_runners
        WHEN EXISTS (SELECT 1 FROM managed_runtimes WHERE runner_id = OLD.id)
        BEGIN
            SELECT RAISE(ABORT, 'cannot change linked managed runtime runner');
        END;
        CREATE TABLE github_installations (
            id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            installation_id INTEGER NOT NULL,
            account_login TEXT NOT NULL,
            account_type TEXT NOT NULL,
            html_url TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, installation_id)
        );
        INSERT INTO users (id, email) VALUES ('user-1', 'runner@example.com');
        INSERT INTO user_runners (
            id, user_id, name, cloud_provider, region
        ) VALUES ('runner-1', 'user-1', 'Existing', 'aws', 'us-west-2');
        INSERT INTO github_installations (
            user_id, installation_id, account_login, account_type, html_url
        ) VALUES ('user-1', 12345, 'example', 'User', 'https://github.com/example');
        """)
    connection.commit()
    connection.close()

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            runner = database.execute(
                "SELECT kind FROM user_runners WHERE id = ?", ("runner-1",)
            ).fetchone()
            installation = database.execute(
                "SELECT installation_id FROM github_installations WHERE user_id = ?",
                ("user-1",),
            ).fetchone()
            trigger_names = {
                row["name"]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type = ? AND name IN (?, ?, ?)",
                    (
                        "trigger",
                        "validate_managed_runtime_runner_insert",
                        "validate_managed_runtime_runner_update",
                        "protect_linked_managed_runner_update",
                    ),
                )
            }
        assert runner is not None
        assert runner["kind"] == "byoc"
        assert installation is not None
        assert installation["installation_id"] == 12345
        assert trigger_names == {
            "validate_managed_runtime_runner_insert",
            "validate_managed_runtime_runner_update",
            "protect_linked_managed_runner_update",
        }
    finally:
        get_settings.cache_clear()


def test_control_db_expands_existing_managed_runner_roles(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing managed runners should survive replacement-role migration."""
    control_path = tmp_path / "control.db"
    monkeypatch.setenv("CONTROL_DB_PATH", str(control_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    connection = sqlite3.connect(control_path)
    connection.executescript("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            credit_used_cents INTEGER DEFAULT 0, credit_limit_cents INTEGER DEFAULT 500
        );
        CREATE TABLE user_runners (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT DEFAULT 'byoc' NOT NULL CHECK (kind IN ('byoc', 'managed')),
            name TEXT NOT NULL, cloud_provider TEXT NOT NULL, region TEXT NOT NULL,
            status TEXT DEFAULT 'pending' NOT NULL, registration_token_hash TEXT,
            registration_token_expires_at TEXT, runner_token_hash TEXT,
            registered_at TEXT, last_heartbeat_at TEXT, runner_version TEXT,
            capabilities_json TEXT DEFAULT '{}' NOT NULL, data_dir TEXT,
            revoked_at TEXT, noise_public_key TEXT, noise_public_key_confirmed_at TEXT,
            UNIQUE(user_id, kind)
        );
        INSERT INTO users (id, email) VALUES ('user-1', 'runner@example.com');
        INSERT INTO user_runners (
            id, user_id, kind, name, cloud_provider, region
        ) VALUES ('runner-1', 'user-1', 'managed', 'Managed', 'fly_sprites', 'ord');
        """)
    connection.commit()
    connection.close()
    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            runner = database.execute("SELECT * FROM user_runners WHERE id = 'runner-1'").fetchone()
            database.execute("""INSERT INTO user_runners (
                       id, user_id, kind, name, cloud_provider, region
                   ) VALUES (
                       'candidate-1', 'user-1', 'managed_restore',
                       'Candidate', 'fly_sprites', 'ord'
                   )""")
        assert runner is not None
        assert runner["kind"] == "managed"
    finally:
        get_settings.cache_clear()


def test_runner_table_replacement_drops_dependent_triggers_first(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No trigger may reference user_runners while that table is replaced."""
    control_path = tmp_path / "control.db"
    monkeypatch.setenv("CONTROL_DB_PATH", str(control_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    connection = sqlite3.connect(control_path)
    connection.executescript("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            credit_used_cents INTEGER DEFAULT 0, credit_limit_cents INTEGER DEFAULT 500
        );
        CREATE TABLE user_runners (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT DEFAULT 'byoc' NOT NULL CHECK (kind IN ('byoc', 'managed')),
            name TEXT NOT NULL, cloud_provider TEXT NOT NULL, region TEXT NOT NULL,
            status TEXT DEFAULT 'pending' NOT NULL, registration_token_hash TEXT,
            registration_token_expires_at TEXT, runner_token_hash TEXT,
            registered_at TEXT, last_heartbeat_at TEXT, runner_version TEXT,
            capabilities_json TEXT DEFAULT '{}' NOT NULL, data_dir TEXT,
            revoked_at TEXT, noise_public_key TEXT, noise_public_key_confirmed_at TEXT,
            restore_job_id TEXT,
            UNIQUE(user_id, kind)
        );
        INSERT INTO users (id, email) VALUES ('user-1', 'runner@example.com');
        INSERT INTO user_runners (
            id, user_id, kind, name, cloud_provider, region
        ) VALUES ('runner-1', 'user-1', 'managed', 'Managed', 'fly_sprites', 'ord');
        """)
    connection.commit()
    connection.close()
    from yinshi.config import get_settings
    from yinshi.db import CONTROL_SCHEMA_SQL, _migrate_control

    get_settings.cache_clear()
    database = sqlite3.connect(control_path)
    database.row_factory = sqlite3.Row
    drop_table_statements: list[str] = []

    class GuardedConnection:
        def __init__(self, guarded: sqlite3.Connection) -> None:
            self._guarded = guarded

        def execute(self, sql: str, parameters: object = ()) -> object:
            if "DROP TABLE user_runners" in sql:
                drop_table_statements.append(sql)
                dangling = self._guarded.execute("""SELECT name FROM sqlite_master
                       WHERE type = 'trigger' AND sql LIKE '%user_runners%'""").fetchall()
                surviving = [row["name"] for row in dangling]
                assert not surviving, (
                    f"triggers {surviving} still reference user_runners "
                    "while it is being replaced"
                )
            return self._guarded.execute(sql, parameters)

        def executescript(self, script: str) -> None:
            self._guarded.executescript(script)

        def commit(self) -> None:
            self._guarded.commit()

        def rollback(self) -> None:
            self._guarded.rollback()

    try:
        database.executescript(CONTROL_SCHEMA_SQL)
        _migrate_control(GuardedConnection(database))
        assert drop_table_statements, "runner table replacement must run during migration"
        surviving = database.execute("""SELECT name FROM sqlite_master
               WHERE type = 'trigger' AND sql LIKE '%user_runners%'""").fetchall()
        assert {row["name"] for row in surviving} == {
            "update_user_runners_updated_at",
            "validate_managed_runtime_runner_insert",
            "validate_managed_runtime_runner_update",
            "protect_linked_managed_runner_update",
        }
    finally:
        database.close()
        get_settings.cache_clear()


def test_activation_guard_trigger_replacement_rolls_back_on_failure(
    tmp_path,
) -> None:
    """A failed guard-trigger replacement leaves the existing triggers intact."""
    from yinshi.db import CONTROL_SCHEMA_SQL, _migrate_managed_runtime_activation_guards

    database = sqlite3.connect(tmp_path / "control.db")
    database.row_factory = sqlite3.Row
    database.executescript(CONTROL_SCHEMA_SQL)
    database.commit()
    trigger_names = (
        "update_user_runners_updated_at",
        "validate_managed_runtime_runner_insert",
        "validate_managed_runtime_runner_update",
        "protect_linked_managed_runner_update",
    )

    def trigger_snapshot() -> dict[str, str]:
        return {
            row["name"]: row["sql"]
            for row in database.execute(
                """SELECT name, sql FROM sqlite_master WHERE type = 'trigger'
                   AND name IN (?, ?, ?, ?)""",
                trigger_names,
            )
        }

    snapshot_before = trigger_snapshot()
    assert set(snapshot_before) == set(trigger_names)

    class FailingRecreationConnection:
        def __init__(self, guarded: sqlite3.Connection) -> None:
            self._guarded = guarded

        def execute(self, sql: str, parameters: object = ()) -> object:
            if sql.lstrip().startswith("CREATE TRIGGER"):
                raise sqlite3.OperationalError("injected trigger recreation failure")
            return self._guarded.execute(sql, parameters)

        def commit(self) -> None:
            self._guarded.commit()

        def rollback(self) -> None:
            self._guarded.rollback()

    try:
        with pytest.raises(sqlite3.OperationalError, match="injected trigger recreation failure"):
            _migrate_managed_runtime_activation_guards(FailingRecreationConnection(database))
        assert trigger_snapshot() == snapshot_before
    finally:
        database.close()


def test_runner_kind_migration_rolls_back_invalid_foreign_keys(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid old references leave the original runner table intact."""
    control_path = tmp_path / "control.db"
    monkeypatch.setenv("CONTROL_DB_PATH", str(control_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")

    connection = sqlite3.connect(control_path)
    connection.executescript("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            credit_used_cents INTEGER DEFAULT 0,
            credit_limit_cents INTEGER DEFAULT 500
        );
        CREATE TABLE user_runners (
            id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            cloud_provider TEXT NOT NULL,
            region TEXT NOT NULL,
            status TEXT DEFAULT 'pending' NOT NULL,
            registration_token_hash TEXT,
            registration_token_expires_at TEXT,
            runner_token_hash TEXT,
            registered_at TEXT,
            last_heartbeat_at TEXT,
            runner_version TEXT,
            capabilities_json TEXT DEFAULT '{}' NOT NULL,
            data_dir TEXT,
            revoked_at TEXT,
            noise_public_key TEXT,
            noise_public_key_confirmed_at TEXT
        );
        CREATE TABLE runner_transfer_grants (
            transfer_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            runner_id TEXT NOT NULL REFERENCES user_runners(id) ON DELETE CASCADE,
            capability_hash TEXT UNIQUE NOT NULL,
            expires_at INTEGER NOT NULL,
            max_session_bytes INTEGER NOT NULL,
            claimed_at INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        );
        INSERT INTO runner_transfer_grants (
            transfer_id, user_id, runner_id, capability_hash,
            expires_at, max_session_bytes
        ) VALUES ('invalid-transfer', 'missing-user', 'missing-runner', 'digest', 200, 4096);
        """)
    connection.close()

    from yinshi.config import get_settings
    from yinshi.db import init_control_db

    get_settings.cache_clear()
    try:
        for _ in range(2):
            with pytest.raises(sqlite3.IntegrityError, match="invalid foreign key"):
                init_control_db()
        with sqlite3.connect(control_path) as database:
            columns = {row[1] for row in database.execute("PRAGMA table_info(user_runners)")}
        assert "kind" not in columns
    finally:
        get_settings.cache_clear()


def test_managed_backup_operation_schema_tracks_resumable_job_ownership(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed maintenance jobs should persist phase, lease, retry, and provider IDs."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            columns = {
                row[1] for row in database.execute("PRAGMA table_info(managed_backup_operations)")
            }
        assert {
            "phase",
            "lease_owner",
            "lease_token",
            "lease_expires_at",
            "attempt_count",
            "next_attempt_at",
            "source_runner_id",
            "source_sprite_id",
            "candidate_runner_id",
            "candidate_sprite_id",
            "activation_generation",
        } <= columns
    finally:
        get_settings.cache_clear()


def test_managed_runtime_upgrade_installs_fenced_activation_triggers(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing control databases should gain guarded replacement activation."""
    control_path = tmp_path / "control.db"
    monkeypatch.setenv("CONTROL_DB_PATH", str(control_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            database.execute("DROP TABLE managed_runtime_activation_guards")
            database.execute("DROP TRIGGER validate_managed_runtime_runner_update")
            database.execute("DROP TRIGGER protect_linked_managed_runner_update")
            database.executescript("""CREATE TRIGGER validate_managed_runtime_runner_update
                   BEFORE UPDATE OF user_id, runner_id ON managed_runtimes
                   BEGIN SELECT RAISE(ABORT, 'legacy'); END;
                   CREATE TRIGGER protect_linked_managed_runner_update
                   BEFORE UPDATE OF user_id, kind ON user_runners
                   BEGIN SELECT RAISE(ABORT, 'legacy'); END;""")
            database.commit()
        init_control_db()
        with get_control_db() as database:
            guard = database.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'managed_runtime_activation_guards'"
            ).fetchone()
            triggers = {
                row["name"]: row["sql"]
                for row in database.execute("""SELECT name, sql FROM sqlite_master
                       WHERE type = 'trigger' AND name IN (
                           'validate_managed_runtime_runner_update',
                           'protect_linked_managed_runner_update'
                       )""").fetchall()
            }
        assert guard is not None
        assert (
            "managed_runtime_activation_guards"
            in triggers["validate_managed_runtime_runner_update"]
        )
        assert (
            "managed_runtime_activation_guards" in triggers["protect_linked_managed_runner_update"]
        )
    finally:
        get_settings.cache_clear()


def test_managed_backup_schema_expands_existing_archive_status_constraint(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted upgrades should permit durable deleted archive tombstones."""
    control_path = tmp_path / "control.db"
    monkeypatch.setenv("CONTROL_DB_PATH", str(control_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    connection = sqlite3.connect(control_path)
    connection.executescript("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            credit_used_cents INTEGER DEFAULT 0, credit_limit_cents INTEGER DEFAULT 500
        );
        CREATE TABLE managed_backup_archives (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            runtime_generation INTEGER NOT NULL CHECK (runtime_generation > 0),
            status TEXT NOT NULL CHECK (
                status IN ('creating', 'uploaded', 'ready', 'failed', 'deleting')
            ),
            object_key TEXT NOT NULL UNIQUE, object_version TEXT, size_bytes INTEGER,
            sha256 TEXT, wrapped_key BLOB NOT NULL, key_id TEXT NOT NULL,
            owner_digest TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
            last_error TEXT
        );
        CREATE TABLE managed_backup_operations (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            job_id TEXT NOT NULL UNIQUE,
            archive_id TEXT NOT NULL REFERENCES managed_backup_archives(id) ON DELETE CASCADE,
            operation TEXT NOT NULL, status TEXT NOT NULL, runtime_generation INTEGER NOT NULL,
            started_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_error TEXT
        );
        INSERT INTO users (id, email) VALUES ('user-1', 'backup@example.com');
        INSERT INTO managed_backup_archives (
            id, user_id, runtime_generation, status, object_key, object_version,
            size_bytes, sha256, wrapped_key, key_id, owner_digest, created_at
        ) VALUES (
            'archive-1', 'user-1', 1, 'ready', 'managed/archive.enc', 'version-1',
            10, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            X'01', 'backup-v1',
            'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
            '2026-08-12T12:00:00Z'
        );
        INSERT INTO managed_backup_operations (
            user_id, job_id, archive_id, operation, status, runtime_generation,
            started_at, updated_at
        ) VALUES (
            'user-1', 'job-1', 'archive-1', 'restore', 'running', 1,
            '2026-08-12T12:00:00Z', '2026-08-12T12:00:00Z'
        );
        """)
    connection.commit()
    connection.close()
    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            database.execute(
                "UPDATE managed_backup_archives SET status = 'deleted' WHERE id = 'archive-1'"
            )
            database.commit()
            archive = database.execute(
                "SELECT status FROM managed_backup_archives WHERE id = 'archive-1'"
            ).fetchone()
            reference = database.execute(
                "PRAGMA foreign_key_list(managed_backup_operations)"
            ).fetchone()
            database.execute("DELETE FROM managed_backup_operations WHERE job_id = 'job-1'")
            database.commit()
        assert archive is not None
        assert archive["status"] == "deleted"
        assert reference is not None
        assert reference["table"] == "managed_backup_archives"
    finally:
        get_settings.cache_clear()


def test_managed_backup_schema_adds_job_columns_to_existing_tables(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted upgrades should add resumable fields to an existing backup table."""
    control_path = tmp_path / "control.db"
    monkeypatch.setenv("CONTROL_DB_PATH", str(control_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")
    connection = sqlite3.connect(control_path)
    connection.executescript("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            credit_used_cents INTEGER DEFAULT 0, credit_limit_cents INTEGER DEFAULT 500
        );
        CREATE TABLE managed_backup_archives (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, runtime_generation INTEGER NOT NULL,
            status TEXT NOT NULL, object_key TEXT NOT NULL UNIQUE, object_version TEXT,
            size_bytes INTEGER, sha256 TEXT, wrapped_key BLOB NOT NULL, key_id TEXT NOT NULL,
            owner_digest TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
            last_error TEXT
        );
        CREATE TABLE managed_backup_operations (
            user_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,
            archive_id TEXT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL,
            runtime_generation INTEGER NOT NULL, started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, last_error TEXT
        );
        INSERT INTO users (id, email) VALUES ('user-1', 'backup@example.com');
        INSERT INTO managed_backup_archives (
            id, user_id, runtime_generation, status, object_key, wrapped_key,
            key_id, owner_digest, created_at
        ) VALUES (
            'archive-1', 'user-1', 1, 'creating', 'managed/archive.enc', X'01',
            'backup-v1', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            '2026-08-12T12:00:00Z'
        );
        INSERT INTO managed_backup_operations (
            user_id, job_id, archive_id, operation, status, runtime_generation,
            started_at, updated_at
        ) VALUES (
            'user-1', 'job-1', 'archive-1', 'create', 'running', 1,
            '2026-08-12T12:00:00Z', '2026-08-12T12:00:00Z'
        );
        """)
    connection.commit()
    connection.close()
    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            operation = database.execute(
                "SELECT * FROM managed_backup_operations WHERE job_id = 'job-1'"
            ).fetchone()
        assert operation is not None
        assert operation["phase"] == "claimed"
        assert operation["attempt_count"] == 0
        assert operation["candidate_sprite_id"] is None
    finally:
        get_settings.cache_clear()


def test_managed_backup_schema_migrates_existing_operation_rows(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing create work should gain resumable defaults without losing identity."""
    control_path = tmp_path / "control.db"
    monkeypatch.setenv("CONTROL_DB_PATH", str(control_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            database.execute(
                "INSERT INTO users (id, email) VALUES ('user-1', 'backup@example.com')"
            )
            database.execute("""INSERT INTO user_runners (
                       id, user_id, kind, name, cloud_provider, region
                   ) VALUES (
                       'runner-1', 'user-1', 'managed', 'Managed', 'fly_sprites', 'ord'
                   )""")
            database.execute("""INSERT INTO managed_runtimes (
                       user_id, runner_id, provider_name, sprite_external_id,
                       lifecycle_status, artifact_version
                   ) VALUES (
                       'user-1', 'runner-1', 'fly_sprites', 'sprite-1', 'ready', 'v1'
                   )""")
            database.execute(
                """INSERT INTO managed_backup_archives (
                       id, user_id, runtime_generation, status, object_key,
                       wrapped_key, key_id, owner_digest, created_at
                   ) VALUES (
                       'archive-1', 'user-1', 1, 'creating', 'managed/archive.enc',
                       X'01', 'backup-v1', ?, '2026-08-12T12:00:00Z'
                   )""",
                ("a" * 64,),
            )
            database.execute("""INSERT INTO managed_backup_operations (
                       user_id, job_id, archive_id, operation, status,
                       runtime_generation, started_at, updated_at
                   ) VALUES (
                       'user-1', 'job-1', 'archive-1', 'create', 'running',
                       1, '2026-08-12T12:00:00Z', '2026-08-12T12:00:00Z'
                   )""")
            database.commit()
        init_control_db()
        with get_control_db() as database:
            operation = database.execute(
                "SELECT * FROM managed_backup_operations WHERE job_id = 'job-1'"
            ).fetchone()
        assert operation is not None
        assert operation["phase"] == "claimed"
        assert operation["attempt_count"] == 0
        assert operation["source_runner_id"] == "runner-1"
        assert operation["source_sprite_id"] == "sprite-1"
    finally:
        get_settings.cache_clear()


def test_managed_runtime_requires_the_users_managed_runner(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed runtime metadata must reference the managed runner owned by its user."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            database.execute(
                "INSERT INTO users (id, email) VALUES (?, ?)",
                ("user-1", "managed@example.com"),
            )
            database.execute(
                """
                INSERT INTO user_runners (id, user_id, kind, name, cloud_provider, region)
                VALUES (?, ?, 'byoc', ?, 'aws', ?)
                """,
                ("byoc-runner", "user-1", "BYOC runner", "us-east-1"),
            )
            database.execute(
                """
                INSERT INTO user_runners (id, user_id, kind, name, cloud_provider, region)
                VALUES (?, ?, 'managed', ?, 'fly_sprites', ?)
                """,
                ("managed-runner", "user-1", "Managed runner", "ord"),
            )

            with pytest.raises(sqlite3.IntegrityError, match="managed runner"):
                database.execute(
                    """
                    INSERT INTO managed_runtimes (
                        user_id, runner_id, provider_name, sprite_external_id,
                        lifecycle_status, generation, artifact_version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "user-1",
                        "byoc-runner",
                        "fly_sprites",
                        "sprite-1",
                        "provisioning",
                        1,
                        "worker-v1",
                    ),
                )

            database.execute(
                """
                INSERT INTO managed_runtimes (
                    user_id, runner_id, provider_name, sprite_external_id,
                    lifecycle_status, generation, artifact_version, last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "user-1",
                    "managed-runner",
                    "fly_sprites",
                    "sprite-1",
                    "ready",
                    2,
                    "worker-v2",
                    "Safe provider summary",
                ),
            )
            runtime = database.execute(
                "SELECT * FROM managed_runtimes WHERE user_id = ?",
                ("user-1",),
            ).fetchone()
            columns = {row[1] for row in database.execute("PRAGMA table_info(managed_runtimes)")}

        assert runtime is not None
        assert runtime["runner_id"] == "managed-runner"
        assert runtime["provider_name"] == "fly_sprites"
        assert runtime["sprite_external_id"] == "sprite-1"
        assert runtime["lifecycle_status"] == "ready"
        assert runtime["generation"] == 2
        assert runtime["artifact_version"] == "worker-v2"
        assert "token" not in " ".join(columns)
    finally:
        get_settings.cache_clear()


def test_managed_runtime_rejects_unsupported_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed runtime rows accept only the configured provider."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            database.execute(
                "INSERT INTO users (id, email) VALUES ('user-1', 'managed@example.com')"
            )
            database.execute("""
                INSERT INTO user_runners (id, user_id, kind, name, cloud_provider, region)
                VALUES ('runner-1', 'user-1', 'managed', 'Managed', 'fly_sprites', 'ord')
                """)
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                database.execute("""
                    INSERT INTO managed_runtimes (
                        user_id, runner_id, provider_name, sprite_external_id,
                        lifecycle_status, artifact_version
                    ) VALUES (
                        'user-1', 'runner-1', 'unsupported',
                        'sprite-1', 'ready', 'worker-v1'
                    )
                    """)
    finally:
        get_settings.cache_clear()


def test_managed_runtime_constrains_lifecycle_status(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed runtime lifecycle uses the control-plane state vocabulary."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            database.execute(
                "INSERT INTO users (id, email) VALUES ('user-1', 'managed@example.com')"
            )
            database.execute("""
                INSERT INTO user_runners (id, user_id, kind, name, cloud_provider, region)
                VALUES ('runner-1', 'user-1', 'managed', 'Managed', 'fly_sprites', 'ord')
                """)
            database.execute("""
                INSERT INTO managed_runtimes (
                    user_id, runner_id, provider_name, sprite_external_id,
                    lifecycle_status, artifact_version
                ) VALUES (
                    'user-1', 'runner-1', 'fly_sprites',
                    'sprite-1', 'provisioning', 'worker-v1'
                )
                """)
            for lifecycle_status in ("ready", "failed", "deleting"):
                database.execute(
                    "UPDATE managed_runtimes SET lifecycle_status = ? WHERE user_id = 'user-1'",
                    (lifecycle_status,),
                )
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                database.execute("UPDATE managed_runtimes SET lifecycle_status = 'unknown'")
    finally:
        get_settings.cache_clear()


def test_managed_runtime_external_id_is_unique(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One provider runtime identity cannot belong to two users."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            database.executemany(
                "INSERT INTO users (id, email) VALUES (?, ?)",
                (("user-1", "first@example.com"), ("user-2", "second@example.com")),
            )
            database.executemany(
                """
                INSERT INTO user_runners (
                    id, user_id, kind, name, cloud_provider, region
                ) VALUES (?, ?, 'managed', ?, 'fly_sprites', 'ord')
                """,
                (
                    ("runner-1", "user-1", "Managed one"),
                    ("runner-2", "user-2", "Managed two"),
                ),
            )
            database.execute("""
                INSERT INTO managed_runtimes (
                    user_id, runner_id, provider_name, sprite_external_id,
                    lifecycle_status, artifact_version
                ) VALUES (
                    'user-1', 'runner-1', 'fly_sprites',
                    'shared-sprite', 'ready', 'worker-v1'
                )
                """)
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
                database.execute("""
                    INSERT INTO managed_runtimes (
                        user_id, runner_id, provider_name, sprite_external_id,
                        lifecycle_status, artifact_version
                    ) VALUES (
                        'user-2', 'runner-2', 'fly_sprites',
                        'shared-sprite', 'ready', 'worker-v1'
                    )
                    """)
    finally:
        get_settings.cache_clear()


def test_linked_managed_runner_kind_cannot_change(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A managed runtime keeps its runner classified as managed."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            database.execute(
                "INSERT INTO users (id, email) VALUES (?, ?)",
                ("user-1", "managed@example.com"),
            )
            database.execute("""
                INSERT INTO user_runners (id, user_id, kind, name, cloud_provider, region)
                VALUES ('managed-runner', 'user-1', 'managed', 'Managed', 'fly_sprites', 'ord')
                """)
            database.execute("""
                INSERT INTO managed_runtimes (
                    user_id, runner_id, provider_name, sprite_external_id,
                    lifecycle_status, artifact_version
                ) VALUES (
                    'user-1', 'managed-runner', 'fly_sprites',
                    'sprite-1', 'ready', 'worker-v1'
                )
                """)
            with pytest.raises(sqlite3.IntegrityError, match="linked managed runtime"):
                database.execute(
                    "UPDATE user_runners SET kind = 'byoc' WHERE id = 'managed-runner'"
                )
    finally:
        get_settings.cache_clear()


def test_linked_managed_runner_owner_cannot_change(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A managed runtime keeps its runner assigned to the same user."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "disabled")

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    try:
        init_control_db()
        with get_control_db() as database:
            database.executemany(
                "INSERT INTO users (id, email) VALUES (?, ?)",
                (
                    ("user-1", "managed@example.com"),
                    ("user-2", "other@example.com"),
                ),
            )
            database.execute("""
                INSERT INTO user_runners (id, user_id, kind, name, cloud_provider, region)
                VALUES ('managed-runner', 'user-1', 'managed', 'Managed', 'fly_sprites', 'ord')
                """)
            database.execute("""
                INSERT INTO managed_runtimes (
                    user_id, runner_id, provider_name, sprite_external_id,
                    lifecycle_status, artifact_version
                ) VALUES (
                    'user-1', 'managed-runner', 'fly_sprites',
                    'sprite-1', 'ready', 'worker-v1'
                )
                """)
            with pytest.raises(sqlite3.IntegrityError, match="linked managed runtime"):
                database.execute(
                    "UPDATE user_runners SET user_id = 'user-2' WHERE id = 'managed-runner'"
                )
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
