"""Tests compact desktop token expiry and type separation through public helpers."""

from __future__ import annotations

import pytest

from tests.conftest import _configure_test_env
from yinshi.services.desktop_tokens import create_desktop_token, verify_desktop_access_token


@pytest.fixture(autouse=True)
def token_settings(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Use an isolated signing secret and clear cached settings after each test."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    yield
    from yinshi.config import get_settings

    get_settings.cache_clear()


def test_verify_desktop_access_token_enforces_expiry_and_access_type() -> None:
    """Verifier should accept active access claims but reject expiry and lease substitution."""
    access_token = create_desktop_token(
        token_type="access",
        user_id="user-id",
        device_id="device-id",
        issued_at=100,
        expires_at=200,
    )
    lease_token = create_desktop_token(
        token_type="lease",
        user_id="user-id",
        device_id="device-id",
        issued_at=100,
        expires_at=200,
    )

    identity = verify_desktop_access_token(access_token, current_time=150)
    assert identity is not None
    assert identity.user_id == "user-id"
    assert identity.device_id == "device-id"
    assert verify_desktop_access_token(access_token, current_time=200) is None
    assert verify_desktop_access_token(lease_token, current_time=150) is None
