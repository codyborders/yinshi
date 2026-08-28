"""Revocation must win over concurrent runner registration and heartbeat writes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

_RUNNER_NOISE_PUBLIC_KEY = "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I"


def _register_byoc_runner(user_id: str) -> dict:
    """Create and consume one BYOC registration, returning both tokens."""
    from yinshi.services.runners import create_runner_registration, register_runner

    registration = create_runner_registration(
        user_id,
        name="Heartbeat race runner",
        cloud_provider="aws",
        region="us-west-2",
        storage_profile="aws_ebs_s3_files",
        control_url="http://testserver",
    )
    return register_runner(
        registration["registration_token"],
        runner_version="0.1.0",
        capabilities={},
        data_dir="/var/lib/yinshi",
        sqlite_dir="/var/lib/yinshi/sqlite",
        shared_files_dir="/mnt/yinshi-s3-files",
        storage_profile="aws_ebs_s3_files",
        noise_public_key=_RUNNER_NOISE_PUBLIC_KEY,
    )


def test_heartbeat_does_not_revive_a_concurrently_revoked_runner(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revoke that commits mid-heartbeat must win over the stale write."""
    from yinshi.exceptions import RunnerAuthenticationError
    from yinshi.services import runners as runners_service
    from yinshi.services.runners import get_runner_for_user, revoke_runner_for_user

    tenant = getattr(auth_client, "yinshi_tenant")
    runner_token = _register_byoc_runner(tenant.user_id)["runner_token"]
    registered_at = get_runner_for_user(tenant.user_id)["registered_at"]

    real_profile_match = runners_service._requested_profile_matches

    def revoke_then_match(**kwargs):
        revoke_runner_for_user(tenant.user_id)
        return real_profile_match(**kwargs)

    monkeypatch.setattr(runners_service, "_requested_profile_matches", revoke_then_match)

    with pytest.raises(RunnerAuthenticationError):
        runners_service.record_runner_heartbeat(
            runner_token,
            runner_version="0.2.0",
            capabilities={},
            data_dir="/var/lib/yinshi",
            sqlite_dir="/var/lib/yinshi/sqlite",
            shared_files_dir="/mnt/yinshi-s3-files",
            storage_profile="aws_ebs_s3_files",
        )

    runner = get_runner_for_user(tenant.user_id)
    assert runner is not None
    assert runner["status"] == "revoked"
    assert runner["runner_version"] == "0.1.0"
    assert runner["registered_at"] == registered_at
    assert runner["last_heartbeat_at"] == registered_at
