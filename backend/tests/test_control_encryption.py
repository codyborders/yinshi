"""Tests for control-plane field encryption trust boundaries."""

from __future__ import annotations

import pytest


@pytest.fixture
def control_field_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Set up an environment with control-field encryption enabled."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("CONTROL_FIELD_ENCRYPTION", "enabled")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_encrypt_control_text_encrypts_plaintext_with_envelope_prefix(control_field_env):
    """Plaintext that merely starts with enc:v1: must not bypass encryption."""
    from yinshi.services.control_encryption import decrypt_control_text, encrypt_control_text

    plaintext = "enc:v1:not-an-envelope"
    stored = encrypt_control_text("pi_configs.source_label", "user-1", plaintext)

    assert stored is not None
    assert stored != plaintext
    assert decrypt_control_text("pi_configs.source_label", "user-1", stored) == plaintext


def test_decrypt_control_text_passes_through_malformed_stored_envelopes(control_field_env):
    """Malformed stored envelope payloads must read as legacy plaintext."""
    from yinshi.services.control_encryption import decrypt_control_text

    for malformed in ("enc:v1:AAAA", "enc:v1:not-an-envelope", "enc:v1:"):
        value = decrypt_control_text("pi_configs.source_label", "user-1", malformed)
        assert value == malformed
