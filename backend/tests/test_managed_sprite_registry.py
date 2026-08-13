"""Managed Sprite registry records deployment ownership before provider work."""

from __future__ import annotations

from datetime import datetime, timezone


def test_registry_persists_and_removes_one_owned_identity(tmp_path, monkeypatch) -> None:
    """Registry should retain uncertain creation until confirmed provider absence."""
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "disabled")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")

    from yinshi.config import get_settings
    from yinshi.db import get_control_db, init_control_db
    from yinshi.services.managed_sprite_registry import (
        list_managed_sprite_identities,
        register_managed_sprite_identity,
        remove_managed_sprite_identity,
    )

    get_settings.cache_clear()
    init_control_db()
    with get_control_db() as database:
        database.execute(
            "INSERT INTO users (id, email, display_name) VALUES (?, ?, ?)",
            ("user-1", "user@example.com", "User"),
        )
        database.commit()

    register_managed_sprite_identity(
        sprite_name="yinshi-owned",
        identity_kind="runtime",
        user_id="user-1",
        job_id=None,
        lifecycle_status="creating",
        now=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )

    identities = list_managed_sprite_identities()
    assert [(identity.sprite_name, identity.lifecycle_status) for identity in identities] == [
        ("yinshi-owned", "creating")
    ]
    with get_control_db() as database:
        database.execute("DELETE FROM users WHERE id = ?", ("user-1",))
        database.commit()
    assert [identity.sprite_name for identity in list_managed_sprite_identities()] == [
        "yinshi-owned"
    ]
    assert remove_managed_sprite_identity("yinshi-owned") is True
    assert list_managed_sprite_identities() == ()
    get_settings.cache_clear()
