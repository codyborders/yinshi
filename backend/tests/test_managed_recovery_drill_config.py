"""Recovery drill configuration fails closed outside isolated staging."""

from __future__ import annotations

import pytest


def test_recovery_drill_cannot_be_enabled_in_production() -> None:
    """Production configuration must reject destructive drill controls."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=True,
        container_enabled=False,
        managed_recovery_drill_enabled=True,
        deployment_environment="production",
        managed_recovery_operator_token_hash="a" * 64,
    )

    with pytest.raises(
        RuntimeError,
        match="MANAGED_RECOVERY_DRILL_ENABLED requires DEPLOYMENT_ENVIRONMENT=staging",
    ):
        _validate_settings(settings)
