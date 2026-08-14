"""Public managed operational health is sanitized and fail closed."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.conftest import _configure_test_env


def test_managed_health_contract_and_process_health_remains_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed health maps control findings and read errors to sanitized status."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    import yinshi.db as database_module
    import yinshi.main as main
    from yinshi.config import get_settings
    from yinshi.services.managed_operational_failures import (
        ManagedPersistentAlertClass,
        record_managed_operational_failure,
    )

    get_settings.cache_clear()
    application = main.create_app(mode="hosted")
    with TestClient(application) as client:
        response = client.get("/health/managed")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

        record_managed_operational_failure(ManagedPersistentAlertClass.SPRITE_RECONCILIATION_FAILED)
        response = client.get("/health/managed")
        assert response.status_code == 503
        assert response.json() == {"status": "critical"}
        assert response.content == b'{"status":"critical"}'

        @contextmanager
        def unavailable_database() -> Iterator[object]:
            raise RuntimeError("private database path and provider details")
            yield object()

        monkeypatch.setattr(database_module, "get_control_db", unavailable_database)
        response = client.get("/health/managed")
        assert response.status_code == 503
        assert response.content == b'{"status":"critical"}'
        assert b"private" not in response.content

        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
