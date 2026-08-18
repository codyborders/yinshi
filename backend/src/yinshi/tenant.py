"""Multi-tenant context and per-user database management."""

from __future__ import annotations

import errno
import fcntl
import importlib
import logging
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final, cast

from yinshi.config import (
    get_settings,
    tenant_db_encryption_enabled,
    tenant_db_encryption_required,
    user_data_encryption_required,
)
from yinshi.db import _open_connection
from yinshi.model_catalog import DEFAULT_SESSION_MODEL
from yinshi.services.crypto import derive_subkey

logger = logging.getLogger(__name__)

_SQLCIPHER_MODULE_NAMES: Final[tuple[str, ...]] = (
    "sqlcipher3.dbapi2",
    "pysqlcipher3.dbapi2",
)
_SQLCIPHER_OPEN_ATTEMPTS: Final[int] = 3
_SQLCIPHER_OPEN_RETRY_DELAY_SECONDS: Final[float] = 0.05
_SQLCIPHER_TRANSIENT_OPEN_ERRORS: Final[frozenset[str]] = frozenset({"disk I/O error"})
_STORAGE_ENCRYPTION_MARKER: Final[str] = ".yinshi-encrypted-storage"
_SCHEMA_OBJECT_TYPES: Final[tuple[str, ...]] = ("index", "table", "trigger", "view")
_MIGRATION_LOCK_SUFFIX: Final[str] = ".migration.lock"
_PLAINTEXT_ROLLBACK_SUFFIX: Final[str] = ".plaintext.rollback"
_MIGRATION_THREAD_LOCKS: Final[dict[str, threading.Lock]] = {}
_MIGRATION_THREAD_LOCKS_GUARD: Final[threading.Lock] = threading.Lock()


@dataclass
class TenantContext:
    """Per-request tenant context resolved from authentication."""

    user_id: str
    email: str
    data_dir: str
    db_path: str


def user_data_dir(base_dir: str, user_id: str) -> str:
    """Compute the data directory for a user, using a 2-char prefix."""
    prefix = user_id[:2]
    return os.path.join(base_dir, prefix, user_id)


def validate_user_path(tenant: TenantContext, path: str) -> None:
    """Validate that a path is within the tenant's data directory.

    Raises ValueError if the path is outside the tenant's data_dir.
    """
    resolved = os.path.realpath(path)
    data_dir = os.path.realpath(tenant.data_dir)
    if not resolved.startswith(data_dir + os.sep) and resolved != data_dir:
        raise ValueError(f"Path {path} is outside tenant data directory")


# User DB schema -- identical to main schema but WITHOUT owner_email
USER_SCHEMA_SQL = f"""
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


def _migrate_user_db(conn: sqlite3.Connection) -> None:
    """Apply forward-only schema fixes for existing per-user databases."""
    repo_columns = [row[1] for row in conn.execute("PRAGMA table_info(repos)").fetchall()]
    if "installation_id" not in repo_columns:
        conn.execute("ALTER TABLE repos ADD COLUMN installation_id INTEGER")

    message_columns = [row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "turn_status" not in message_columns:
        conn.execute("ALTER TABLE messages ADD COLUMN turn_status TEXT")

    # agents_md column for repo-level AGENTS.md override
    repo_columns = [row[1] for row in conn.execute("PRAGMA table_info(repos)").fetchall()]
    if "agents_md" not in repo_columns:
        conn.execute("ALTER TABLE repos ADD COLUMN agents_md TEXT")

    session_columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    if "pi_context_version" not in session_columns:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN pi_context_version INTEGER DEFAULT 0 NOT NULL"
        )

    conn.commit()


def _ensure_user_db_schema(conn: sqlite3.Connection) -> None:
    """Create missing tables and apply migrations for a per-user database."""
    conn.executescript(USER_SCHEMA_SQL)
    _migrate_user_db(conn)


def _load_sqlcipher_module() -> ModuleType:
    """Load an installed SQLCipher DB-API module or raise a clear error."""
    import_errors: list[str] = []
    for module_name in _SQLCIPHER_MODULE_NAMES:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            import_errors.append(f"{module_name}: {exc}")
            continue
        if not hasattr(module, "connect"):
            import_errors.append(f"{module_name}: missing connect")
            continue
        if not hasattr(module, "Row"):
            import_errors.append(f"{module_name}: missing Row")
            continue
        return module
    joined_errors = "; ".join(import_errors) or "no SQLCipher module candidates configured"
    raise RuntimeError(
        "TENANT_DB_ENCRYPTION requires sqlcipher3 or pysqlcipher3. "
        f"Import failures: {joined_errors}"
    )


def _tenant_database_key(tenant: TenantContext) -> bytes:
    """Derive the SQLCipher key for one tenant database from the user's DEK."""
    if tenant is None:
        raise ValueError("tenant is required when tenant DB encryption is enabled")
    from yinshi.services.keys import get_user_dek

    user_dek = get_user_dek(tenant.user_id)
    return derive_subkey(user_dek, purpose="tenant-sqlcipher", context=tenant.user_id)


def _open_sqlcipher_connection(db_path: str, sqlcipher_key: bytes) -> sqlite3.Connection:
    """Open a SQLCipher-backed SQLite connection and validate the key immediately."""
    if not isinstance(db_path, str):
        raise TypeError("db_path must be a string")
    if not db_path.strip():
        raise ValueError("db_path must not be empty")
    if not isinstance(sqlcipher_key, bytes):
        raise TypeError("sqlcipher_key must be bytes")
    if len(sqlcipher_key) != 32:
        raise ValueError("sqlcipher_key must be exactly 32 bytes")

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    sqlcipher_module = _load_sqlcipher_module()
    sqlcipher_database_error = getattr(
        sqlcipher_module,
        "DatabaseError",
        sqlite3.DatabaseError,
    )
    if not isinstance(sqlcipher_database_error, type) or not issubclass(
        sqlcipher_database_error,
        Exception,
    ):
        sqlcipher_database_error = sqlite3.DatabaseError
    sqlcipher_operational_error = getattr(
        sqlcipher_module,
        "OperationalError",
        sqlite3.OperationalError,
    )
    if not isinstance(sqlcipher_operational_error, type) or not issubclass(
        sqlcipher_operational_error,
        Exception,
    ):
        sqlcipher_operational_error = sqlite3.OperationalError

    for attempt in range(_SQLCIPHER_OPEN_ATTEMPTS):
        conn: sqlite3.Connection | None = None
        try:
            conn = cast(sqlite3.Connection, sqlcipher_module.connect(db_path))
            conn.row_factory = getattr(sqlcipher_module, "Row")
            # The key is derived binary material, converted to hex locally, and never
            # includes user-controlled SQL. SQLCipher requires PRAGMA key syntax.
            conn.execute(f"PRAGMA key = \"x'{sqlcipher_key.hex()}'\"")  # noqa: S608
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return conn
        except (sqlite3.DatabaseError, sqlcipher_database_error) as exc:
            if conn is not None:
                conn.close()
            is_transient = isinstance(exc, sqlcipher_operational_error) and (
                str(exc) in _SQLCIPHER_TRANSIENT_OPEN_ERRORS
            )
            if not is_transient or attempt + 1 >= _SQLCIPHER_OPEN_ATTEMPTS:
                raise RuntimeError(
                    "Tenant database could not be opened with the configured key"
                ) from exc
            time.sleep(_SQLCIPHER_OPEN_RETRY_DELAY_SECONDS * (attempt + 1))

    raise AssertionError("SQLCipher connection attempts must return or raise")


def _plaintext_database_readable(db_path: str) -> bool:
    """Return whether a database can be opened by plaintext stdlib SQLite."""
    if not os.path.exists(db_path):
        return False
    try:
        conn = _open_connection(db_path)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return True
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def _copy_plaintext_user_database(source_path: str, target_path: str, sqlcipher_key: bytes) -> None:
    """Export a complete plaintext tenant DB into a SQLCipher database."""
    sqlcipher_module = _load_sqlcipher_module()
    source = cast(sqlite3.Connection, sqlcipher_module.connect(source_path))
    attached = False
    try:
        checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if (
            checkpoint is None
            or len(checkpoint) < 3
            or int(checkpoint[0]) != 0
            or int(checkpoint[1]) != int(checkpoint[2])
        ):
            raise RuntimeError("Tenant database WAL checkpoint did not complete")
        integrity_row = source.execute("PRAGMA integrity_check").fetchone()
        if integrity_row is None or str(integrity_row[0]).lower() != "ok":
            raise RuntimeError("Plaintext tenant database failed integrity validation")
        source.execute(
            "ATTACH DATABASE ? AS encrypted KEY ?",
            (target_path, f"x'{sqlcipher_key.hex()}'"),
        )
        attached = True
        source.execute("SELECT sqlcipher_export('encrypted')").fetchone()
    finally:
        try:
            if attached:
                source.execute("DETACH DATABASE encrypted")
        finally:
            source.close()


def _database_schema_objects(
    connection: sqlite3.Connection,
) -> list[tuple[str, str, str, str | None]]:
    """Return all application-defined SQLite schema objects in stable order."""
    placeholders = ", ".join("?" for _ in _SCHEMA_OBJECT_TYPES)
    rows = connection.execute(
        f"""SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE type IN ({placeholders}) AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name""",  # noqa: S608
        _SCHEMA_OBJECT_TYPES,
    ).fetchall()
    return [
        (str(row[0]), str(row[1]), str(row[2]), None if row[3] is None else str(row[3]))
        for row in rows
    ]


def _quote_sqlite_identifier(identifier: str) -> str:
    """Quote one SQLite identifier obtained from trusted schema metadata."""
    return '"' + identifier.replace('"', '""') + '"'


def _database_table_rows(
    connection: sqlite3.Connection,
    table_name: str,
) -> Iterator[tuple[object, ...]]:
    """Yield rows from one table in primary-key or rowid order."""
    quoted_table = _quote_sqlite_identifier(table_name)
    columns = connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()  # noqa: S608
    primary_key_columns = sorted(
        ((int(row[5]), str(row[1])) for row in columns if int(row[5]) > 0),
        key=lambda item: item[0],
    )
    if primary_key_columns:
        ordering = ", ".join(
            _quote_sqlite_identifier(column_name) for _, column_name in primary_key_columns
        )
    else:
        ordering = "rowid"
    rows = connection.execute(f"SELECT * FROM {quoted_table} ORDER BY {ordering}")  # noqa: S608
    for row in rows:
        yield tuple(row)


def _database_table_row_count(connection: sqlite3.Connection, table_name: str) -> int:
    """Return row count for one table."""
    quoted_table = _quote_sqlite_identifier(table_name)
    row = connection.execute(f"SELECT count(*) FROM {quoted_table}").fetchone()  # noqa: S608
    if row is None:
        raise RuntimeError("Tenant database row count query failed")
    return int(row[0])


def _validate_export_matches_source(
    source_path: str,
    target_path: str,
    sqlcipher_key: bytes,
) -> None:
    """Require exported schema, row counts, values, and ordering to match source."""
    source = _open_connection(source_path)
    target = _open_sqlcipher_connection(target_path, sqlcipher_key)
    try:
        source_schema = _database_schema_objects(source)
        if _database_schema_objects(target) != source_schema:
            raise RuntimeError("Encrypted tenant database export does not match source schema")
        table_names = [name for object_type, name, _, _ in source_schema if object_type == "table"]
        for table_name in table_names:
            source_count = _database_table_row_count(source, table_name)
            if _database_table_row_count(target, table_name) != source_count:
                raise RuntimeError("Encrypted tenant database export does not match source data")
            source_rows = _database_table_rows(source, table_name)
            target_rows = _database_table_rows(target, table_name)
            for source_row, target_row in zip(source_rows, target_rows, strict=True):
                if target_row != source_row:
                    raise RuntimeError(
                        "Encrypted tenant database export does not match source data"
                    )
    finally:
        source.close()
        target.close()


def _validate_encrypted_user_database(db_path: str, sqlcipher_key: bytes) -> None:
    """Require an encrypted database to open and pass SQLCipher integrity checks."""
    if not db_path:
        raise ValueError("db_path must not be empty")
    if len(sqlcipher_key) != 32:
        raise ValueError("sqlcipher_key must contain 32 bytes")
    connection = _open_sqlcipher_connection(db_path, sqlcipher_key)
    try:
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity_row is None or str(integrity_row[0]).lower() != "ok":
            raise RuntimeError("Encrypted tenant database failed integrity validation")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        connection.close()


def _migration_thread_lock(db_path: str) -> threading.Lock:
    """Return the process-local lock for one tenant database path."""
    canonical_path = os.path.realpath(os.path.abspath(db_path))
    with _MIGRATION_THREAD_LOCKS_GUARD:
        return _MIGRATION_THREAD_LOCKS.setdefault(canonical_path, threading.Lock())


@contextmanager
def _tenant_migration_lock(db_path: str) -> Iterator[None]:
    """Hold an owner-only advisory lock for tenant database migration work."""
    if not db_path:
        raise ValueError("db_path must not be empty")
    lock_path = f"{db_path}{_MIGRATION_LOCK_SUFFIX}"
    open_flags = os.O_CREAT | os.O_RDWR
    open_flags |= getattr(os, "O_CLOEXEC", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    thread_lock = _migration_thread_lock(db_path)

    with thread_lock:
        try:
            lock_fd = os.open(lock_path, open_flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RuntimeError(
                    "Tenant database migration lock path must not be a symlink"
                ) from exc
            raise RuntimeError("Tenant database migration lock could not be opened") from exc
        try:
            lock_stat = os.fstat(lock_fd)
            path_stat = os.lstat(lock_path)
            if stat.S_ISLNK(path_stat.st_mode):
                raise RuntimeError("Tenant database migration lock path must not be a symlink")
            if not stat.S_ISREG(lock_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
                raise RuntimeError("Tenant database migration lock must be a regular file")
            if (lock_stat.st_dev, lock_stat.st_ino) != (
                path_stat.st_dev,
                path_stat.st_ino,
            ):
                raise RuntimeError("Tenant database migration lock path changed during open")
            if lock_stat.st_uid != os.geteuid():
                raise RuntimeError("Tenant database migration lock must be owned by this user")
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            locked_path_stat = os.lstat(lock_path)
            if stat.S_ISLNK(locked_path_stat.st_mode) or (
                lock_stat.st_dev,
                lock_stat.st_ino,
            ) != (locked_path_stat.st_dev, locked_path_stat.st_ino):
                raise RuntimeError("Tenant database migration lock path changed while waiting")
            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


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


def _tenant_rollback_is_trusted(descriptor: int, rollback_path: str) -> bool:
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


def _open_trusted_tenant_rollback(rollback_path: str) -> int | None:
    """Open and validate a tenant rollback without following links."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(rollback_path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(
            "Tenant database migration rollback must be a trusted regular file"
        ) from exc
    if _tenant_rollback_is_trusted(descriptor, rollback_path):
        return descriptor
    os.close(descriptor)
    raise RuntimeError("Tenant database migration rollback must be a trusted regular file")


def _recover_plaintext_migration_rollback(db_path: str) -> None:
    """Restore a durable rollback when an interrupted replacement lost its primary."""
    rollback_path = f"{db_path}{_PLAINTEXT_ROLLBACK_SUFFIX}"
    if os.path.lexists(db_path):
        return
    descriptor = _open_trusted_tenant_rollback(rollback_path)
    if descriptor is None:
        return
    try:
        os.fsync(descriptor)
        if os.path.lexists(db_path):
            return
        if not _tenant_rollback_is_trusted(descriptor, rollback_path):
            raise RuntimeError("Tenant database migration rollback must be a trusted regular file")
        os.replace(rollback_path, db_path)
        os.chmod(db_path, 0o600)
        _fsync_file(db_path)
        _fsync_parent_directory(db_path)
    finally:
        os.close(descriptor)
    logger.warning("Recovered an interrupted tenant database migration")


def _remove_validated_migration_rollback(db_path: str) -> None:
    """Remove rollback only after the validated primary is durable."""
    rollback_path = f"{db_path}{_PLAINTEXT_ROLLBACK_SUFFIX}"
    if not os.path.exists(rollback_path):
        return
    _fsync_file(db_path)
    _fsync_parent_directory(db_path)
    os.unlink(rollback_path)
    _fsync_parent_directory(db_path)


def _migrate_plaintext_user_database(db_path: str, sqlcipher_key: bytes) -> None:
    """Replace a plaintext tenant DB without retaining the original copy."""
    if not _plaintext_database_readable(db_path):
        return
    rollback_path = f"{db_path}{_PLAINTEXT_ROLLBACK_SUFFIX}"
    temp_path = f"{db_path}.encrypted.tmp"
    temporary_paths = (temp_path, f"{temp_path}-wal", f"{temp_path}-shm")
    for stale_path in temporary_paths:
        if os.path.exists(stale_path):
            os.unlink(stale_path)
    if os.path.exists(rollback_path):
        _fsync_file(db_path)
        _fsync_parent_directory(db_path)
        os.unlink(rollback_path)
        _fsync_parent_directory(db_path)
    try:
        _copy_plaintext_user_database(db_path, temp_path, sqlcipher_key)
        _remove_sqlite_sidecars(db_path)
        _validate_encrypted_user_database(temp_path, sqlcipher_key)
        _validate_export_matches_source(db_path, temp_path, sqlcipher_key)
        os.chmod(temp_path, 0o600)
        _fsync_file(temp_path)
        _create_private_rollback_copy(db_path, rollback_path)
        _fsync_file(rollback_path)
        _fsync_parent_directory(db_path)
        os.replace(temp_path, db_path)
        _fsync_parent_directory(db_path)
        _validate_encrypted_user_database(db_path, sqlcipher_key)
        os.chmod(db_path, 0o600)
        _fsync_file(db_path)
        _fsync_parent_directory(db_path)
    except Exception:
        if os.path.exists(rollback_path):
            os.replace(rollback_path, db_path)
            os.chmod(db_path, 0o600)
            _fsync_file(db_path)
            _fsync_parent_directory(db_path)
        for temporary_path in temporary_paths:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
        raise
    else:
        os.unlink(rollback_path)
        _fsync_parent_directory(db_path)
    _remove_sqlite_sidecars(db_path)
    logger.info("Migrated plaintext tenant database to encrypted storage")


def _remove_plaintext_migration_backups(db_path: str) -> None:
    """Remove legacy plaintext backups after the encrypted primary is validated."""
    if not db_path:
        raise ValueError("db_path must not be empty")
    database_path = Path(db_path)
    pattern = f"{database_path.name}.plaintext.*.bak"
    for backup_path in database_path.parent.glob(pattern):
        backup_path.unlink()
        logger.info("Removed legacy plaintext tenant database backup")


def _encrypted_storage_marker_exists(data_dir: str) -> bool:
    """Return whether operations marked a user directory as encrypted storage."""
    current_path = Path(data_dir).resolve()
    for candidate in (current_path, *current_path.parents):
        marker_path = candidate / _STORAGE_ENCRYPTION_MARKER
        if marker_path.is_file():
            return True
    return False


def _ensure_user_data_encryption_marker(tenant: TenantContext) -> None:
    """Fail closed when configured encrypted user storage is absent."""
    settings = get_settings()
    if not user_data_encryption_required(settings):
        return
    if _encrypted_storage_marker_exists(tenant.data_dir):
        return
    raise RuntimeError(
        "USER_DATA_ENCRYPTION is required, but no .yinshi-encrypted-storage marker "
        "was found. Mount an fscrypt, LUKS, or encrypted volume first."
    )


def _open_user_connection(
    db_path: str,
    tenant: TenantContext | None,
) -> sqlite3.Connection:
    """Open a tenant database using SQLCipher when policy enables it."""
    settings = get_settings()
    if tenant is not None:
        _ensure_user_data_encryption_marker(tenant)
    encryption_enabled = tenant_db_encryption_enabled(settings)
    encryption_required = tenant_db_encryption_required(settings)
    with _tenant_migration_lock(db_path):
        _recover_plaintext_migration_rollback(db_path)
        if not encryption_enabled:
            return _open_connection(db_path)
        if tenant is None:
            raise ValueError("tenant is required when tenant DB encryption is enabled")
        try:
            _load_sqlcipher_module()
        except RuntimeError:
            if encryption_required:
                raise
            logger.warning("SQLCipher unavailable; opening tenant database without encryption")
            return _open_connection(db_path)

        sqlcipher_key = _tenant_database_key(tenant)
        if os.path.exists(db_path):
            _migrate_plaintext_user_database(db_path, sqlcipher_key)
        connection = _open_sqlcipher_connection(db_path, sqlcipher_key)
        _remove_validated_migration_rollback(db_path)
        _remove_plaintext_migration_backups(db_path)
        return connection


def init_user_db(db_path: str, tenant: TenantContext | None = None) -> None:
    """Initialize a per-user SQLite database with the user schema."""
    conn = _open_user_connection(db_path, tenant)
    try:
        _ensure_user_db_schema(conn)
        if os.path.exists(db_path):
            os.chmod(db_path, 0o600)
    finally:
        conn.close()


@contextmanager
def get_user_db(tenant: TenantContext) -> Iterator[sqlite3.Connection]:
    """Get a SQLite connection to a user's database."""
    conn = _open_user_connection(tenant.db_path, tenant)
    try:
        _ensure_user_db_schema(conn)
        yield conn
    finally:
        conn.close()
