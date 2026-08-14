"""Durable managed operational failure state uses bounded alert classes."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import _configure_test_env


def test_control_schema_creates_managed_operational_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control initialization must install durable operational failure state."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    init_control_db()
    with get_control_db() as database:
        columns = {
            row["name"]
            for row in database.execute(
                "PRAGMA table_info(managed_operational_failures)"
            ).fetchall()
        }

    assert columns == {"alert_class", "first_seen_at", "last_seen_at"}


def test_failure_upsert_preserves_first_seen_and_clear_removes_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated failures update recency while preserving initial detection time."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db
    from yinshi.services.managed_operational_failures import (
        ManagedPersistentAlertClass,
        clear_managed_operational_failure,
        record_managed_operational_failure,
    )

    get_settings.cache_clear()
    init_control_db()
    first = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
    second = first + timedelta(minutes=5)
    alert_class = ManagedPersistentAlertClass.SPRITE_RECONCILIATION_FAILED

    record_managed_operational_failure(alert_class, now=first)
    record_managed_operational_failure(alert_class, now=second)

    with get_control_db() as database:
        row = database.execute(
            "SELECT alert_class, first_seen_at, last_seen_at " "FROM managed_operational_failures"
        ).fetchone()
    assert row is not None
    assert row["alert_class"] == alert_class.value
    assert row["first_seen_at"] == "2026-08-13T10:00:00Z"
    assert row["last_seen_at"] == "2026-08-13T10:05:00Z"

    clear_managed_operational_failure(alert_class)
    with get_control_db() as database:
        assert (
            database.execute("SELECT COUNT(*) FROM managed_operational_failures").fetchone()[0] == 0
        )


def test_failure_helpers_reject_unapproved_classes_and_naive_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers cannot persist arbitrary alert names or ambiguous timestamps."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    from yinshi.config import get_settings
    from yinshi.db import init_control_db
    from yinshi.services.managed_operational_failures import (
        ManagedPersistentAlertClass,
        clear_managed_operational_failure,
        record_managed_operational_failure,
    )

    get_settings.cache_clear()
    init_control_db()
    with pytest.raises(ValueError, match="approved"):
        record_managed_operational_failure(  # type: ignore[arg-type]
            "private-provider-error",
            now=datetime.now(UTC),
        )
    with pytest.raises(ValueError, match="timezone"):
        record_managed_operational_failure(
            ManagedPersistentAlertClass.STORAGE_PREFLIGHT_FAILED,
            now=datetime(2026, 8, 13, 10, 0),
        )
    with pytest.raises(ValueError, match="approved"):
        clear_managed_operational_failure("private-provider-error")  # type: ignore[arg-type]


def test_control_schema_rejects_unapproved_alert_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Database constraints must reject bypasses around helper validation."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db

    get_settings.cache_clear()
    init_control_db()
    with get_control_db() as database:
        with pytest.raises(sqlite3.IntegrityError):
            database.execute(
                "INSERT INTO managed_operational_failures VALUES (?, ?, ?)",
                (
                    "private-provider-error",
                    "2026-08-13T10:00:00Z",
                    "2026-08-13T10:00:00Z",
                ),
            )
