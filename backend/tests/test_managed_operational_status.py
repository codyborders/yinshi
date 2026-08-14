"""Managed operational checks report alert counts without resource identifiers."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from yinshi.managed_operations_check import main
from yinshi.services.managed_operational_status import (
    ManagedAlertClass,
    collect_managed_operational_status,
)


def _database() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.executescript("""
        CREATE TABLE managed_runtimes (
            user_id TEXT PRIMARY KEY,
            lifecycle_status TEXT NOT NULL
        );
        CREATE TABLE managed_backup_archives (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            status TEXT NOT NULL,
            completed_at TEXT,
            last_error TEXT
        );
        CREATE TABLE managed_backup_operations (
            job_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            status TEXT NOT NULL,
            phase TEXT NOT NULL,
            lease_token TEXT,
            lease_expires_at TEXT,
            attempt_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            last_error TEXT,
            failure_class TEXT
        );
        CREATE TABLE managed_operational_failures (
            alert_class TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        """)
    return database


def test_status_aggregates_critical_alerts_without_identifiers() -> None:
    """Critical state becomes counts and ages, never tenant or job details."""
    database = _database()
    database.execute("INSERT INTO managed_runtimes VALUES (?, ?)", ("secret-user", "ready"))
    database.execute(
        "INSERT INTO managed_backup_archives VALUES (?, ?, ?, ?, ?)",
        ("secret-archive", "secret-user", "failed", None, "secret provider path"),
    )
    database.execute(
        "INSERT INTO managed_backup_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "secret-job",
            "restore",
            "failed",
            "candidate_created",
            None,
            None,
            3,
            "2026-08-12T08:00:00Z",
            "secret error",
            "restore_failed",
        ),
    )
    database.commit()

    report = collect_managed_operational_status(
        database,
        now=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        backup_stale_seconds=86_400,
        operation_stuck_seconds=3_600,
    )

    payload = report.to_dict()
    serialized = json.dumps(payload, sort_keys=True)
    assert payload["status"] == "critical"
    assert {alert["alert_class"] for alert in payload["alerts"]} == {
        ManagedAlertClass.BACKUP_STALE.value,
        ManagedAlertClass.RESTORE_FAILED.value,
    }
    assert "secret-user" not in serialized
    assert "secret-job" not in serialized
    assert "secret-archive" not in serialized
    assert "secret provider path" not in serialized
    assert "secret error" not in serialized


def test_status_includes_active_persisted_service_failures() -> None:
    """Persisted service failures become aggregate alerts without stored details."""
    database = _database()
    database.execute(
        "INSERT INTO managed_operational_failures VALUES (?, ?, ?)",
        (
            ManagedAlertClass.SPRITE_RECONCILIATION_FAILED.value,
            "2026-08-13T10:00:00Z",
            "2026-08-13T11:00:00Z",
        ),
    )
    database.commit()

    report = collect_managed_operational_status(
        database,
        now=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        backup_stale_seconds=86_400,
        operation_stuck_seconds=3_600,
    )

    assert report.to_dict()["alerts"] == [
        {
            "alert_class": ManagedAlertClass.SPRITE_RECONCILIATION_FAILED.value,
            "count": 1,
            "oldest_age_seconds": 7200,
        }
    ]


def test_checker_returns_nonzero_with_sanitized_json(
    tmp_path: Path,
    capsys: object,
) -> None:
    """CLI should support monitoring without printing stored identifiers."""
    database_path = tmp_path / "control.db"
    database = sqlite3.connect(database_path)
    database.row_factory = sqlite3.Row
    database.executescript("""
        CREATE TABLE managed_runtimes (user_id TEXT, lifecycle_status TEXT);
        CREATE TABLE managed_backup_archives (
            id TEXT, user_id TEXT, status TEXT, completed_at TEXT, last_error TEXT
        );
        CREATE TABLE managed_backup_operations (
            job_id TEXT, operation TEXT, status TEXT, phase TEXT, lease_token TEXT,
            lease_expires_at TEXT, attempt_count INTEGER, updated_at TEXT, last_error TEXT,
            failure_class TEXT
        );
        CREATE TABLE managed_operational_failures (
            alert_class TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        INSERT INTO managed_runtimes VALUES ('private-user', 'ready');
        """)
    database.close()

    exit_code = main(
        [
            "--control-db",
            str(database_path),
            "--now",
            "2026-08-13T12:00:00Z",
            "--backup-stale-seconds",
            "86400",
            "--operation-stuck-seconds",
            "3600",
        ]
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code == 2
    assert json.loads(captured.out)["status"] == "critical"
    assert "private-user" not in captured.out
    assert captured.err == ""
