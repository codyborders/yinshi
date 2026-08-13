"""Hosted authentication preserves the dedicated recovery operator boundary."""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient
from pydantic import SecretStr


def test_hosted_auth_allows_valid_recovery_operator_token(monkeypatch, tmp_path) -> None:
    """A valid operator token must bypass tenant-token resolution for this route."""
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "staging-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "staging-secret")

    import yinshi.auth as auth
    import yinshi.main as main
    from yinshi.config import Settings

    token = "operator-secret"
    settings = Settings(
        _env_file=None,
        disable_auth=False,
        container_enabled=False,
        google_client_id="staging-client",
        google_client_secret="staging-secret",
        managed_runtime_provider="fly_sprites",
        managed_recovery_drill_enabled=True,
        deployment_environment="staging",
        managed_recovery_operator_token_hash=hashlib.sha256(token.encode()).hexdigest(),
        sprites_api_token=SecretStr("sprites-token"),
        sprites_name_key=SecretStr("n" * 32),
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    application = main.create_app(mode="hosted")

    response = TestClient(application, base_url="http://localhost").post(
        "/internal/managed-recovery/drills",
        headers={"Authorization": f"Bearer {token}"},
        json={"commit_sha": "1" * 40},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Managed recovery drill is unavailable"}
