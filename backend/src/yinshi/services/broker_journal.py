"""Durable append-only SQLite state for broker lifecycle requests."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from yinshi.services.broker_protocol import (
    BROKER_FRAME_BYTES_MAX,
    BROKER_RESPONSE_BYTES_MAX,
    BrokerProtocolError,
    BrokerRequest,
    canonical_json,
)

BROKER_JOURNAL_SCHEMA_VERSION = 2
_LEGACY_V1_SCHEMA_VERSION = 1
_APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STAGES = frozenset({"prepare", "runtime_setup", "launch"})
_UNRESOLVED_REASONS = frozenset(
    {
        "transport_unknown",
        "timeout_unknown",
        "cancellation_unknown",
        "verification_unknown",
        "sqlite_commit_unknown",
        "broker_restart_unknown",
    }
)
_STAGE_STATUSES = {
    "prepare": frozenset({"prepared", "rejected"}),
    "runtime_setup": frozenset({"runtime_ready"}),
    "launch": frozenset({"launched", "rejected"}),
}

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE broker_journal_meta (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 0),
        application_id TEXT NOT NULL,
        schema_version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE broker_journal_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        database_incarnation TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        request_type TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (
            event_type IN (
                'accepted', 'lifecycle_claimed', 'stage_started',
                'stage_outcome', 'stage_unresolved'
            )
        ),
        stage TEXT CHECK (stage IS NULL OR stage IN ('prepare', 'runtime_setup', 'launch')),
        nonce TEXT,
        connection_sequence INTEGER,
        payload_digest TEXT,
        request_frame BLOB,
        broker_incarnation TEXT,
        owner_token TEXT,
        stage_status TEXT,
        stage_error TEXT,
        unresolved_reason TEXT,
        response_frame BLOB,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (
            (event_type = 'accepted'
                AND stage IS NULL AND nonce IS NOT NULL
                AND connection_sequence IS NOT NULL AND payload_digest IS NOT NULL
                AND request_frame IS NOT NULL AND broker_incarnation IS NULL
                AND owner_token IS NULL AND stage_status IS NULL AND stage_error IS NULL
                AND unresolved_reason IS NULL AND response_frame IS NULL)
            OR (event_type = 'lifecycle_claimed'
                AND stage IS NULL AND nonce IS NULL AND connection_sequence IS NULL
                AND payload_digest IS NULL AND request_frame IS NULL
                AND broker_incarnation IS NOT NULL AND owner_token IS NOT NULL
                AND stage_status IS NULL AND stage_error IS NULL
                AND unresolved_reason IS NULL AND response_frame IS NULL)
            OR (event_type = 'stage_started'
                AND stage IS NOT NULL AND nonce IS NULL AND connection_sequence IS NULL
                AND payload_digest IS NULL AND request_frame IS NULL
                AND broker_incarnation IS NULL AND owner_token IS NOT NULL
                AND stage_status IS NULL AND stage_error IS NULL
                AND unresolved_reason IS NULL AND response_frame IS NULL)
            OR (event_type = 'stage_outcome'
                AND stage IS NOT NULL AND nonce IS NULL AND connection_sequence IS NULL
                AND payload_digest IS NULL AND request_frame IS NULL
                AND broker_incarnation IS NULL AND owner_token IS NOT NULL
                AND stage_status IS NOT NULL
                AND ((stage_status = 'rejected' AND stage_error IS NOT NULL)
                    OR (stage_status != 'rejected' AND stage_error IS NULL))
                AND unresolved_reason IS NULL
                AND ((stage = 'launch' OR stage_status = 'rejected')
                    = (response_frame IS NOT NULL)))
            OR (event_type = 'stage_unresolved'
                AND stage IS NOT NULL AND nonce IS NULL AND connection_sequence IS NULL
                AND payload_digest IS NULL AND request_frame IS NULL
                AND broker_incarnation IS NULL AND owner_token IS NOT NULL
                AND stage_status IS NULL AND stage_error IS NULL
                AND unresolved_reason IS NOT NULL AND response_frame IS NOT NULL)
        )
    )
    """,
    """
    CREATE UNIQUE INDEX broker_journal_one_acceptance
    ON broker_journal_events(database_incarnation, operation_id, request_type)
    WHERE event_type = 'accepted'
    """,
    """
    CREATE UNIQUE INDEX broker_journal_nonce_once
    ON broker_journal_events(database_incarnation, nonce)
    WHERE event_type = 'accepted'
    """,
    """
    CREATE UNIQUE INDEX broker_journal_sequence_once
    ON broker_journal_events(database_incarnation, connection_sequence)
    WHERE event_type = 'accepted'
    """,
    """
    CREATE UNIQUE INDEX broker_journal_one_claim
    ON broker_journal_events(database_incarnation, operation_id, request_type)
    WHERE event_type = 'lifecycle_claimed'
    """,
    """
    CREATE UNIQUE INDEX broker_journal_one_stage_start
    ON broker_journal_events(database_incarnation, operation_id, request_type, stage)
    WHERE event_type = 'stage_started'
    """,
    """
    CREATE UNIQUE INDEX broker_journal_one_stage_completion
    ON broker_journal_events(database_incarnation, operation_id, request_type, stage)
    WHERE event_type IN ('stage_outcome', 'stage_unresolved')
    """,
    """
    CREATE UNIQUE INDEX broker_journal_one_terminal
    ON broker_journal_events(database_incarnation, operation_id, request_type)
    WHERE event_type = 'stage_unresolved'
       OR (event_type = 'stage_outcome' AND (stage = 'launch' OR stage_status = 'rejected'))
    """,
    """
    CREATE TRIGGER broker_journal_reject_update
    BEFORE UPDATE ON broker_journal_events
    BEGIN
        SELECT RAISE(ABORT, 'broker journal events are immutable');
    END
    """,
    """
    CREATE TRIGGER broker_journal_reject_delete
    BEFORE DELETE ON broker_journal_events
    BEGIN
        SELECT RAISE(ABORT, 'broker journal events are immutable');
    END
    """,
    """
    CREATE TRIGGER broker_journal_require_transition
    BEFORE INSERT ON broker_journal_events
    WHEN NEW.event_type != 'accepted'
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM broker_journal_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id AND request_type = NEW.request_type
              AND event_type = 'accepted'
        ) THEN RAISE(ABORT, 'broker journal acceptance is missing') END;
        SELECT CASE WHEN NEW.event_type != 'lifecycle_claimed' AND NOT EXISTS (
            SELECT 1 FROM broker_journal_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id AND request_type = NEW.request_type
              AND event_type = 'lifecycle_claimed' AND owner_token = NEW.owner_token
        ) THEN RAISE(ABORT, 'broker journal lifecycle ownership is missing') END;
        SELECT CASE WHEN NEW.event_type IN ('stage_outcome', 'stage_unresolved') AND NOT EXISTS (
            SELECT 1 FROM broker_journal_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id AND request_type = NEW.request_type
              AND event_type = 'stage_started' AND stage = NEW.stage
              AND owner_token = NEW.owner_token
        ) THEN RAISE(ABORT, 'broker journal stage start is missing') END;
        SELECT CASE WHEN NEW.event_type = 'stage_started' AND NEW.stage = 'runtime_setup'
            AND NOT EXISTS (
                SELECT 1 FROM broker_journal_events
                WHERE database_incarnation = NEW.database_incarnation
                  AND operation_id = NEW.operation_id AND request_type = NEW.request_type
                  AND event_type = 'stage_outcome' AND stage = 'prepare'
                  AND stage_status = 'prepared'
            ) THEN RAISE(ABORT, 'broker journal prepare outcome is missing') END;
        SELECT CASE WHEN NEW.event_type = 'stage_started' AND NEW.stage = 'launch'
            AND NOT EXISTS (
                SELECT 1 FROM broker_journal_events
                WHERE database_incarnation = NEW.database_incarnation
                  AND operation_id = NEW.operation_id AND request_type = NEW.request_type
                  AND event_type = 'stage_outcome' AND stage = 'runtime_setup'
                  AND stage_status = 'runtime_ready'
            ) THEN RAISE(ABORT, 'broker journal runtime outcome is missing') END;
        SELECT CASE WHEN EXISTS (
            SELECT 1 FROM broker_journal_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id AND request_type = NEW.request_type
              AND (event_type = 'stage_unresolved'
                OR (event_type = 'stage_outcome'
                    AND (stage = 'launch' OR stage_status = 'rejected')))
        ) THEN RAISE(ABORT, 'broker journal terminal state is immutable') END;
    END
    """,
    """
    CREATE TRIGGER broker_journal_meta_reject_update
    BEFORE UPDATE ON broker_journal_meta
    BEGIN
        SELECT RAISE(ABORT, 'broker journal metadata is immutable');
    END
    """,
    """
    CREATE TRIGGER broker_journal_meta_reject_delete
    BEFORE DELETE ON broker_journal_meta
    BEGIN
        SELECT RAISE(ABORT, 'broker journal metadata is immutable');
    END
    """,
)


LEGACY_V1_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE broker_journal_meta (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 0),
        application_id TEXT NOT NULL,
        schema_version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE broker_journal_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        database_incarnation TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        request_type TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (
            event_type IN ('accepted', 'launcher_claimed', 'launcher_outcome', 'unresolved')
        ),
        nonce TEXT,
        connection_sequence INTEGER,
        payload_digest TEXT,
        request_frame BLOB,
        broker_incarnation TEXT,
        owner_token TEXT,
        launcher_status TEXT,
        launcher_error TEXT,
        unresolved_reason TEXT,
        response_frame BLOB,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (
            (event_type = 'accepted'
                AND nonce IS NOT NULL
                AND connection_sequence IS NOT NULL
                AND payload_digest IS NOT NULL
                AND request_frame IS NOT NULL
                AND broker_incarnation IS NULL
                AND owner_token IS NULL
                AND launcher_status IS NULL
                AND launcher_error IS NULL
                AND unresolved_reason IS NULL
                AND response_frame IS NULL)
            OR (event_type = 'launcher_claimed'
                AND nonce IS NULL
                AND connection_sequence IS NULL
                AND payload_digest IS NULL
                AND request_frame IS NULL
                AND broker_incarnation IS NOT NULL
                AND owner_token IS NOT NULL
                AND launcher_status IS NULL
                AND launcher_error IS NULL
                AND unresolved_reason IS NULL
                AND response_frame IS NULL)
            OR (event_type = 'launcher_outcome'
                AND nonce IS NULL
                AND connection_sequence IS NULL
                AND payload_digest IS NULL
                AND request_frame IS NULL
                AND broker_incarnation IS NULL
                AND owner_token IS NOT NULL
                AND launcher_status IN ('launched', 'rejected')
                AND ((launcher_status = 'launched' AND launcher_error IS NULL)
                    OR (launcher_status = 'rejected' AND launcher_error IS NOT NULL))
                AND unresolved_reason IS NULL
                AND response_frame IS NOT NULL)
            OR (event_type = 'unresolved'
                AND nonce IS NULL
                AND connection_sequence IS NULL
                AND payload_digest IS NULL
                AND request_frame IS NULL
                AND broker_incarnation IS NULL
                AND owner_token IS NOT NULL
                AND launcher_status IS NULL
                AND launcher_error IS NULL
                AND unresolved_reason IN (
                    'launcher_unreachable',
                    'launcher_unverifiable',
                    'cancelled_uncertain'
                )
                AND response_frame IS NULL)
        )
    )
    """,
    """
    CREATE UNIQUE INDEX broker_journal_one_acceptance
    ON broker_journal_events(database_incarnation, operation_id, request_type)
    WHERE event_type = 'accepted'
    """,
    """
    CREATE UNIQUE INDEX broker_journal_nonce_once
    ON broker_journal_events(database_incarnation, nonce)
    WHERE event_type = 'accepted'
    """,
    """
    CREATE UNIQUE INDEX broker_journal_sequence_once
    ON broker_journal_events(database_incarnation, connection_sequence)
    WHERE event_type = 'accepted'
    """,
    """
    CREATE UNIQUE INDEX broker_journal_one_claim
    ON broker_journal_events(database_incarnation, operation_id, request_type)
    WHERE event_type = 'launcher_claimed'
    """,
    """
    CREATE UNIQUE INDEX broker_journal_one_outcome
    ON broker_journal_events(database_incarnation, operation_id, request_type)
    WHERE event_type = 'launcher_outcome'
    """,
    """
    CREATE UNIQUE INDEX broker_journal_one_unresolved
    ON broker_journal_events(database_incarnation, operation_id, request_type)
    WHERE event_type = 'unresolved'
    """,
    """
    CREATE TRIGGER broker_journal_reject_update
    BEFORE UPDATE ON broker_journal_events
    BEGIN
        SELECT RAISE(ABORT, 'broker journal events are immutable');
    END
    """,
    """
    CREATE TRIGGER broker_journal_reject_delete
    BEFORE DELETE ON broker_journal_events
    BEGIN
        SELECT RAISE(ABORT, 'broker journal events are immutable');
    END
    """,
    """
    CREATE TRIGGER broker_journal_require_transition
    BEFORE INSERT ON broker_journal_events
    WHEN NEW.event_type != 'accepted'
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM broker_journal_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id
              AND request_type = NEW.request_type
              AND event_type = 'accepted'
        ) THEN RAISE(ABORT, 'broker journal acceptance is missing') END;
        SELECT CASE WHEN NEW.event_type IN ('launcher_outcome', 'unresolved') AND NOT EXISTS (
            SELECT 1 FROM broker_journal_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id
              AND request_type = NEW.request_type
              AND event_type = 'launcher_claimed'
              AND owner_token = NEW.owner_token
        ) THEN RAISE(ABORT, 'broker journal launch ownership is missing') END;
        SELECT CASE WHEN NEW.event_type IN ('launcher_outcome', 'unresolved') AND EXISTS (
            SELECT 1 FROM broker_journal_events
            WHERE database_incarnation = NEW.database_incarnation
              AND operation_id = NEW.operation_id
              AND request_type = NEW.request_type
              AND event_type IN ('launcher_outcome', 'unresolved')
        ) THEN RAISE(ABORT, 'broker journal terminal state is immutable') END;
    END
    """,
    """
    CREATE TRIGGER broker_journal_meta_reject_update
    BEFORE UPDATE ON broker_journal_meta
    BEGIN
        SELECT RAISE(ABORT, 'broker journal metadata is immutable');
    END
    """,
    """
    CREATE TRIGGER broker_journal_meta_reject_delete
    BEFORE DELETE ON broker_journal_meta
    BEGIN
        SELECT RAISE(ABORT, 'broker journal metadata is immutable');
    END
    """,
)


class JournalSyncError(RuntimeError):
    """Report uncertain or failed durable journal synchronization."""


class LegacyJournalStateError(JournalSyncError):
    """Reject unsafe legacy v1 journal state before broker startup."""


class JournalCommitAbsent(JournalSyncError):
    """Report a confirmed missing journal event after an uncertain commit."""


@dataclass(frozen=True, slots=True)
class JournalDecision:
    state: str
    stage: str | None = None
    response_frame: bytes | None = None
    owner_broker_incarnation: str | None = None


@dataclass(frozen=True, slots=True)
class IncompleteLifecycle:
    request_frame: bytes
    owner_token: str
    stage: str
    stage_started: bool


def _normalized_sql(value: str) -> str:
    return " ".join(value.split())


def _exact_event_query(
    identity: tuple[str, str, str],
    event_type: str,
    exact: dict[str, object],
) -> tuple[str, tuple[object, ...]]:
    columns = ("database_incarnation", "operation_id", "request_type", "event_type", *exact)
    parameters: list[object] = [*identity, event_type, *exact.values()]
    conditions = " AND ".join(f"{column} IS ?" for column in columns)
    return (
        f"SELECT 1 FROM broker_journal_events WHERE {conditions} LIMIT 1",
        tuple(parameters),
    )


@lru_cache(maxsize=1)
def _expected_schema() -> dict[tuple[str, str], str]:
    database = sqlite3.connect(":memory:")
    try:
        for statement in _SCHEMA_STATEMENTS:
            database.execute(statement)
        rows = database.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {(str(kind), str(name)): _normalized_sql(str(sql)) for kind, name, sql in rows}
    finally:
        database.close()


def _validate_stored_json(frame: object, *, maximum: int) -> None:
    if not isinstance(frame, bytes) or not frame or len(frame) > maximum:
        raise JournalSyncError("broker journal contains a malformed stored frame")
    try:
        value = json.loads(frame)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise JournalSyncError("broker journal contains a malformed stored frame") from exc
    if not isinstance(value, dict) or canonical_json(value) != frame:
        raise JournalSyncError("broker journal contains a malformed stored frame")


@lru_cache(maxsize=1)
def _legacy_v1_expected_schema() -> dict[tuple[str, str], str]:
    database = sqlite3.connect(":memory:")
    try:
        for statement in LEGACY_V1_SCHEMA_STATEMENTS:
            database.execute(statement)
        rows = database.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {(str(kind), str(name)): _normalized_sql(str(sql)) for kind, name, sql in rows}
    finally:
        database.close()


def _connect_legacy_read_only(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    database = sqlite3.connect(uri, uri=True, timeout=5.0)
    database.execute("PRAGMA query_only = ON")
    return database


def _legacy_sidecar_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LegacyJournalStateError("legacy broker journal sidecar is unreadable") from exc
    return True


def inspect_legacy_v1_journal(path: Path, *, application_id: str) -> None:
    """Validate the legacy v1 journal without changing any legacy file."""
    if (
        not isinstance(application_id, str)
        or _APPLICATION_ID_PATTERN.fullmatch(application_id) is None
    ):
        raise ValueError("broker journal application ID is invalid")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        for suffix in ("-wal", "-shm"):
            if _legacy_sidecar_exists(Path(f"{path}{suffix}")):
                raise LegacyJournalStateError("legacy broker journal sidecar is orphaned")
        return
    except OSError as exc:
        raise LegacyJournalStateError("legacy broker journal is unreadable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise LegacyJournalStateError("legacy broker journal is unreadable")
    for suffix in ("-wal", "-shm"):
        if _legacy_sidecar_exists(Path(f"{path}{suffix}")):
            raise LegacyJournalStateError("legacy broker journal has unresolved sidecar state")
    try:
        database = _connect_legacy_read_only(path)
    except (OSError, sqlite3.Error) as exc:
        raise LegacyJournalStateError("legacy broker journal is unreadable") from exc
    try:
        _validate_legacy_v1_database(database, application_id)
    finally:
        database.close()


def _validate_legacy_v1_database(
    database: sqlite3.Connection,
    application_id: str,
) -> None:
    try:
        if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise LegacyJournalStateError("legacy broker journal is malformed")
        objects = database.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        actual = {(str(kind), str(name)): _normalized_sql(str(sql)) for kind, name, sql in objects}
        if actual != _legacy_v1_expected_schema():
            raise LegacyJournalStateError("legacy broker journal schema is malformed")
        meta = database.execute(
            "SELECT singleton, application_id, schema_version FROM broker_journal_meta"
        ).fetchall()
        if meta == [(0, application_id, _LEGACY_V1_SCHEMA_VERSION)]:
            accepted = database.execute(
                "SELECT 1 FROM broker_journal_events WHERE event_type = 'accepted' LIMIT 1"
            ).fetchone()
            if accepted is not None:
                raise LegacyJournalStateError(
                    "legacy broker journal contains an accepted operation"
                )
            return
        stored_version = meta[0][2] if len(meta) == 1 else None
        if isinstance(stored_version, int) and stored_version > _LEGACY_V1_SCHEMA_VERSION:
            raise LegacyJournalStateError("legacy broker journal version is unsupported")
        raise LegacyJournalStateError("legacy broker journal metadata is malformed")
    except LegacyJournalStateError:
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise LegacyJournalStateError("legacy broker journal is unreadable") from exc


class BrokerJournal:
    """Own append transitions without retaining SQLite handles between calls."""

    def __init__(self, path: Path, *, application_id: str) -> None:
        if (
            not isinstance(application_id, str)
            or _APPLICATION_ID_PATTERN.fullmatch(application_id) is None
        ):
            raise ValueError("broker journal application ID is invalid")
        self._path = path
        self._application_id = application_id
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _identity(request: BrokerRequest) -> tuple[str, str, str]:
        return request.database_incarnation, request.operation_id, request.request_type

    def _connect(self) -> sqlite3.Connection:
        try:
            database = sqlite3.connect(self._path, timeout=5.0)
            database.execute("PRAGMA busy_timeout = 5000")
            database.execute("PRAGMA foreign_keys = ON")
            database.execute("PRAGMA journal_mode = DELETE")
            database.execute("PRAGMA synchronous = FULL")
            database.execute("PRAGMA fullfsync = ON")
            return database
        except (OSError, sqlite3.Error) as exc:
            raise JournalSyncError("broker journal synchronization failed") from exc

    def _initialize(self) -> None:
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._path.parent, 0o700)
            database = self._connect()
            try:
                database.execute("BEGIN EXCLUSIVE")
                existing = database.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
                if existing is None:
                    for statement in _SCHEMA_STATEMENTS:
                        database.execute(statement)
                    database.execute(
                        "INSERT INTO broker_journal_meta VALUES (0, ?, ?)",
                        (self._application_id, BROKER_JOURNAL_SCHEMA_VERSION),
                    )
                database.commit()
            finally:
                database.close()
            os.chmod(self._path, 0o600)
            parent = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
            with self._open():
                pass
        except JournalSyncError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise JournalSyncError("broker journal synchronization failed") from exc

    def _open(self) -> sqlite3.Connection:
        database = self._connect()
        try:
            self._validate(database)
        except BaseException:
            database.close()
            raise
        return database

    def _validate(self, database: sqlite3.Connection) -> None:
        try:
            if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise JournalSyncError("broker journal integrity check failed")
            rows = database.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            actual = {(str(kind), str(name)): _normalized_sql(str(sql)) for kind, name, sql in rows}
            if actual != _expected_schema():
                raise JournalSyncError("broker journal schema objects are unsupported")
            meta = database.execute(
                "SELECT singleton, application_id, schema_version FROM broker_journal_meta"
            ).fetchall()
            if meta != [(0, self._application_id, BROKER_JOURNAL_SCHEMA_VERSION)]:
                raise JournalSyncError(
                    "broker journal application or schema version is unsupported"
                )
            self._validate_events(database)
        except JournalSyncError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise JournalSyncError("broker journal validation failed") from exc

    def _validate_events(self, database: sqlite3.Connection) -> None:
        histories: dict[tuple[str, str, str], list[tuple[str, str | None, str | None]]] = {}
        owner_tokens: dict[tuple[str, str, str], str] = {}
        rows = database.execute(
            """
            SELECT database_incarnation, operation_id, request_type, event_type, stage,
                   nonce, payload_digest, request_frame, broker_incarnation, owner_token,
                   stage_status, unresolved_reason, response_frame, connection_sequence
            FROM broker_journal_events ORDER BY event_id
            """
        ).fetchall()
        for row in rows:
            identity = str(row[0]), str(row[1]), str(row[2])
            event_type = str(row[3])
            stage = None if row[4] is None else str(row[4])
            status = None if row[10] is None else str(row[10])
            history = histories.setdefault(identity, [])
            if event_type == "accepted":
                if (
                    history
                    or not isinstance(row[6], str)
                    or _DIGEST_PATTERN.fullmatch(row[6]) is None
                ):
                    raise JournalSyncError("broker journal accepted event is invalid")
                _validate_stored_json(row[7], maximum=BROKER_FRAME_BYTES_MAX)
                message = json.loads(bytes(row[7]))
                if (
                    not isinstance(message, dict)
                    or message.get("database_incarnation") != identity[0]
                    or message.get("operation_id") != identity[1]
                    or message.get("request_type") != identity[2]
                    or message.get("nonce") != row[5]
                    or message.get("payload_digest") != row[6]
                    or message.get("connection_sequence") != row[13]
                ):
                    raise JournalSyncError("broker journal accepted frame binding is invalid")
            elif event_type == "lifecycle_claimed":
                if [item[0] for item in history] != ["accepted"]:
                    raise JournalSyncError("broker journal claim event order is invalid")
                if any(
                    not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None
                    for value in (row[8], row[9])
                ):
                    raise JournalSyncError("broker journal lifecycle owner is malformed")
                owner_tokens[identity] = str(row[9])
            elif row[9] != owner_tokens.get(identity):
                raise JournalSyncError("broker journal lifecycle owner changed")
            elif event_type == "stage_started":
                self._validate_stage_start(history, stage)
            elif event_type in {"stage_outcome", "stage_unresolved"}:
                if stage not in _STAGES or not any(
                    item[0] == "stage_started" and item[1] == stage for item in history
                ):
                    raise JournalSyncError("broker journal stage outcome order is invalid")
                if event_type == "stage_outcome" and status not in _STAGE_STATUSES[stage]:
                    raise JournalSyncError("broker journal stage outcome is invalid")
                if event_type == "stage_unresolved" and row[11] not in _UNRESOLVED_REASONS:
                    raise JournalSyncError("broker journal unresolved reason is invalid")
                if row[12] is not None:
                    _validate_stored_json(row[12], maximum=BROKER_RESPONSE_BYTES_MAX)
            else:
                raise JournalSyncError("broker journal event type is unsupported")
            history.append((event_type, stage, status))

    @staticmethod
    def _validate_stage_start(
        history: list[tuple[str, str | None, str | None]], stage: str | None
    ) -> None:
        if len(history) < 2 or history[0][0] != "accepted" or history[1][0] != "lifecycle_claimed":
            raise JournalSyncError("broker journal stage start order is invalid")
        if stage == "prepare" and len(history) == 2:
            return
        if stage is None:
            raise JournalSyncError("broker journal stage start order is invalid")
        expected = {
            "runtime_setup": ("stage_outcome", "prepare", "prepared"),
            "launch": ("stage_outcome", "runtime_setup", "runtime_ready"),
        }.get(stage)
        if expected is None or not history or history[-1] != expected:
            raise JournalSyncError("broker journal stage start order is invalid")

    @staticmethod
    def _terminal(
        database: sqlite3.Connection, identity: tuple[str, str, str]
    ) -> JournalDecision | None:
        row = database.execute(
            """
            SELECT event_type, stage, response_frame FROM broker_journal_events
            WHERE database_incarnation = ? AND operation_id = ? AND request_type = ?
              AND (event_type = 'stage_unresolved'
                OR (event_type = 'stage_outcome'
                    AND (stage = 'launch' OR stage_status = 'rejected')))
            ORDER BY event_id DESC LIMIT 1
            """,
            identity,
        ).fetchone()
        if row is None:
            return None
        return JournalDecision(
            "unresolved" if row[0] == "stage_unresolved" else "terminal",
            stage=str(row[1]),
            response_frame=bytes(row[2]),
        )

    def _commit_transaction(self, database: sqlite3.Connection) -> None:
        """Commit one append transaction through an overridable fault boundary."""
        database.commit()

    def _verify_intended_event(
        self,
        identity: tuple[str, str, str],
        event_type: str,
        exact: dict[str, object],
    ) -> bool:
        database = self._open()
        try:
            sql, parameters = _exact_event_query(identity, event_type, exact)
            return database.execute(sql, parameters).fetchone() is not None
        except (OSError, sqlite3.Error, TypeError) as exc:
            raise JournalSyncError("broker journal verification failed") from exc
        finally:
            database.close()

    def _confirm_appended_event(
        self,
        *,
        identity: tuple[str, str, str],
        event_type: str,
        exact: dict[str, object],
    ) -> None:
        if self._verify_intended_event(identity, event_type, exact):
            return
        raise JournalCommitAbsent("broker journal commit is absent after verification")

    def _append_verified(
        self,
        *,
        insert_sql: str,
        insert_params: tuple[object, ...],
        identity: tuple[str, str, str],
        event_type: str,
        exact: dict[str, object],
    ) -> None:
        database = self._open()
        commit_uncertain = False
        try:
            database.execute("BEGIN IMMEDIATE")
            database.execute(insert_sql, insert_params)
            try:
                self._commit_transaction(database)
            except (OSError, sqlite3.Error):
                commit_uncertain = True
        except (OSError, sqlite3.Error, TypeError) as exc:
            raise JournalSyncError("broker journal synchronization failed") from exc
        finally:
            database.close()
        if commit_uncertain:
            self._confirm_appended_event(identity=identity, event_type=event_type, exact=exact)

    def accept(self, request: BrokerRequest, request_frame: bytes) -> JournalDecision:
        identity = self._identity(request)
        database = self._open()
        commit_uncertain = False
        try:
            database.execute("BEGIN IMMEDIATE")
            accepted = database.execute(
                """
                SELECT nonce, connection_sequence, payload_digest, request_frame
                FROM broker_journal_events
                WHERE database_incarnation = ? AND operation_id = ? AND request_type = ?
                  AND event_type = 'accepted'
                """,
                identity,
            ).fetchone()
            if accepted is not None:
                if (
                    accepted[0] != request.nonce
                    or accepted[1] != request.connection_sequence
                    or accepted[2] != request.payload_digest
                    or bytes(accepted[3]) != request_frame
                ):
                    raise BrokerProtocolError("operation payload mismatch")
                return self._terminal(database, identity) or JournalDecision("accepted")
            if (
                database.execute(
                    "SELECT 1 FROM broker_journal_events WHERE database_incarnation = ? "
                    "AND nonce = ? AND event_type = 'accepted'",
                    (request.database_incarnation, request.nonce),
                ).fetchone()
                is not None
            ):
                raise BrokerProtocolError("duplicate nonce")
            latest = database.execute(
                "SELECT MAX(connection_sequence) FROM broker_journal_events "
                "WHERE database_incarnation = ? AND event_type = 'accepted'",
                (request.database_incarnation,),
            ).fetchone()[0]
            if latest is not None and request.connection_sequence <= latest:
                raise BrokerProtocolError("stale connection sequence")
            database.execute(
                """
                INSERT INTO broker_journal_events (
                    database_incarnation, operation_id, request_type, event_type,
                    nonce, connection_sequence, payload_digest, request_frame
                ) VALUES (?, ?, ?, 'accepted', ?, ?, ?, ?)
                """,
                (
                    *identity,
                    request.nonce,
                    request.connection_sequence,
                    request.payload_digest,
                    request_frame,
                ),
            )
            try:
                self._commit_transaction(database)
            except (OSError, sqlite3.Error):
                commit_uncertain = True
        except BrokerProtocolError:
            raise
        except (OSError, sqlite3.Error, TypeError) as exc:
            raise JournalSyncError("broker journal synchronization failed") from exc
        finally:
            database.close()
        if commit_uncertain:
            self._confirm_appended_event(
                identity=identity,
                event_type="accepted",
                exact={
                    "nonce": request.nonce,
                    "connection_sequence": request.connection_sequence,
                    "payload_digest": request.payload_digest,
                    "request_frame": request_frame,
                },
            )
        return JournalDecision("accepted")

    def claim_lifecycle(
        self, request: BrokerRequest, *, owner_token: str, broker_incarnation: str
    ) -> JournalDecision:
        if (
            _TOKEN_PATTERN.fullmatch(owner_token) is None
            or _TOKEN_PATTERN.fullmatch(broker_incarnation) is None
        ):
            raise ValueError("broker lifecycle owner is invalid")
        identity = self._identity(request)
        database = self._open()
        commit_uncertain = False
        try:
            database.execute("BEGIN IMMEDIATE")
            terminal = self._terminal(database, identity)
            if terminal is not None:
                return terminal
            claim = database.execute(
                "SELECT broker_incarnation FROM broker_journal_events "
                "WHERE database_incarnation = ? AND operation_id = ? AND request_type = ? "
                "AND event_type = 'lifecycle_claimed'",
                identity,
            ).fetchone()
            if claim is not None:
                return JournalDecision("in_flight", owner_broker_incarnation=str(claim[0]))
            database.execute(
                "INSERT INTO broker_journal_events (database_incarnation, operation_id, "
                "request_type, event_type, broker_incarnation, owner_token) "
                "VALUES (?, ?, ?, 'lifecycle_claimed', ?, ?)",
                (*identity, broker_incarnation, owner_token),
            )
            try:
                self._commit_transaction(database)
            except (OSError, sqlite3.Error):
                commit_uncertain = True
        except (OSError, sqlite3.Error, TypeError) as exc:
            raise JournalSyncError("broker journal synchronization failed") from exc
        finally:
            database.close()
        if commit_uncertain:
            self._confirm_appended_event(
                identity=identity,
                event_type="lifecycle_claimed",
                exact={"broker_incarnation": broker_incarnation, "owner_token": owner_token},
            )
        return JournalDecision("claimed", owner_broker_incarnation=broker_incarnation)

    def claim_launcher(
        self, request: BrokerRequest, *, owner_token: str, broker_incarnation: str
    ) -> JournalDecision:
        return self.claim_lifecycle(
            request, owner_token=owner_token, broker_incarnation=broker_incarnation
        )

    def begin_stage(self, request: BrokerRequest, *, owner_token: str, stage: str) -> None:
        if stage not in _STAGES:
            raise ValueError("broker lifecycle stage is invalid")
        identity = self._identity(request)
        self._append_verified(
            insert_sql=(
                "INSERT INTO broker_journal_events (database_incarnation, operation_id, "
                "request_type, event_type, stage, owner_token) "
                "VALUES (?, ?, ?, 'stage_started', ?, ?)"
            ),
            insert_params=(*identity, stage, owner_token),
            identity=identity,
            event_type="stage_started",
            exact={"stage": stage, "owner_token": owner_token},
        )

    def record_stage_outcome(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
        stage_status: str,
        stage_error: str | None = None,
        response_frame: bytes | None = None,
    ) -> None:
        if stage not in _STAGES or stage_status not in _STAGE_STATUSES[stage]:
            raise ValueError("broker lifecycle outcome is invalid")
        identity = self._identity(request)
        self._append_verified(
            insert_sql=(
                "INSERT INTO broker_journal_events (database_incarnation, operation_id, "
                "request_type, event_type, stage, owner_token, stage_status, stage_error, "
                "response_frame) VALUES (?, ?, ?, 'stage_outcome', ?, ?, ?, ?, ?)"
            ),
            insert_params=(
                *identity,
                stage,
                owner_token,
                stage_status,
                stage_error,
                response_frame,
            ),
            identity=identity,
            event_type="stage_outcome",
            exact={
                "stage": stage,
                "owner_token": owner_token,
                "stage_status": stage_status,
                "stage_error": stage_error,
                "response_frame": response_frame,
            },
        )

    def record_stage_unresolved(
        self,
        request: BrokerRequest,
        *,
        owner_token: str,
        stage: str,
        reason: str,
        response_frame: bytes,
    ) -> None:
        if stage not in _STAGES or reason not in _UNRESOLVED_REASONS:
            raise ValueError("unresolved lifecycle outcome is invalid")
        identity = self._identity(request)
        database = self._open()
        commit_uncertain = False
        try:
            database.execute("BEGIN IMMEDIATE")
            terminal = self._terminal(database, identity)
            if terminal is not None:
                exact_sql, exact_parameters = _exact_event_query(
                    identity,
                    "stage_unresolved",
                    {
                        "stage": stage,
                        "owner_token": owner_token,
                        "unresolved_reason": reason,
                        "response_frame": response_frame,
                    },
                )
                if database.execute(exact_sql, exact_parameters).fetchone() is not None:
                    return
                raise JournalSyncError("broker journal terminal state is immutable")
            database.execute(
                "INSERT INTO broker_journal_events (database_incarnation, operation_id, "
                "request_type, event_type, stage, owner_token, unresolved_reason, "
                "response_frame) VALUES (?, ?, ?, 'stage_unresolved', ?, ?, ?, ?)",
                (*identity, stage, owner_token, reason, response_frame),
            )
            try:
                self._commit_transaction(database)
            except (OSError, sqlite3.Error):
                commit_uncertain = True
        except JournalSyncError:
            raise
        except (OSError, sqlite3.Error, TypeError) as exc:
            raise JournalSyncError("broker journal synchronization failed") from exc
        finally:
            database.close()
        if commit_uncertain:
            self._confirm_appended_event(
                identity=identity,
                event_type="stage_unresolved",
                exact={
                    "stage": stage,
                    "owner_token": owner_token,
                    "unresolved_reason": reason,
                    "response_frame": response_frame,
                },
            )

    def incomplete_lifecycles(self) -> tuple[IncompleteLifecycle, ...]:
        """Return claimed nonterminal operations for startup reconciliation."""
        database = self._open()
        try:
            identities = database.execute(
                """
                SELECT a.database_incarnation, a.operation_id, a.request_type,
                       a.request_frame, c.owner_token
                FROM broker_journal_events AS a
                JOIN broker_journal_events AS c
                  ON c.database_incarnation = a.database_incarnation
                 AND c.operation_id = a.operation_id
                 AND c.request_type = a.request_type
                 AND c.event_type = 'lifecycle_claimed'
                WHERE a.event_type = 'accepted'
                  AND NOT EXISTS (
                    SELECT 1 FROM broker_journal_events AS terminal
                    WHERE terminal.database_incarnation = a.database_incarnation
                      AND terminal.operation_id = a.operation_id
                      AND terminal.request_type = a.request_type
                      AND (terminal.event_type = 'stage_unresolved'
                        OR (terminal.event_type = 'stage_outcome'
                          AND (terminal.stage = 'launch'
                            OR terminal.stage_status = 'rejected')))
                  )
                ORDER BY a.event_id
                """
            ).fetchall()
            results: list[IncompleteLifecycle] = []
            for database_id, operation_id, request_type, frame, owner_token in identities:
                identity = str(database_id), str(operation_id), str(request_type)
                stages = database.execute(
                    "SELECT event_type, stage, stage_status FROM broker_journal_events "
                    "WHERE database_incarnation = ? AND operation_id = ? AND request_type = ? "
                    "AND event_type IN ('stage_started', 'stage_outcome') "
                    "ORDER BY event_id",
                    identity,
                ).fetchall()
                started = {
                    str(stage)
                    for event_type, stage, _status in stages
                    if event_type == "stage_started"
                }
                completed = {
                    str(stage)
                    for event_type, stage, _status in stages
                    if event_type == "stage_outcome"
                }
                unmatched = started - completed
                if unmatched:
                    stage = next(
                        name for name in ("prepare", "runtime_setup", "launch") if name in unmatched
                    )
                    stage_started = True
                elif "runtime_setup" in completed:
                    stage, stage_started = "launch", False
                elif "prepare" in completed:
                    stage, stage_started = "runtime_setup", False
                else:
                    stage, stage_started = "prepare", False
                results.append(
                    IncompleteLifecycle(
                        request_frame=bytes(frame),
                        owner_token=str(owner_token),
                        stage=stage,
                        stage_started=stage_started,
                    )
                )
            return tuple(results)
        except (OSError, sqlite3.Error, TypeError) as exc:
            raise JournalSyncError("broker journal synchronization failed") from exc
        finally:
            database.close()

    def status(self, request: BrokerRequest) -> JournalDecision:
        database = self._open()
        try:
            identity = self._identity(request)
            terminal = self._terminal(database, identity)
            if terminal is not None:
                return terminal
            claim = database.execute(
                "SELECT broker_incarnation FROM broker_journal_events "
                "WHERE database_incarnation = ? AND operation_id = ? AND request_type = ? "
                "AND event_type = 'lifecycle_claimed'",
                identity,
            ).fetchone()
            if claim is None:
                return JournalDecision("accepted")
            stage = database.execute(
                "SELECT stage FROM broker_journal_events WHERE database_incarnation = ? "
                "AND operation_id = ? AND request_type = ? AND event_type = 'stage_started' "
                "ORDER BY event_id DESC LIMIT 1",
                identity,
            ).fetchone()
            return JournalDecision(
                "in_flight",
                stage=None if stage is None else str(stage[0]),
                owner_broker_incarnation=str(claim[0]),
            )
        except (OSError, sqlite3.Error, TypeError) as exc:
            raise JournalSyncError("broker journal synchronization failed") from exc
        finally:
            database.close()
