"""Staging source-loss drill harness validates inputs and sanitizes output."""

from __future__ import annotations

import json

import pytest

from yinshi.managed_source_loss_recovery import (
    DrillConfigurationError,
    ManagedSourceLossDrill,
    ManagedSourceLossReceipt,
    configuration_check_main,
    load_drill_configuration,
    sanitized_configuration_status,
)


class FakeDrillBoundary:
    """Return a complete fake drill receipt after checking typed capabilities."""

    def run(self, configuration: object) -> ManagedSourceLossReceipt:
        control = configuration.control
        provider = configuration.provider
        storage = configuration.storage
        assert control.url == "https://staging.example.com"
        assert control.operator_token == "operator-secret"
        assert provider.api_token == "sprites-secret"
        assert provider.name_key == "name-secret"
        assert storage.bucket == "staging-backups"
        assert storage.endpoint_url == "https://objects.example.com"
        return ManagedSourceLossReceipt(
            archive_version_count=1,
            cleanup_verified=True,
            data_verified=True,
            multipart_upload_count=0,
            replacement_authority_verified=True,
        )


def test_drill_emits_only_sanitized_verification_results() -> None:
    """Drill output should disclose outcomes but no credentials or resource identities."""
    environment = {
        "STAGING_CONTROL_URL": "https://staging.example.com",
        "STAGING_OPERATOR_TOKEN": "operator-secret",
        "STAGING_SPRITES_API_TOKEN": "sprites-secret",
        "STAGING_SPRITES_NAME_KEY": "name-secret",
        "STAGING_BACKUP_BUCKET": "staging-backups",
        "STAGING_BACKUP_ENDPOINT_URL": "https://objects.example.com",
        "STAGING_BACKUP_REGION": "test-region",
        "STAGING_BACKUP_ACCESS_KEY_ID": "access-secret",
        "STAGING_BACKUP_SECRET_ACCESS_KEY": "storage-secret",
        "STAGING_BACKUP_ENCRYPTION_KEY": "ab" * 32,
    }
    configuration = load_drill_configuration(environment)

    result = ManagedSourceLossDrill(FakeDrillBoundary()).run(
        configuration,
        commit_sha="1" * 40,
    )

    payload = result.to_dict()
    serialized = json.dumps(payload, sort_keys=True)
    assert payload["status"] == "passed"
    assert payload["checks"] == {
        "archive_version_count": 1,
        "cleanup_verified": True,
        "data_verified": True,
        "multipart_upload_count": 0,
        "replacement_authority_verified": True,
    }
    for sensitive_value in environment.values():
        assert sensitive_value not in serialized
    assert "yinshi-secret-sprite" not in serialized
    assert "/var/lib/yinshi" not in serialized


def test_drill_rejects_boolean_count_fields() -> None:
    """Count checks must be integers rather than Boolean look-alikes."""
    environment = {
        "STAGING_CONTROL_URL": "https://staging.example.com",
        "STAGING_OPERATOR_TOKEN": "operator-secret",
        "STAGING_SPRITES_API_TOKEN": "sprites-secret",
        "STAGING_SPRITES_NAME_KEY": "name-secret",
        "STAGING_BACKUP_BUCKET": "staging-backups",
        "STAGING_BACKUP_ENDPOINT_URL": "https://objects.example.com",
        "STAGING_BACKUP_REGION": "test-region",
        "STAGING_BACKUP_ACCESS_KEY_ID": "access-secret",
        "STAGING_BACKUP_SECRET_ACCESS_KEY": "storage-secret",
        "STAGING_BACKUP_ENCRYPTION_KEY": "ab" * 32,
    }

    class InvalidCountBoundary:
        def run(self, configuration: object) -> ManagedSourceLossReceipt:
            return ManagedSourceLossReceipt(
                archive_version_count=True,
                cleanup_verified=True,
                data_verified=True,
                multipart_upload_count=False,
                replacement_authority_verified=True,
            )

    with pytest.raises(ValueError, match="archive_version_count"):
        ManagedSourceLossDrill(InvalidCountBoundary()).run(
            load_drill_configuration(environment),
            commit_sha="1" * 40,
        )


def test_drill_configuration_rejects_missing_external_credentials() -> None:
    """Staging drill must fail closed before contacting provider boundaries."""
    environment = {
        "STAGING_CONTROL_URL": "https://staging.example.com",
        "STAGING_OPERATOR_TOKEN": "operator-secret",
    }

    with pytest.raises(DrillConfigurationError, match="STAGING_SPRITES_API_TOKEN"):
        load_drill_configuration(environment)


def test_configuration_status_contains_no_secret_values() -> None:
    """Workflow preflight should emit a safe pending-live-execution receipt."""
    environment = {
        "STAGING_CONTROL_URL": "https://staging.example.com",
        "STAGING_OPERATOR_TOKEN": "operator-secret",
        "STAGING_SPRITES_API_TOKEN": "sprites-secret",
        "STAGING_SPRITES_NAME_KEY": "name-secret",
        "STAGING_BACKUP_BUCKET": "staging-backups",
        "STAGING_BACKUP_ENDPOINT_URL": "https://objects.example.com",
        "STAGING_BACKUP_REGION": "test-region",
        "STAGING_BACKUP_ACCESS_KEY_ID": "access-secret",
        "STAGING_BACKUP_SECRET_ACCESS_KEY": "storage-secret",
        "STAGING_BACKUP_ENCRYPTION_KEY": "ab" * 32,
    }

    payload = sanitized_configuration_status(load_drill_configuration(environment))

    serialized = json.dumps(payload, sort_keys=True)
    assert payload == {
        "schema_version": 1,
        "status": "pending_live_staging_integration",
        "required_settings_configured": True,
        "live_staging_execution": False,
    }
    for sensitive_value in environment.values():
        assert sensitive_value not in serialized


def test_configuration_check_command_emits_pending_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Scheduled workflow should validate secrets without claiming a live drill."""
    environment = {
        "STAGING_CONTROL_URL": "https://staging.example.com",
        "STAGING_OPERATOR_TOKEN": "operator-secret",
        "STAGING_SPRITES_API_TOKEN": "sprites-secret",
        "STAGING_SPRITES_NAME_KEY": "name-secret",
        "STAGING_BACKUP_BUCKET": "staging-backups",
        "STAGING_BACKUP_ENDPOINT_URL": "https://objects.example.com",
        "STAGING_BACKUP_REGION": "test-region",
        "STAGING_BACKUP_ACCESS_KEY_ID": "access-secret",
        "STAGING_BACKUP_SECRET_ACCESS_KEY": "storage-secret",
        "STAGING_BACKUP_ENCRYPTION_KEY": "ab" * 32,
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    exit_code = configuration_check_main()

    captured = capsys.readouterr()
    assert exit_code == 3
    assert json.loads(captured.out)["status"] == "pending_live_staging_integration"
    assert captured.err == ""
    for sensitive_value in environment.values():
        assert sensitive_value not in captured.out
