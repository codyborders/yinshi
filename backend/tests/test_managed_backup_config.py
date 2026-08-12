"""Tests for managed backup configuration."""

from __future__ import annotations

import pytest


def test_managed_backup_settings_have_local_safe_defaults() -> None:
    """Managed backup settings should require explicit hosted configuration."""
    from yinshi.config import Settings

    settings = Settings(_env_file=None)

    assert settings.managed_backup_bucket == ""
    assert settings.managed_backup_endpoint_url == ""
    assert settings.managed_backup_region == ""
    assert settings.managed_backup_access_key_id is None
    assert settings.managed_backup_secret_access_key is None
    assert settings.managed_backup_prefix == "yinshi-managed-v1"
    assert settings.managed_backup_part_bytes == 16 * 1024 * 1024
    assert settings.managed_backup_retention_days == 30


def test_fly_sprites_requires_managed_backup_bucket() -> None:
    """Fly mode should fail closed without independent backup storage."""
    from pathlib import Path

    import pytest

    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=False,
        container_enabled=False,
        google_client_id="test-client-id",
        google_client_secret="test-client-secret",
        secret_key="test-session-secret-0123456789abcdef",
        key_encryption_key="b" * 64,
        backup_encryption_key="c" * 64,
        control_field_encryption="required",
        require_https="required",
        managed_runtime_provider="fly_sprites",
        sprites_api_token="test-token",
        sprites_name_key="n" * 32,
        sprites_artifact_url="https://artifacts.example.com/runner.pyz",
        sprites_artifact_sha256="a" * 64,
        sprites_allowed_domains="control.example.com",
        sprites_public_control_url="https://control.example.com",
        sprites_bootstrap_script_path=str(Path(__file__).resolve()),
    )

    with pytest.raises(RuntimeError, match="MANAGED_BACKUP_BUCKET"):
        _validate_settings(settings)


def test_fly_sprites_accepts_complete_managed_backup_configuration() -> None:
    """Fly mode should accept complete independent encrypted backup settings."""
    from pathlib import Path

    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=False,
        container_enabled=False,
        google_client_id="test-client-id",
        google_client_secret="test-client-secret",
        secret_key="test-session-secret-0123456789abcdef",
        key_encryption_key="b" * 64,
        backup_encryption_key="c" * 64,
        control_field_encryption="required",
        require_https="required",
        managed_runtime_provider="fly_sprites",
        sprites_api_token="test-token",
        sprites_name_key="n" * 32,
        sprites_artifact_url="https://artifacts.example.com/runner.pyz",
        sprites_artifact_sha256="a" * 64,
        sprites_allowed_domains="control.example.com",
        sprites_public_control_url="https://control.example.com",
        sprites_bootstrap_script_path=str(Path(__file__).resolve()),
        managed_backup_bucket="backup-bucket",
        managed_backup_endpoint_url="https://storage.example.com",
        managed_backup_region="us-east-1",
        managed_backup_access_key_id="backup-access-key",
        managed_backup_secret_access_key="backup-secret-key",
    )

    _validate_settings(settings)


def test_s3_backup_store_factory_supports_default_credential_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted storage may use an instance role without explicit static keys."""
    from yinshi.config import Settings
    from yinshi.services.managed_backup_store import create_managed_backup_store

    captured: dict[str, object] = {}

    def client(service_name: str, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("boto3.client", client)
    settings = Settings(
        managed_backup_bucket="backup-bucket",
        managed_backup_endpoint_url="https://objects.example",
        managed_backup_region="us-east-1",
    )

    create_managed_backup_store(settings)

    assert "aws_access_key_id" not in captured
    assert "aws_secret_access_key" not in captured


def test_s3_backup_store_factory_uses_bounded_https_client_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backup store construction should pin HTTPS, retries, and S3 addressing."""
    from pydantic import SecretStr

    from yinshi.config import Settings
    from yinshi.services import managed_backup_store

    create_managed_backup_store = getattr(
        managed_backup_store,
        "create_managed_backup_store",
        None,
    )
    assert callable(create_managed_backup_store)
    captured: dict[str, object] = {}

    def client(service_name: str, **kwargs):
        captured["service_name"] = service_name
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("boto3.client", client)
    settings = Settings(
        managed_backup_bucket="backup-bucket",
        managed_backup_endpoint_url="https://objects.example",
        managed_backup_region="us-east-1",
        managed_backup_access_key_id=SecretStr("access"),
        managed_backup_secret_access_key=SecretStr("secret"),
    )

    store = create_managed_backup_store(settings)

    assert store is not None
    assert captured["service_name"] == "s3"
    assert captured["endpoint_url"] == "https://objects.example"
    assert captured["region_name"] == "us-east-1"
    config = captured["config"]
    assert getattr(config, "retries") == {"max_attempts": 3, "mode": "standard"}
    assert getattr(config, "s3") == {"addressing_style": "path"}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("managed_backup_region", "", "MANAGED_BACKUP_REGION"),
        ("managed_backup_prefix", "../unsafe", "MANAGED_BACKUP_PREFIX"),
        ("managed_backup_part_bytes", 1024, "MANAGED_BACKUP_PART_BYTES"),
        ("managed_backup_part_bytes", 5 * 1024**3 + 1, "MANAGED_BACKUP_PART_BYTES"),
        ("managed_backup_retention_days", 0, "MANAGED_BACKUP_RETENTION_DAYS"),
        ("managed_backup_secret_access_key", None, "MANAGED_BACKUP credentials"),
    ],
)
def test_fly_sprites_rejects_invalid_managed_backup_values(
    field: str,
    value: object,
    message: str,
) -> None:
    """Fly mode should validate every managed backup storage boundary."""
    from pathlib import Path

    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=False,
        container_enabled=False,
        google_client_id="test-client-id",
        google_client_secret="test-client-secret",
        secret_key="test-session-secret-0123456789abcdef",
        key_encryption_key="b" * 64,
        backup_encryption_key="c" * 64,
        control_field_encryption="required",
        require_https="required",
        managed_runtime_provider="fly_sprites",
        sprites_api_token="test-token",
        sprites_name_key="n" * 32,
        sprites_artifact_url="https://artifacts.example.com/runner.pyz",
        sprites_artifact_sha256="a" * 64,
        sprites_allowed_domains="control.example.com",
        sprites_public_control_url="https://control.example.com",
        sprites_bootstrap_script_path=str(Path(__file__).resolve()),
        managed_backup_bucket="backup-bucket",
        managed_backup_endpoint_url="https://storage.example.com",
        managed_backup_region="us-east-1",
        managed_backup_access_key_id="backup-access-key",
        managed_backup_secret_access_key="backup-secret-key",
    )
    setattr(settings, field, value)

    with pytest.raises(RuntimeError, match=message):
        _validate_settings(settings)


def test_fly_sprites_rejects_insecure_managed_backup_endpoint() -> None:
    """Fly mode should reject plaintext object-store endpoints."""
    from pathlib import Path

    import pytest

    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=False,
        container_enabled=False,
        google_client_id="test-client-id",
        google_client_secret="test-client-secret",
        secret_key="test-session-secret-0123456789abcdef",
        key_encryption_key="b" * 64,
        backup_encryption_key="c" * 64,
        control_field_encryption="required",
        require_https="required",
        managed_runtime_provider="fly_sprites",
        sprites_api_token="test-token",
        sprites_name_key="n" * 32,
        sprites_artifact_url="https://artifacts.example.com/runner.pyz",
        sprites_artifact_sha256="a" * 64,
        sprites_allowed_domains="control.example.com",
        sprites_public_control_url="https://control.example.com",
        sprites_bootstrap_script_path=str(Path(__file__).resolve()),
        managed_backup_bucket="backup-bucket",
        managed_backup_endpoint_url="http://storage.example.com",
        managed_backup_region="us-east-1",
        managed_backup_access_key_id="backup-access-key",
        managed_backup_secret_access_key="backup-secret-key",
    )

    with pytest.raises(RuntimeError, match="MANAGED_BACKUP_ENDPOINT_URL"):
        _validate_settings(settings)
