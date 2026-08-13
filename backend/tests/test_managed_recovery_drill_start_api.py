"""Recovery operator API starts one bounded drill through its application owner."""

from __future__ import annotations

import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from yinshi.api import managed_recovery_drills


class FakeDrillController:
    """Return one sanitized accepted state."""

    async def start(self, *, commit_sha: str) -> dict[str, object]:
        assert commit_sha == "1" * 40
        return {"schema_version": 1, "status": "running"}


def test_valid_operator_can_start_drill() -> None:
    """The route should authenticate and delegate one validated commit."""
    token = "operator-secret"
    application = FastAPI()
    application.state.managed_recovery_operator_token_hash = hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()
    application.state.managed_recovery_drill_controller = FakeDrillController()
    application.include_router(managed_recovery_drills.router)

    response = TestClient(application).post(
        "/internal/managed-recovery/drills",
        headers={"Authorization": f"Bearer {token}"},
        json={"commit_sha": "1" * 40},
    )

    assert response.status_code == 202
    assert response.json() == {"schema_version": 1, "status": "running"}
