"""Deletion API tests for managed backup coordination."""

from __future__ import annotations

from unittest.mock import Mock

from fastapi.testclient import TestClient


def test_managed_backup_delete_returns_safe_accepted_job(auth_client: TestClient) -> None:
    """Delete mutation should pass only tenant and archive identity to manager."""
    tenant = getattr(auth_client, "yinshi_tenant")
    job = {
        "id": "job-1",
        "archive_id": "archive-1",
        "operation": "delete",
        "status": "running",
        "phase": "claimed",
        "started_at": "2026-08-12T12:00:00Z",
        "updated_at": "2026-08-12T12:00:00Z",
        "last_error": None,
    }
    manager = Mock()
    manager.enqueue_delete = Mock(return_value=job)
    auth_client.app.state.managed_backup_manager = manager

    response = auth_client.delete("/api/runtime/backups/archive-1")

    assert response.status_code == 202
    assert response.json() == job
    manager.enqueue_delete.assert_called_once_with(tenant.user_id, "archive-1")
    manager.wake.assert_called_once_with()
