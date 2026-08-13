"""Typed staging harness for destructive managed source-loss drills."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

_REQUIRED_ENVIRONMENT_NAMES = (
    "STAGING_CONTROL_URL",
    "STAGING_OPERATOR_TOKEN",
    "STAGING_SPRITES_API_TOKEN",
    "STAGING_BACKUP_BUCKET",
    "STAGING_BACKUP_ENDPOINT_URL",
    "STAGING_BACKUP_REGION",
    "STAGING_BACKUP_ACCESS_KEY_ID",
    "STAGING_BACKUP_SECRET_ACCESS_KEY",
    "STAGING_BACKUP_ENCRYPTION_KEY",
)


class DrillConfigurationError(RuntimeError):
    """Raised when a staging drill cannot safely start."""


_COUNT_CHECK_NAMES = ("archive_version_count", "multipart_upload_count")
_BOOLEAN_CHECK_NAMES = (
    "cleanup_verified",
    "data_verified",
    "replacement_authority_verified",
)
_CHECK_NAMES = _COUNT_CHECK_NAMES + _BOOLEAN_CHECK_NAMES


@dataclass(frozen=True, slots=True)
class ManagedSourceLossConfiguration:
    """Staging settings kept outside drill output."""

    values: dict[str, str]


class ManagedSourceLossBoundary(Protocol):
    """Run provider-specific destructive drill actions."""

    def run(self, *, control_url: str, bucket: str) -> dict[str, object]:
        """Return required verification fields after cleanup."""
        ...


@dataclass(frozen=True, slots=True)
class ManagedSourceLossResult:
    """Sanitized drill result suitable for a retained CI artifact."""

    commit_sha: str
    started_at: str
    checks: dict[str, bool | int]

    def to_dict(self) -> dict[str, Any]:
        """Return only allow-listed, non-sensitive drill fields."""
        passed = (
            self.checks["archive_version_count"] == 1
            and self.checks["multipart_upload_count"] == 0
            and self.checks["cleanup_verified"] is True
            and self.checks["data_verified"] is True
            and self.checks["replacement_authority_verified"] is True
        )
        return {
            "schema_version": 1,
            "status": "passed" if passed else "failed",
            "commit_sha": self.commit_sha,
            "started_at": self.started_at,
            "checks": dict(self.checks),
        }


def load_drill_configuration(
    environment: Mapping[str, str],
) -> ManagedSourceLossConfiguration:
    """Copy required staging settings for boundary use."""
    values: dict[str, str] = {}
    for name in _REQUIRED_ENVIRONMENT_NAMES:
        value = environment.get(name, "").strip()
        if not value:
            raise DrillConfigurationError(f"missing required staging setting: {name}")
        values[name] = value
    return ManagedSourceLossConfiguration(values=values)


def sanitized_configuration_status(
    configuration: ManagedSourceLossConfiguration,
) -> dict[str, bool | int | str]:
    """Describe workflow readiness without exposing configured values."""
    if len(configuration.values) != len(_REQUIRED_ENVIRONMENT_NAMES):
        raise ValueError("configuration is incomplete")
    return {
        "schema_version": 1,
        "status": "pending_live_staging_integration",
        "required_settings_configured": True,
        "live_staging_execution": False,
    }


def configuration_check_main() -> int:
    """Validate workflow settings and report that live work remains pending."""
    configuration = load_drill_configuration(os.environ)
    payload = sanitized_configuration_status(configuration)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 3


class ManagedSourceLossDrill:
    """Emit only allow-listed recovery results from a drill boundary."""

    def __init__(self, boundary: ManagedSourceLossBoundary) -> None:
        self._boundary = boundary

    def run(
        self,
        configuration: ManagedSourceLossConfiguration,
        *,
        commit_sha: str,
    ) -> ManagedSourceLossResult:
        """Run staging boundary and retain only aggregate checks."""
        receipt = self._boundary.run(
            control_url=configuration.values["STAGING_CONTROL_URL"],
            bucket=configuration.values["STAGING_BACKUP_BUCKET"],
        )
        checks: dict[str, bool | int] = {}
        for name in _CHECK_NAMES:
            value = receipt[name]
            if name in _COUNT_CHECK_NAMES:
                if type(value) is not int or value < 0:
                    raise RuntimeError(f"drill boundary returned invalid check: {name}")
            elif type(value) is not bool:
                raise RuntimeError(f"drill boundary returned invalid check: {name}")
            checks[name] = value
        return ManagedSourceLossResult(
            commit_sha=commit_sha,
            started_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            checks=checks,
        )
