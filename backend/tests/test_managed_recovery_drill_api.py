"""Staging recovery control routes remain unavailable outside explicit drill mode."""

from __future__ import annotations


def test_recovery_drill_route_is_absent_by_default(monkeypatch, tmp_path) -> None:
    """Normal hosted deployments must not expose operator drill controls."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("TRUSTED_HOSTS", "localhost,127.0.0.1,testserver")

    import yinshi.main as main
    from yinshi.config import Settings

    settings = Settings(_env_file=None, disable_auth=True, container_enabled=False)
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    application = main.create_app(mode="hosted")

    assert "/internal/managed-recovery/drills" not in application.openapi()["paths"]
