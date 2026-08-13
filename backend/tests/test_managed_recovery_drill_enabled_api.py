"""Explicit staging settings expose the recovery operator boundary."""

from __future__ import annotations


def test_recovery_drill_route_requires_explicit_staging_mode(monkeypatch, tmp_path) -> None:
    """Explicit staging drill settings should expose only the operator boundary."""
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
        managed_recovery_operator_token_hash="a" * 64,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    application = main.create_app(mode="hosted")

    assert "/internal/managed-recovery/drills" in application.openapi()["paths"]
    assert application.state.sprites_public_launch_enabled is False
