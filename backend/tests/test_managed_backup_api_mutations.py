"""Mutation API tests for managed backup restore and deletion."""

from __future__ import annotations

from unittest.mock import Mock

from fastapi.testclient import TestClient


def _safe_job(operation: str) -> dict[str, object]:
    return {
        "id": "job-1",
        "archive_id": "archive-1",
        "operation": operation,
        "status": "running",
        "phase": "claimed",
        "started_at": "2026-08-12T12:00:00Z",
        "updated_at": "2026-08-12T12:00:00Z",
        "last_error": None,
    }


def test_managed_backup_conflict_returns_safe_409(auth_client: TestClient) -> None:
    """Durable catalog conflicts must not escape as internal server failures."""
    from yinshi.services.managed_backups import ManagedBackupConflictError

    manager = Mock()
    manager.enqueue_restore = Mock(side_effect=ManagedBackupConflictError("operation is active"))
    auth_client.app.state.managed_backup_manager = manager

    response = auth_client.post("/api/runtime/backups/archive-1/restore")

    assert response.status_code == 409
    assert response.json() == {"detail": "Managed backup state is invalid"}


def test_managed_backup_restore_returns_safe_accepted_job(auth_client: TestClient) -> None:
    """Restore mutation should pass only tenant and archive identity to manager."""
    tenant = getattr(auth_client, "yinshi_tenant")
    manager = Mock()
    manager.enqueue_restore = Mock(return_value=_safe_job("restore"))
    auth_client.app.state.managed_backup_manager = manager

    response = auth_client.post("/api/runtime/backups/archive-1/restore")

    assert response.status_code == 202
    assert response.json() == manager.enqueue_restore.return_value
    manager.enqueue_restore.assert_called_once_with(tenant.user_id, "archive-1")
    manager.wake.assert_called_once_with()
