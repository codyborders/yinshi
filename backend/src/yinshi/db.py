"""SQLite database connection and schema management."""

import importlib
import logging
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import cast

from yinshi.config import (
    control_field_encryption_enabled,
    get_settings,
    tenant_db_encryption_enabled,
    tenant_db_encryption_required,
)
from yinshi.model_catalog import DEFAULT_SESSION_MODEL

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 5
_SQLCIPHER_MODULE_NAMES = ("sqlcipher3.dbapi2", "pysqlcipher3.dbapi2")
_PLAINTEXT_ROLLBACK_SUFFIX = ".plaintext.rollback"


class DatabaseEncryptionError(RuntimeError):
    """Raised when a required application database cannot be encrypted or unlocked."""


SCHEMA_SQL = f"""
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS repos (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
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

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    repo_id TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    branch TEXT NOT NULL,
    path TEXT NOT NULL,
    state TEXT DEFAULT 'ready' NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'idle' NOT NULL,
    model TEXT DEFAULT '{DEFAULT_SESSION_MODEL}',
    pi_context_version INTEGER DEFAULT 1 NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT,
    full_message TEXT,
    turn_id TEXT,
    turn_status TEXT
);

CREATE TABLE IF NOT EXISTS prompt_runs (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    status TEXT DEFAULT 'starting' NOT NULL CHECK (
        status IN ('starting', 'running', 'stopping', 'completed', 'failed', 'cancelled', 'interrupted')
    ),
    UNIQUE(session_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS prompt_events (
    run_id TEXT NOT NULL REFERENCES prompt_runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    event_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY(run_id, sequence)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_prompt_runs_active_session
    ON prompt_runs(session_id)
    WHERE status IN ('starting', 'running', 'stopping');
CREATE INDEX IF NOT EXISTS idx_prompt_events_run
    ON prompt_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_turn_id ON messages(turn_id);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace_id);
CREATE INDEX IF NOT EXISTS idx_workspaces_repo ON workspaces(repo_id);

CREATE TRIGGER IF NOT EXISTS update_repos_updated_at AFTER UPDATE ON repos
BEGIN UPDATE repos SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS update_workspaces_updated_at AFTER UPDATE ON workspaces
BEGIN UPDATE workspaces SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS update_sessions_updated_at AFTER UPDATE ON sessions
BEGIN UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id; END;
"""


def _open_connection(db_path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a SQLite connection with standard settings."""
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _load_sqlcipher_module() -> ModuleType:
    """Load a compatible SQLCipher DB-API driver or raise a sanitized error."""
    for module_name in _SQLCIPHER_MODULE_NAMES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if hasattr(module, "connect") and hasattr(module, "Row"):
            return module
    raise DatabaseEncryptionError("SQLCipher is unavailable for required database encryption")


def _application_database_key(*, context: str) -> bytes:
    """Derive one database key from the configured Keychain-injected pepper."""
    if context not in {"control", "local"}:
        raise ValueError("database key context is invalid")
    from yinshi.services.crypto import derive_subkey

    settings = get_settings()
    return derive_subkey(
        settings.encryption_pepper_bytes,
        purpose="application-sqlcipher",
        context=context,
    )


def _sqlcipher_error_type(sqlcipher_module: ModuleType) -> type[Exception]:
    """Return a safe exception class exported by one SQLCipher driver."""
    database_error = getattr(sqlcipher_module, "DatabaseError", sqlite3.DatabaseError)
    if not isinstance(database_error, type) or not issubclass(database_error, Exception):
        return sqlite3.DatabaseError
    return database_error


def _open_keyed_connection(
    db_path: str,
    *,
    sqlcipher_module: ModuleType,
    database_key: bytes,
) -> sqlite3.Connection:
    """Open and immediately validate one SQLCipher database with an exact key."""
    if len(database_key) != 32:
        raise ValueError("database_key must contain 32 bytes")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = cast(sqlite3.Connection, sqlcipher_module.connect(db_path))
    connection.row_factory = getattr(sqlcipher_module, "Row")
    connection.execute(f"PRAGMA key = \"x'{database_key.hex()}'\"")  # noqa: S608
    connection.execute("PRAGMA cipher_memory_security = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        cipher_version = connection.execute("PRAGMA cipher_version").fetchone()
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except (sqlite3.DatabaseError, _sqlcipher_error_type(sqlcipher_module)) as error:
        connection.close()
        raise DatabaseEncryptionError("Application database could not be unlocked") from error
    if cipher_version is None or not cipher_version[0]:
        connection.close()
        raise DatabaseEncryptionError("SQLCipher driver did not report a cipher version")
    os.chmod(db_path, 0o600)
    return connection


def _plaintext_database_readable(db_path: str) -> bool:
    """Return whether an existing application database is plaintext SQLite."""
    if not os.path.isfile(db_path):
        return False
    try:
        connection = _open_connection(db_path)
        try:
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return True
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return False


def _validate_encrypted_database(
    db_path: str,
    *,
    sqlcipher_module: ModuleType,
    database_key: bytes,
) -> None:
    """Require an encrypted database to open and pass SQLCipher integrity checks."""
    connection = _open_keyed_connection(
        db_path,
        sqlcipher_module=sqlcipher_module,
        database_key=database_key,
    )
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise DatabaseEncryptionError(
                "Encrypted application database failed integrity validation"
            )
    finally:
        connection.close()


def _fsync_file(path: str) -> None:
    """Flush one regular file through its filesystem."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_parent_directory(path: str) -> None:
    """Flush directory entries for one filesystem path."""
    parent_path = os.path.dirname(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(parent_path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_private_rollback_copy(source_path: str, rollback_path: str) -> None:
    """Copy a plaintext database into a new owner-only rollback file."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(rollback_path, flags, 0o600)
    try:
        with open(source_path, "rb") as source, os.fdopen(descriptor, "wb") as rollback:
            descriptor = -1
            while chunk := source.read(1024 * 1024):
                rollback.write(chunk)
            rollback.flush()
            os.fchmod(rollback.fileno(), 0o600)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.exists(rollback_path):
            os.unlink(rollback_path)
        raise


def _remove_sqlite_sidecars(db_path: str) -> None:
    """Remove checkpointed WAL files and durably record their removal."""
    removed = False
    for suffix in ("-wal", "-shm"):
        sidecar_path = f"{db_path}{suffix}"
        if os.path.exists(sidecar_path):
            os.unlink(sidecar_path)
            removed = True
    if removed:
        _fsync_parent_directory(db_path)


def _application_rollback_is_trusted(descriptor: int, rollback_path: str) -> bool:
    """Return whether an open rollback still names one private owner-controlled file."""
    opened_stat = os.fstat(descriptor)
    try:
        path_stat = os.lstat(rollback_path)
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(opened_stat.st_mode)
        and stat.S_ISREG(path_stat.st_mode)
        and not stat.S_ISLNK(path_stat.st_mode)
        and (opened_stat.st_dev, opened_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)
        and opened_stat.st_uid == os.geteuid()
        and opened_stat.st_nlink == 1
        and opened_stat.st_mode & 0o077 == 0
    )


def _open_trusted_application_rollback(rollback_path: str) -> int | None:
    """Open and validate an application rollback without following links."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(rollback_path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DatabaseEncryptionError(
            "Application database migration rollback must be a trusted regular file"
        ) from exc
    if _application_rollback_is_trusted(descriptor, rollback_path):
        return descriptor
    os.close(descriptor)
    raise DatabaseEncryptionError(
        "Application database migration rollback must be a trusted regular file"
    )


def _recover_plaintext_migration_rollback(db_path: str) -> None:
    """Restore a durable rollback when an interrupted replacement lost its primary."""
    rollback_path = f"{db_path}{_PLAINTEXT_ROLLBACK_SUFFIX}"
    if os.path.lexists(db_path):
        return
    descriptor = _open_trusted_application_rollback(rollback_path)
    if descriptor is None:
        return
    try:
        os.fsync(descriptor)
        if os.path.lexists(db_path):
            return
        if not _application_rollback_is_trusted(descriptor, rollback_path):
            raise DatabaseEncryptionError(
                "Application database migration rollback must be a trusted regular file"
            )
        os.replace(rollback_path, db_path)
        os.chmod(db_path, 0o600)
        _fsync_file(db_path)
        _fsync_parent_directory(db_path)
    finally:
        os.close(descriptor)
    logger.warning("Recovered an interrupted application database migration")


def _remove_validated_migration_rollback(db_path: str) -> None:
    """Remove rollback only after the validated primary is durable."""
    rollback_path = f"{db_path}{_PLAINTEXT_ROLLBACK_SUFFIX}"
    if not os.path.exists(rollback_path):
        return
    _fsync_file(db_path)
    _fsync_parent_directory(db_path)
    os.unlink(rollback_path)
    _fsync_parent_directory(db_path)


def _migrate_plaintext_application_database(
    db_path: str,
    *,
    sqlcipher_module: ModuleType,
    database_key: bytes,
) -> None:
    """Export plaintext SQLite into SQLCipher and atomically replace the original."""
    temporary_path = f"{db_path}.encrypted.tmp"
    rollback_path = f"{db_path}{_PLAINTEXT_ROLLBACK_SUFFIX}"
    for path in (temporary_path, f"{temporary_path}-wal", f"{temporary_path}-shm"):
        if os.path.exists(path):
            os.unlink(path)

    if os.path.exists(rollback_path):
        _fsync_file(db_path)
        _fsync_parent_directory(db_path)
        os.unlink(rollback_path)
        _fsync_parent_directory(db_path)

    source = cast(sqlite3.Connection, sqlcipher_module.connect(db_path))
    try:
        checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if (
            checkpoint is None
            or len(checkpoint) < 3
            or int(checkpoint[0]) != 0
            or int(checkpoint[1]) != int(checkpoint[2])
        ):
            raise DatabaseEncryptionError("Application database WAL checkpoint did not complete")
        integrity = source.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise DatabaseEncryptionError(
                "Plaintext application database failed integrity validation"
            )
        source.execute(
            "ATTACH DATABASE ? AS encrypted KEY ?",
            (temporary_path, f"x'{database_key.hex()}'"),
        )
        try:
            source.execute("SELECT sqlcipher_export('encrypted')").fetchone()
        finally:
            source.execute("DETACH DATABASE encrypted")
    except (sqlite3.DatabaseError, _sqlcipher_error_type(sqlcipher_module)) as error:
        raise DatabaseEncryptionError("Application database encryption migration failed") from error
    finally:
        source.close()

    _remove_sqlite_sidecars(db_path)
    try:
        _validate_encrypted_database(
            temporary_path,
            sqlcipher_module=sqlcipher_module,
            database_key=database_key,
        )
        os.chmod(temporary_path, 0o600)
        _fsync_file(temporary_path)
        _create_private_rollback_copy(db_path, rollback_path)
        _fsync_file(rollback_path)
        _fsync_parent_directory(db_path)
        os.replace(temporary_path, db_path)
        _fsync_parent_directory(db_path)
        _validate_encrypted_database(
            db_path,
            sqlcipher_module=sqlcipher_module,
            database_key=database_key,
        )
        os.chmod(db_path, 0o600)
        _fsync_file(db_path)
        _fsync_parent_directory(db_path)
    except (DatabaseEncryptionError, OSError):
        if os.path.exists(rollback_path):
            os.replace(rollback_path, db_path)
            os.chmod(db_path, 0o600)
            _fsync_file(db_path)
            _fsync_parent_directory(db_path)
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
            _fsync_parent_directory(db_path)
        raise
    else:
        os.unlink(rollback_path)
        _fsync_parent_directory(db_path)
    _remove_sqlite_sidecars(db_path)
    logger.info("Migrated an application database to SQLCipher")


def _open_application_connection(db_path: str, *, context: str) -> sqlite3.Connection:
    """Open one application database under configured encryption policy."""
    settings = get_settings()
    _recover_plaintext_migration_rollback(db_path)
    if not tenant_db_encryption_enabled(settings):
        return _open_connection(db_path)
    try:
        sqlcipher_module = _load_sqlcipher_module()
    except DatabaseEncryptionError:
        if tenant_db_encryption_required(settings):
            raise
        logger.warning("SQLCipher unavailable; opening application database without encryption")
        return _open_connection(db_path)

    database_key = _application_database_key(context=context)
    if os.path.exists(db_path):
        try:
            connection = _open_keyed_connection(
                db_path,
                sqlcipher_module=sqlcipher_module,
                database_key=database_key,
            )
        except DatabaseEncryptionError:
            if not _plaintext_database_readable(db_path):
                raise
            _migrate_plaintext_application_database(
                db_path,
                sqlcipher_module=sqlcipher_module,
                database_key=database_key,
            )
        else:
            _remove_validated_migration_rollback(db_path)
            return connection
    return _open_keyed_connection(
        db_path,
        sqlcipher_module=sqlcipher_module,
        database_key=database_key,
    )


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Get a SQLite connection as a context manager."""
    settings = get_settings()
    conn = _open_application_connection(settings.db_path, context="local")
    try:
        yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply versioned schema migrations."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = row[0] if row else 0

    if current < 1:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(repos)").fetchall()]
        if "owner_email" not in columns:
            logger.info("Migration v1: adding owner_email column to repos")
            conn.execute("ALTER TABLE repos ADD COLUMN owner_email TEXT")

    if current < 2:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(repos)").fetchall()]
        if "installation_id" not in columns:
            logger.info("Migration v2: adding installation_id column to repos")
            conn.execute("ALTER TABLE repos ADD COLUMN installation_id INTEGER")

    if current < 3:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
        if "turn_status" not in columns:
            logger.info("Migration v3: adding turn_status column to messages")
            conn.execute("ALTER TABLE messages ADD COLUMN turn_status TEXT")

    if current < 4:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(repos)").fetchall()]
        if "agents_md" not in columns:
            logger.info("Migration v4: adding agents_md column to repos")
            conn.execute("ALTER TABLE repos ADD COLUMN agents_md TEXT")

    if current < 5:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "pi_context_version" not in columns:
            logger.info("Migration v5: adding pi_context_version column to sessions")
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN pi_context_version INTEGER DEFAULT 0 NOT NULL"
            )

    if current != _SCHEMA_VERSION:
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
        conn.commit()


def init_db() -> None:
    """Initialize the database schema."""
    logger.info("Initializing application database")
    try:
        with get_db() as conn:
            conn.executescript(SCHEMA_SQL)
            _migrate(conn)
    except sqlite3.Error:
        logger.error("Failed to initialize application database")
        raise
    logger.info("Database initialized")


# --- Control plane database (multi-tenant) ---

CONTROL_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT,
    avatar_url TEXT,
    status TEXT DEFAULT 'active' NOT NULL,
    tier TEXT DEFAULT 'free' NOT NULL,
    disk_quota_mb INTEGER DEFAULT 5000,
    disk_used_mb INTEGER DEFAULT 0,
    encrypted_dek BLOB,
    credit_used_cents INTEGER DEFAULT 0,
    credit_limit_cents INTEGER DEFAULT 500,
    last_login_at TIMESTAMP,
    deletion_requested_at TIMESTAMP,
    deletion_scheduled_for TIMESTAMP
);

CREATE TABLE IF NOT EXISTS oauth_identities (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    provider_email TEXT NOT NULL,
    provider_data TEXT,
    UNIQUE(provider, provider_user_id)
);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    encrypted_key BLOB NOT NULL,
    label TEXT DEFAULT '',
    last_used_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS provider_connections (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    auth_strategy TEXT NOT NULL,
    encrypted_secret BLOB NOT NULL,
    label TEXT DEFAULT '',
    config_json TEXT DEFAULT '{}' NOT NULL,
    status TEXT DEFAULT 'connected' NOT NULL,
    last_used_at TIMESTAMP,
    expires_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS desktop_authorization_requests (
    request_id_hash TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    state TEXT NOT NULL,
    user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
    authorization_code_hash TEXT,
    approved_at INTEGER,
    consumed_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_desktop_authorization_requests_expiry
ON desktop_authorization_requests(expires_at);

CREATE TABLE IF NOT EXISTS desktop_devices (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    refresh_token_hash TEXT NOT NULL UNIQUE,
    refresh_token_expires_at INTEGER NOT NULL,
    revoked_at INTEGER,
    last_seen_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_desktop_devices_user ON desktop_devices(user_id);

CREATE TABLE IF NOT EXISTS desktop_used_refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES desktop_devices(id) ON DELETE CASCADE,
    rotated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_desktop_used_refresh_tokens_device
ON desktop_used_refresh_tokens(device_id);

CREATE TABLE IF NOT EXISTS usage_log (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cost_cents REAL DEFAULT 0,
    key_source TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_oauth_user ON oauth_identities(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_provider_connections_user ON provider_connections(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_log(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_log(session_id);

CREATE TABLE IF NOT EXISTS github_installations (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    installation_id INTEGER NOT NULL,
    account_login TEXT NOT NULL,
    account_type TEXT NOT NULL,
    html_url TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, installation_id)
);

CREATE INDEX IF NOT EXISTS idx_github_installations_user ON github_installations(user_id);

CREATE TABLE IF NOT EXISTS github_install_flows (
    state_digest TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    installation_id INTEGER,
    expires_at INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_github_install_flows_user ON github_install_flows(user_id);
CREATE INDEX IF NOT EXISTS idx_github_install_flows_expiry ON github_install_flows(expires_at);

CREATE TABLE IF NOT EXISTS pi_configs (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_label TEXT NOT NULL,
    repo_url TEXT,
    available_categories TEXT DEFAULT '[]' NOT NULL,
    enabled_categories TEXT DEFAULT '[]' NOT NULL,
    last_synced_at TIMESTAMP,
    status TEXT DEFAULT 'ready' NOT NULL,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_pi_configs_user ON pi_configs(user_id);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    pi_settings_json TEXT DEFAULT '{}' NOT NULL,
    pi_settings_enabled INTEGER DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS user_runners (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT DEFAULT 'byoc' NOT NULL
        CHECK (kind IN ('byoc', 'managed', 'managed_restore', 'managed_retired')),
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
    noise_public_key_confirmed_at TEXT,
    restore_job_id TEXT,
    UNIQUE(user_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_user_runners_user ON user_runners(user_id);
CREATE INDEX IF NOT EXISTS idx_user_runners_registration_token
ON user_runners(registration_token_hash);
CREATE INDEX IF NOT EXISTS idx_user_runners_runner_token ON user_runners(runner_token_hash);

CREATE TABLE IF NOT EXISTS runner_transfer_grants (
    transfer_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    runner_id TEXT NOT NULL REFERENCES user_runners(id) ON DELETE CASCADE,
    capability_hash TEXT UNIQUE NOT NULL,
    expires_at INTEGER NOT NULL,
    max_session_bytes INTEGER NOT NULL,
    claimed_at INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runner_transfer_grants_runner
ON runner_transfer_grants(runner_id, expires_at);

CREATE TABLE IF NOT EXISTS managed_runtimes (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    runner_id TEXT NOT NULL UNIQUE REFERENCES user_runners(id) ON DELETE CASCADE,
    provider_name TEXT NOT NULL CHECK (provider_name = 'fly_sprites'),
    sprite_external_id TEXT NOT NULL UNIQUE,
    lifecycle_status TEXT NOT NULL CHECK (
        lifecycle_status IN ('provisioning', 'ready', 'failed', 'deleting')
    ),
    generation INTEGER DEFAULT 1 NOT NULL CHECK (generation > 0),
    artifact_version TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 1000)
);

CREATE TABLE IF NOT EXISTS managed_sprite_identities (
    sprite_name TEXT PRIMARY KEY,
    provider_name TEXT NOT NULL CHECK (provider_name = 'fly_sprites'),
    identity_kind TEXT NOT NULL CHECK (identity_kind IN ('runtime', 'restore_candidate')),
    user_id TEXT NOT NULL,
    job_id TEXT,
    lifecycle_status TEXT NOT NULL CHECK (
        lifecycle_status IN ('creating', 'active', 'retired', 'deleting')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (identity_kind = 'runtime' AND job_id IS NULL)
        OR (identity_kind = 'restore_candidate' AND job_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS managed_operational_failures (
    alert_class TEXT PRIMARY KEY CHECK (
        alert_class IN (
            'managed_sprite_reconciliation_failed',
            'managed_storage_preflight_failed'
        )
    ),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS managed_backup_archives (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    runtime_generation INTEGER NOT NULL CHECK (runtime_generation > 0),
    status TEXT NOT NULL CHECK (
        status IN ('creating', 'uploaded', 'ready', 'failed', 'deleting', 'deleted')
    ),
    object_key TEXT NOT NULL UNIQUE,
    object_version TEXT,
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes > 0),
    sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
    wrapped_key BLOB NOT NULL,
    key_id TEXT NOT NULL,
    owner_digest TEXT NOT NULL CHECK (length(owner_digest) = 64),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 1000)
);

CREATE INDEX IF NOT EXISTS idx_managed_backup_archives_user_created
ON managed_backup_archives(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS managed_backup_operations (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL UNIQUE,
    archive_id TEXT NOT NULL REFERENCES managed_backup_archives(id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK (operation IN ('create', 'restore', 'delete')),
    status TEXT NOT NULL CHECK (status IN ('running', 'failed')),
    runtime_generation INTEGER NOT NULL CHECK (runtime_generation > 0),
    phase TEXT NOT NULL DEFAULT 'claimed',
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    source_runner_id TEXT,
    source_sprite_id TEXT,
    source_lost INTEGER NOT NULL DEFAULT 0 CHECK (source_lost IN (0, 1)),
    candidate_runner_id TEXT,
    candidate_sprite_id TEXT,
    activation_generation INTEGER,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 1000),
    failure_class TEXT CHECK (
        failure_class IS NULL OR failure_class IN ('restore_failed', 'deletion_failed')
    )
);

CREATE TABLE IF NOT EXISTS managed_runtime_activation_guards (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL UNIQUE,
    lease_token TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS validate_managed_runtime_runner_insert
BEFORE INSERT ON managed_runtimes
WHEN NOT EXISTS (
    SELECT 1 FROM user_runners
    WHERE id = NEW.runner_id AND user_id = NEW.user_id AND kind = 'managed'
)
BEGIN SELECT RAISE(ABORT, 'managed runtime must reference matching managed runner'); END;

CREATE TRIGGER IF NOT EXISTS validate_managed_runtime_runner_update
BEFORE UPDATE OF user_id, runner_id ON managed_runtimes
WHEN NOT EXISTS (
    SELECT 1 FROM user_runners
    WHERE id = NEW.runner_id AND user_id = NEW.user_id AND kind = 'managed'
)
AND NOT EXISTS (
    SELECT 1 FROM managed_runtime_activation_guards WHERE user_id = NEW.user_id
)
BEGIN SELECT RAISE(ABORT, 'managed runtime must reference matching managed runner'); END;

CREATE TRIGGER IF NOT EXISTS protect_linked_managed_runner_update
BEFORE UPDATE OF user_id, kind ON user_runners
WHEN EXISTS (SELECT 1 FROM managed_runtimes WHERE runner_id = OLD.id)
    AND (NEW.user_id != OLD.user_id OR NEW.kind != OLD.kind)
    AND NOT EXISTS (
        SELECT 1 FROM managed_runtime_activation_guards WHERE user_id = OLD.user_id
    )
BEGIN SELECT RAISE(ABORT, 'cannot change linked managed runtime runner'); END;

CREATE TRIGGER IF NOT EXISTS update_users_updated_at AFTER UPDATE ON users
BEGIN UPDATE users SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS update_pi_configs_updated_at AFTER UPDATE ON pi_configs
BEGIN UPDATE pi_configs SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS update_user_settings_updated_at AFTER UPDATE ON user_settings
BEGIN UPDATE user_settings SET updated_at = CURRENT_TIMESTAMP WHERE user_id = NEW.user_id; END;

CREATE TRIGGER IF NOT EXISTS update_user_runners_updated_at AFTER UPDATE ON user_runners
BEGIN UPDATE user_runners SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS update_managed_runtimes_updated_at AFTER UPDATE ON managed_runtimes
BEGIN UPDATE managed_runtimes SET updated_at = CURRENT_TIMESTAMP WHERE user_id = NEW.user_id; END;

CREATE TRIGGER IF NOT EXISTS update_provider_connections_updated_at AFTER UPDATE ON provider_connections
BEGIN UPDATE provider_connections SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id; END;
"""


@contextmanager
def get_control_db() -> Iterator[sqlite3.Connection]:
    """Get a connection to the control plane database."""
    settings = get_settings()
    conn = _open_application_connection(settings.control_db_path, context="control")
    try:
        yield conn
    finally:
        conn.close()


def _migrate_control(conn: sqlite3.Connection) -> None:
    """Apply control DB schema migrations for existing databases."""
    columns = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "credit_used_cents" not in columns:
        logger.info("Control migration: adding credit tracking columns to users")
        conn.execute("ALTER TABLE users ADD COLUMN credit_used_cents INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN credit_limit_cents INTEGER DEFAULT 500")
        conn.commit()

    pi_config_columns = [row[1] for row in conn.execute("PRAGMA table_info(pi_configs)").fetchall()]
    if pi_config_columns and "available_categories" not in pi_config_columns:
        logger.info("Control migration: adding available_categories column to pi_configs")
        conn.execute(
            "ALTER TABLE pi_configs ADD COLUMN available_categories TEXT DEFAULT '[]' NOT NULL"
        )
        conn.commit()

    provider_connection_columns = [
        row[1] for row in conn.execute("PRAGMA table_info(provider_connections)").fetchall()
    ]
    if not provider_connection_columns:
        logger.info("Control migration: creating provider_connections table")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS provider_connections (
                id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                auth_strategy TEXT NOT NULL,
                encrypted_secret BLOB NOT NULL,
                label TEXT DEFAULT '',
                config_json TEXT DEFAULT '{}' NOT NULL,
                status TEXT DEFAULT 'connected' NOT NULL,
                last_used_at TIMESTAMP,
                expires_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_provider_connections_user
            ON provider_connections(user_id);
            CREATE TRIGGER IF NOT EXISTS update_provider_connections_updated_at
            AFTER UPDATE ON provider_connections
            BEGIN
                UPDATE provider_connections SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
            END;
            """)
        conn.commit()

    migrated_row = conn.execute(
        "SELECT COUNT(*) FROM provider_connections WHERE auth_strategy = 'api_key'"
    ).fetchone()
    api_key_count = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()
    assert migrated_row is not None, "provider connection count must be queryable"
    assert api_key_count is not None, "api key count must be queryable"
    if api_key_count[0] > 0 and migrated_row[0] < api_key_count[0]:
        logger.info("Control migration: backfilling api_keys into provider_connections")
        conn.execute("""
            INSERT INTO provider_connections
            (id, created_at, updated_at, user_id, provider, auth_strategy,
             encrypted_secret, label, config_json, status, last_used_at, expires_at)
            SELECT id, created_at, created_at, user_id, provider, 'api_key',
                   encrypted_key, label, '{}', 'connected', last_used_at, NULL
            FROM api_keys
            WHERE id NOT IN (SELECT id FROM provider_connections)
            """)
        conn.commit()

    desktop_device_columns = {row[1] for row in conn.execute("PRAGMA table_info(desktop_devices)")}
    desktop_device_migrations = {
        "revoked_at": "ALTER TABLE desktop_devices ADD COLUMN revoked_at INTEGER",
        "last_seen_at": "ALTER TABLE desktop_devices ADD COLUMN last_seen_at INTEGER",
    }
    for column_name, statement in desktop_device_migrations.items():
        if column_name not in desktop_device_columns:
            conn.execute(statement)
    conn.commit()

    desktop_request_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(desktop_authorization_requests)")
    }
    desktop_request_migrations = {
        "user_id": "ALTER TABLE desktop_authorization_requests ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE",
        "authorization_code_hash": "ALTER TABLE desktop_authorization_requests ADD COLUMN authorization_code_hash TEXT",
        "approved_at": "ALTER TABLE desktop_authorization_requests ADD COLUMN approved_at INTEGER",
        "consumed_at": "ALTER TABLE desktop_authorization_requests ADD COLUMN consumed_at INTEGER",
    }
    for column_name, statement in desktop_request_migrations.items():
        if column_name not in desktop_request_columns:
            conn.execute(statement)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_desktop_authorization_code_hash
        ON desktop_authorization_requests(authorization_code_hash)
        """)
    conn.commit()

    runner_columns = [row[1] for row in conn.execute("PRAGMA table_info(user_runners)")]
    if not runner_columns:
        logger.info("Control migration: creating user_runners table")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_runners (
                id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT DEFAULT 'byoc' NOT NULL CHECK (kind IN ('byoc', 'managed')),
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
                noise_public_key_confirmed_at TEXT,
                restore_job_id TEXT,
                UNIQUE(user_id, kind)
            );
            CREATE INDEX IF NOT EXISTS idx_user_runners_user ON user_runners(user_id);
            CREATE INDEX IF NOT EXISTS idx_user_runners_registration_token
            ON user_runners(registration_token_hash);
            CREATE INDEX IF NOT EXISTS idx_user_runners_runner_token
            ON user_runners(runner_token_hash);
            CREATE TRIGGER IF NOT EXISTS update_user_runners_updated_at
            AFTER UPDATE ON user_runners
            BEGIN
                UPDATE user_runners SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
            END;
            """)
        conn.commit()

    runner_columns = [row[1] for row in conn.execute("PRAGMA table_info(user_runners)")]
    if runner_columns and "noise_public_key" not in runner_columns:
        logger.info("Control migration: adding Noise identity to user_runners")
        conn.execute("ALTER TABLE user_runners ADD COLUMN noise_public_key TEXT")
        conn.commit()
        runner_columns.append("noise_public_key")
    if runner_columns and "noise_public_key_confirmed_at" not in runner_columns:
        logger.info("Control migration: adding Noise key confirmation to user_runners")
        conn.execute("ALTER TABLE user_runners ADD COLUMN noise_public_key_confirmed_at TEXT")
        conn.commit()
    if runner_columns and "restore_job_id" not in runner_columns:
        logger.info("Control migration: adding restore job binding to user_runners")
        conn.execute("ALTER TABLE user_runners ADD COLUMN restore_job_id TEXT")
        conn.commit()

    _migrate_runner_kinds(conn)
    _migrate_managed_restore_runner_kind(conn)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS runner_transfer_grants (
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runner_transfer_grants_runner "
        "ON runner_transfer_grants(runner_id, expires_at)"
    )
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS managed_runtimes (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            runner_id TEXT NOT NULL UNIQUE REFERENCES user_runners(id) ON DELETE CASCADE,
            provider_name TEXT NOT NULL CHECK (provider_name = 'fly_sprites'),
            sprite_external_id TEXT NOT NULL UNIQUE,
            lifecycle_status TEXT NOT NULL CHECK (
                lifecycle_status IN ('provisioning', 'ready', 'failed', 'deleting')
            ),
            generation INTEGER DEFAULT 1 NOT NULL CHECK (generation > 0),
            artifact_version TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 1000)
        );
        CREATE TABLE IF NOT EXISTS managed_backup_archives (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            runtime_generation INTEGER NOT NULL CHECK (runtime_generation > 0),
            status TEXT NOT NULL CHECK (
                status IN ('creating', 'uploaded', 'ready', 'failed', 'deleting')
            ),
            object_key TEXT NOT NULL UNIQUE,
            object_version TEXT,
            size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes > 0),
            sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
            wrapped_key BLOB NOT NULL,
            key_id TEXT NOT NULL,
            owner_digest TEXT NOT NULL CHECK (length(owner_digest) = 64),
            created_at TEXT NOT NULL,
            completed_at TEXT,
            last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 1000)
        );
        CREATE INDEX IF NOT EXISTS idx_managed_backup_archives_user_created
        ON managed_backup_archives(user_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS managed_backup_operations (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            job_id TEXT NOT NULL UNIQUE,
            archive_id TEXT NOT NULL REFERENCES managed_backup_archives(id) ON DELETE CASCADE,
            operation TEXT NOT NULL CHECK (operation IN ('create', 'restore', 'delete')),
            status TEXT NOT NULL CHECK (status IN ('running', 'failed')),
            runtime_generation INTEGER NOT NULL CHECK (runtime_generation > 0),
            phase TEXT NOT NULL DEFAULT 'claimed',
            lease_owner TEXT,
            lease_token TEXT,
            lease_expires_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            source_runner_id TEXT,
            source_sprite_id TEXT,
            source_lost INTEGER NOT NULL DEFAULT 0 CHECK (source_lost IN (0, 1)),
            candidate_runner_id TEXT,
            candidate_sprite_id TEXT,
            activation_generation INTEGER,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 1000),
            failure_class TEXT CHECK (
                failure_class IS NULL OR failure_class IN ('restore_failed', 'deletion_failed')
            )
        );
        CREATE TRIGGER IF NOT EXISTS validate_managed_runtime_runner_insert
        BEFORE INSERT ON managed_runtimes
        WHEN NOT EXISTS (
            SELECT 1 FROM user_runners
            WHERE id = NEW.runner_id AND user_id = NEW.user_id AND kind = 'managed'
        )
        BEGIN
            SELECT RAISE(ABORT, 'managed runtime must reference matching managed runner');
        END;
        CREATE TRIGGER IF NOT EXISTS validate_managed_runtime_runner_update
        BEFORE UPDATE OF user_id, runner_id ON managed_runtimes
        WHEN NOT EXISTS (
            SELECT 1 FROM user_runners
            WHERE id = NEW.runner_id AND user_id = NEW.user_id AND kind = 'managed'
        )
        BEGIN
            SELECT RAISE(ABORT, 'managed runtime must reference matching managed runner');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_linked_managed_runner_update
        BEFORE UPDATE OF user_id, kind ON user_runners
        WHEN EXISTS (SELECT 1 FROM managed_runtimes WHERE runner_id = OLD.id)
            AND (NEW.user_id != OLD.user_id OR NEW.kind != OLD.kind)
        BEGIN
            SELECT RAISE(ABORT, 'cannot change linked managed runtime runner');
        END;
        CREATE TRIGGER IF NOT EXISTS update_managed_runtimes_updated_at
        AFTER UPDATE ON managed_runtimes
        BEGIN
            UPDATE managed_runtimes SET updated_at = CURRENT_TIMESTAMP
            WHERE user_id = NEW.user_id;
        END;
        """)
    _migrate_managed_runtime_activation_guards(conn)
    _migrate_managed_backup_archive_statuses(conn)
    _migrate_managed_backup_operation_columns(conn)
    _migrate_managed_sprite_identity_ownership(conn)
    _backfill_managed_sprite_identities(conn)
    conn.commit()

    _migrate_encrypted_control_fields(conn)


_USER_RUNNER_DEPENDENT_TRIGGER_NAMES: tuple[str, ...] = (
    "update_user_runners_updated_at",
    "validate_managed_runtime_runner_insert",
    "validate_managed_runtime_runner_update",
    "protect_linked_managed_runner_update",
)
_USER_RUNNER_DEPENDENT_TRIGGER_DDL: tuple[str, ...] = (
    """CREATE TRIGGER IF NOT EXISTS update_user_runners_updated_at
    AFTER UPDATE ON user_runners
    BEGIN
        UPDATE user_runners SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
    END""",
    """CREATE TRIGGER IF NOT EXISTS validate_managed_runtime_runner_insert
    BEFORE INSERT ON managed_runtimes
    WHEN NOT EXISTS (
        SELECT 1 FROM user_runners
        WHERE id = NEW.runner_id AND user_id = NEW.user_id AND kind = 'managed'
    )
    BEGIN
        SELECT RAISE(ABORT, 'managed runtime must reference matching managed runner');
    END""",
    """CREATE TRIGGER IF NOT EXISTS validate_managed_runtime_runner_update
    BEFORE UPDATE OF user_id, runner_id ON managed_runtimes
    WHEN NOT EXISTS (
        SELECT 1 FROM user_runners
        WHERE id = NEW.runner_id AND user_id = NEW.user_id AND kind = 'managed'
    )
    AND NOT EXISTS (
        SELECT 1 FROM managed_runtime_activation_guards WHERE user_id = NEW.user_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'managed runtime must reference matching managed runner');
    END""",
    """CREATE TRIGGER IF NOT EXISTS protect_linked_managed_runner_update
    BEFORE UPDATE OF user_id, kind ON user_runners
    WHEN EXISTS (SELECT 1 FROM managed_runtimes WHERE runner_id = OLD.id)
        AND (NEW.user_id != OLD.user_id OR NEW.kind != OLD.kind)
        AND NOT EXISTS (
            SELECT 1 FROM managed_runtime_activation_guards WHERE user_id = OLD.user_id
        )
    BEGIN
        SELECT RAISE(ABORT, 'cannot change linked managed runtime runner');
    END""",
)


def _drop_user_runner_dependent_triggers(conn: sqlite3.Connection) -> None:
    """Drop every trigger that depends on user_runners before table replacement."""
    for trigger_name in _USER_RUNNER_DEPENDENT_TRIGGER_NAMES:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")


def _recreate_user_runner_dependent_triggers(conn: sqlite3.Connection) -> None:
    """Recreate the canonical guard-aware triggers around a user_runners table."""
    for trigger_definition in _USER_RUNNER_DEPENDENT_TRIGGER_DDL:
        conn.execute(trigger_definition)


def _migrate_managed_runtime_activation_guards(conn: sqlite3.Connection) -> None:
    """Install the durable guard and replacement-aware integrity triggers."""
    conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS managed_runtime_activation_guards (
               user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
               job_id TEXT NOT NULL UNIQUE,
               lease_token TEXT NOT NULL
           )""")
        conn.execute("DROP TRIGGER IF EXISTS validate_managed_runtime_runner_update")
        conn.execute("DROP TRIGGER IF EXISTS protect_linked_managed_runner_update")
        _recreate_user_runner_dependent_triggers(conn)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def _migrate_managed_backup_archive_statuses(conn: sqlite3.Connection) -> None:
    """Expand existing archive status checks to include deleted tombstones."""
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' " "AND name = 'managed_backup_archives'"
    ).fetchone()
    if schema_row is None or "'deleted'" in str(schema_row["sql"]):
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("ALTER TABLE managed_backup_archives RENAME TO managed_backup_archives_old")
        conn.execute("""CREATE TABLE managed_backup_archives (
                   id TEXT PRIMARY KEY,
                   user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                   runtime_generation INTEGER NOT NULL CHECK (runtime_generation > 0),
                   status TEXT NOT NULL CHECK (
                       status IN (
                           'creating', 'uploaded', 'ready', 'failed', 'deleting', 'deleted'
                       )
                   ),
                   object_key TEXT NOT NULL UNIQUE,
                   object_version TEXT,
                   size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes > 0),
                   sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
                   wrapped_key BLOB NOT NULL,
                   key_id TEXT NOT NULL,
                   owner_digest TEXT NOT NULL CHECK (length(owner_digest) = 64),
                   created_at TEXT NOT NULL,
                   completed_at TEXT,
                   last_error TEXT CHECK (last_error IS NULL OR length(last_error) <= 1000)
               )""")
        conn.execute("""INSERT INTO managed_backup_archives
               SELECT * FROM managed_backup_archives_old""")
        conn.execute("DROP TABLE managed_backup_archives_old")
        conn.execute(
            "CREATE INDEX idx_managed_backup_archives_user_created "
            "ON managed_backup_archives(user_id, created_at DESC)"
        )
        violation = conn.execute("PRAGMA foreign_key_check").fetchone()
        if violation is not None:
            raise sqlite3.IntegrityError("Managed backup archive migration left an invalid key")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_managed_backup_operation_columns(conn: sqlite3.Connection) -> None:
    """Add resumable maintenance fields to existing managed backup tables."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(managed_backup_operations)")}
    if not columns:
        return
    migrations = {
        "phase": "ALTER TABLE managed_backup_operations ADD COLUMN phase TEXT NOT NULL DEFAULT 'claimed'",
        "lease_owner": "ALTER TABLE managed_backup_operations ADD COLUMN lease_owner TEXT",
        "lease_token": "ALTER TABLE managed_backup_operations ADD COLUMN lease_token TEXT",
        "lease_expires_at": (
            "ALTER TABLE managed_backup_operations ADD COLUMN lease_expires_at TEXT"
        ),
        "attempt_count": (
            "ALTER TABLE managed_backup_operations "
            "ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
        ),
        "next_attempt_at": (
            "ALTER TABLE managed_backup_operations ADD COLUMN next_attempt_at TEXT"
        ),
        "source_runner_id": (
            "ALTER TABLE managed_backup_operations ADD COLUMN source_runner_id TEXT"
        ),
        "source_sprite_id": (
            "ALTER TABLE managed_backup_operations ADD COLUMN source_sprite_id TEXT"
        ),
        "source_lost": (
            "ALTER TABLE managed_backup_operations "
            "ADD COLUMN source_lost INTEGER NOT NULL DEFAULT 0 CHECK (source_lost IN (0, 1))"
        ),
        "candidate_runner_id": (
            "ALTER TABLE managed_backup_operations ADD COLUMN candidate_runner_id TEXT"
        ),
        "candidate_sprite_id": (
            "ALTER TABLE managed_backup_operations ADD COLUMN candidate_sprite_id TEXT"
        ),
        "activation_generation": (
            "ALTER TABLE managed_backup_operations ADD COLUMN activation_generation INTEGER"
        ),
        "failure_class": "ALTER TABLE managed_backup_operations ADD COLUMN failure_class TEXT",
    }
    for column_name, statement in migrations.items():
        if column_name not in columns:
            conn.execute(statement)
    conn.execute("""
        UPDATE managed_backup_operations
        SET source_runner_id = (
                SELECT runtime.runner_id
                FROM managed_runtimes AS runtime
                WHERE runtime.user_id = managed_backup_operations.user_id
                  AND runtime.generation = managed_backup_operations.runtime_generation
            ),
            source_sprite_id = (
                SELECT runtime.sprite_external_id
                FROM managed_runtimes AS runtime
                WHERE runtime.user_id = managed_backup_operations.user_id
                  AND runtime.generation = managed_backup_operations.runtime_generation
            )
        WHERE status = 'running'
          AND source_runner_id IS NULL
          AND source_sprite_id IS NULL
          AND EXISTS (
              SELECT 1
              FROM managed_runtimes AS runtime
              WHERE runtime.user_id = managed_backup_operations.user_id
                AND runtime.generation = managed_backup_operations.runtime_generation
          )
        """)
    conn.commit()


def _migrate_managed_sprite_identity_ownership(conn: sqlite3.Connection) -> None:
    """Keep provider ownership after the related user row is removed."""
    foreign_keys = conn.execute("PRAGMA foreign_key_list(managed_sprite_identities)").fetchall()
    if not foreign_keys:
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute(
            "ALTER TABLE managed_sprite_identities " "RENAME TO managed_sprite_identities_old"
        )
        conn.execute("""CREATE TABLE managed_sprite_identities (
                   sprite_name TEXT PRIMARY KEY,
                   provider_name TEXT NOT NULL CHECK (provider_name = 'fly_sprites'),
                   identity_kind TEXT NOT NULL CHECK (
                       identity_kind IN ('runtime', 'restore_candidate')
                   ),
                   user_id TEXT NOT NULL,
                   job_id TEXT,
                   lifecycle_status TEXT NOT NULL CHECK (
                       lifecycle_status IN ('creating', 'active', 'retired', 'deleting')
                   ),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   CHECK (
                       (identity_kind = 'runtime' AND job_id IS NULL)
                       OR (identity_kind = 'restore_candidate' AND job_id IS NOT NULL)
                   )
               )""")
        conn.execute("""INSERT INTO managed_sprite_identities
               SELECT * FROM managed_sprite_identities_old""")
        conn.execute("DROP TABLE managed_sprite_identities_old")
        violation = conn.execute("PRAGMA foreign_key_check").fetchone()
        if violation is not None:
            raise sqlite3.IntegrityError("Managed Sprite identity migration left an invalid key")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def _backfill_managed_sprite_identities(conn: sqlite3.Connection) -> None:
    """Register only provider identities already bound to durable control state."""
    conn.execute("""INSERT OR IGNORE INTO managed_sprite_identities
           (sprite_name, provider_name, identity_kind, user_id, job_id,
            lifecycle_status, created_at, updated_at)
           SELECT sprite_external_id, provider_name, 'runtime', user_id, NULL,
                  CASE WHEN lifecycle_status = 'ready' THEN 'active' ELSE 'creating' END,
                  created_at, updated_at
           FROM managed_runtimes WHERE provider_name = 'fly_sprites'""")
    conn.execute("""INSERT OR IGNORE INTO managed_sprite_identities
           (sprite_name, provider_name, identity_kind, user_id, job_id,
            lifecycle_status, created_at, updated_at)
           SELECT candidate_sprite_id, 'fly_sprites', 'restore_candidate', user_id, job_id,
                  'creating', started_at, updated_at
           FROM managed_backup_operations
           WHERE operation = 'restore' AND status = 'running'
             AND candidate_sprite_id IS NOT NULL""")


def _migrate_runner_kinds(conn: sqlite3.Connection) -> None:
    """Rebuild the runner table so one BYOC and one managed runner can coexist."""
    runner_columns = {row[1] for row in conn.execute("PRAGMA table_info(user_runners)")}
    if not runner_columns or "kind" in runner_columns:
        return

    logger.info("Control migration: adding runner kinds to user_runners")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        _drop_user_runner_dependent_triggers(conn)
        conn.execute("""
            CREATE TABLE user_runners_with_kinds (
                id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT DEFAULT 'byoc' NOT NULL CHECK (kind IN ('byoc', 'managed')),
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
                noise_public_key_confirmed_at TEXT,
                restore_job_id TEXT,
                UNIQUE(user_id, kind)
            )
            """)
        conn.execute("""
            INSERT INTO user_runners_with_kinds (
                id, created_at, updated_at, user_id, kind, name, cloud_provider,
                region, status, registration_token_hash,
                registration_token_expires_at, runner_token_hash, registered_at,
                last_heartbeat_at, runner_version, capabilities_json, data_dir,
                revoked_at, noise_public_key, noise_public_key_confirmed_at,
                restore_job_id
            )
            SELECT
                id, created_at, updated_at, user_id, 'byoc', name, cloud_provider,
                region, status, registration_token_hash,
                registration_token_expires_at, runner_token_hash, registered_at,
                last_heartbeat_at, runner_version, capabilities_json, data_dir,
                revoked_at, noise_public_key, noise_public_key_confirmed_at,
                restore_job_id
            FROM user_runners
            """)
        conn.execute("DROP TABLE user_runners")
        conn.execute("ALTER TABLE user_runners_with_kinds RENAME TO user_runners")
        conn.execute("CREATE INDEX idx_user_runners_user ON user_runners(user_id)")
        conn.execute(
            "CREATE INDEX idx_user_runners_registration_token "
            "ON user_runners(registration_token_hash)"
        )
        conn.execute(
            "CREATE INDEX idx_user_runners_runner_token ON user_runners(runner_token_hash)"
        )
        _recreate_user_runner_dependent_triggers(conn)
        foreign_key_issue = conn.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_issue is not None:
            raise sqlite3.IntegrityError("Runner kind migration left an invalid foreign key")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_managed_restore_runner_kind(conn: sqlite3.Connection) -> None:
    """Expand existing runner kinds while preserving rows and foreign keys."""
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'user_runners'"
    ).fetchone()
    if schema_row is None or "managed_retired" in str(schema_row["sql"]):
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        _drop_user_runner_dependent_triggers(conn)
        conn.execute("""CREATE TABLE user_runners_expanded (
                   id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                   updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                   user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                   kind TEXT DEFAULT 'byoc' NOT NULL CHECK (
                       kind IN ('byoc', 'managed', 'managed_restore', 'managed_retired')
                   ),
                   name TEXT NOT NULL, cloud_provider TEXT NOT NULL, region TEXT NOT NULL,
                   status TEXT DEFAULT 'pending' NOT NULL, registration_token_hash TEXT,
                   registration_token_expires_at TEXT, runner_token_hash TEXT,
                   registered_at TEXT, last_heartbeat_at TEXT, runner_version TEXT,
                   capabilities_json TEXT DEFAULT '{}' NOT NULL, data_dir TEXT,
                   revoked_at TEXT, noise_public_key TEXT,
                   noise_public_key_confirmed_at TEXT, restore_job_id TEXT,
                   UNIQUE(user_id, kind)
               )""")
        conn.execute("""INSERT INTO user_runners_expanded
               SELECT * FROM user_runners""")
        conn.execute("DROP TABLE user_runners")
        conn.execute("ALTER TABLE user_runners_expanded RENAME TO user_runners")
        conn.execute("CREATE INDEX idx_user_runners_user ON user_runners(user_id)")
        conn.execute(
            "CREATE INDEX idx_user_runners_registration_token "
            "ON user_runners(registration_token_hash)"
        )
        conn.execute(
            "CREATE INDEX idx_user_runners_runner_token ON user_runners(runner_token_hash)"
        )
        _recreate_user_runner_dependent_triggers(conn)
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError(
                "Managed restore runner migration left an invalid foreign key"
            )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_encrypted_control_fields(conn: sqlite3.Connection) -> None:
    """Encrypt sensitive control-plane text fields when field encryption is active."""
    settings = get_settings()
    if not control_field_encryption_enabled(settings):
        return

    from yinshi.services.control_encryption import encrypt_control_text
    from yinshi.services.crypto import is_encrypted_text

    user_settings_rows = conn.execute(
        "SELECT user_id, pi_settings_json FROM user_settings"
    ).fetchall()
    for row in user_settings_rows:
        stored_value = row["pi_settings_json"]
        if isinstance(stored_value, str) and not is_encrypted_text(stored_value):
            conn.execute(
                "UPDATE user_settings SET pi_settings_json = ? WHERE user_id = ?",
                (
                    encrypt_control_text(
                        "user_settings.pi_settings_json",
                        row["user_id"],
                        stored_value,
                    ),
                    row["user_id"],
                ),
            )

    pi_config_rows = conn.execute(
        "SELECT id, user_id, source_label, repo_url, error_message FROM pi_configs"
    ).fetchall()
    for row in pi_config_rows:
        updates: list[str] = []
        values: list[object] = []
        for field_name in ("source_label", "repo_url", "error_message"):
            stored_value = row[field_name]
            if stored_value is None:
                continue
            if isinstance(stored_value, str) and not is_encrypted_text(stored_value):
                updates.append(f"{field_name} = ?")
                values.append(
                    encrypt_control_text(
                        f"pi_configs.{field_name}",
                        row["user_id"],
                        stored_value,
                    )
                )
        if updates:
            values.append(row["id"])
            conn.execute(
                f"UPDATE pi_configs SET {', '.join(updates)} WHERE id = ?",  # noqa: S608
                values,
            )
    conn.commit()


def init_control_db() -> None:
    """Initialize the control plane database schema."""
    settings = get_settings()
    Path(settings.control_db_path).parent.mkdir(parents=True, exist_ok=True)
    logger.info("Initializing control database")
    try:
        with get_control_db() as conn:
            conn.executescript(CONTROL_SCHEMA_SQL)
            _migrate_control(conn)
    except sqlite3.Error:
        logger.error("Failed to initialize control database")
        raise
    logger.info("Control database initialized")
