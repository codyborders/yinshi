"""Managed backup provider profiles fail closed and preserve local defaults."""

from __future__ import annotations

import pytest


def test_managed_backup_provider_defaults_to_aws_s3() -> None:
    """Existing deployments should retain strict AWS-compatible preflight."""
    from yinshi.config import Settings

    settings = Settings(_env_file=None)

    assert settings.managed_backup_provider == "aws_s3"


def test_managed_backup_provider_rejects_unknown_profile() -> None:
    """Unknown storage behavior must fail before hosted startup."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(_env_file=None, managed_backup_provider="unknown")

    with pytest.raises(RuntimeError, match="MANAGED_BACKUP_PROVIDER"):
        _validate_settings(settings)


@pytest.mark.asyncio
async def test_spaces_profile_builds_versioned_object_encryption_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spaces should skip only its unsupported bucket-default encryption query."""
    from yinshi.config import Settings
    from yinshi.services.managed_backup_store import create_managed_backup_store

    class Client:
        def get_bucket_versioning(self, **_request):
            return {"Status": "Enabled"}

        def get_bucket_encryption(self, **_request):
            raise AssertionError("Spaces does not implement bucket encryption settings")

    monkeypatch.setattr("boto3.client", lambda *_args, **_values: Client())
    settings = Settings(
        _env_file=None,
        managed_backup_provider="digitalocean_spaces",
        managed_backup_bucket="backup-bucket",
        managed_backup_endpoint_url="https://sfo3.digitaloceanspaces.com",
        managed_backup_region="sfo3",
    )

    store = create_managed_backup_store(settings)

    await store.preflight()


def test_spaces_profile_rejects_noncanonical_endpoint() -> None:
    """Spaces credentials must target one explicit regional service endpoint."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        managed_backup_provider="digitalocean_spaces",
        managed_backup_endpoint_url="https://storage.example.com",
        managed_backup_region="sfo3",
        disable_auth=True,
        container_enabled=False,
    )

    with pytest.raises(RuntimeError, match="DigitalOcean Spaces regional endpoint"):
        _validate_settings(settings)
