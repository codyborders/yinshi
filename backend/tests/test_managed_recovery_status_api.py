"""Recovery operator API exposes sanitized latest drill status."""

from __future__ import annotations

import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from yinshi.api import managed_recovery_drills


class FakeController:
    """Return one terminal aggregate state."""

    def status(self) -> dict[str, object]:
        return {"schema_version": 1, "status": "passed"}


def test_operator_can_poll_latest_status() -> None:
    """A valid operator token should return sanitized status."""
    token = "operator-secret"
    application = FastAPI()
    application.state.managed_recovery_operator_token_hash = hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()
    application.state.managed_recovery_drill_controller = FakeController()
    application.include_router(managed_recovery_drills.router)

    response = TestClient(application).get(
        "/internal/managed-recovery/drills/latest",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"schema_version": 1, "status": "passed"}
