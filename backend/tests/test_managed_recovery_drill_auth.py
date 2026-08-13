"""Recovery operator routes authenticate a dedicated staging bearer token."""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient


def test_recovery_drill_rejects_wrong_operator_token(monkeypatch, tmp_path) -> None:
    """A wrong dedicated bearer token must not reach drill coordination."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")

    import yinshi.main as main
    from yinshi.config import Settings

    settings = Settings(
        _env_file=None,
        disable_auth=True,
        container_enabled=False,
        managed_runtime_provider="fly_sprites",
        managed_recovery_drill_enabled=True,
        deployment_environment="staging",
        managed_recovery_operator_token_hash=hashlib.sha256(b"operator-secret").hexdigest(),
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    application = main.create_app(mode="hosted")

    response = TestClient(application, base_url="http://localhost").post(
        "/internal/managed-recovery/drills",
        headers={"Authorization": "Bearer wrong-secret"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid operator token"}
