"""Exercise cloud runner registration, heartbeat, revocation, and status APIs.

The tests cover the control-plane lifecycle without launching cloud resources: a
user creates a one-time token, a runner consumes it, the runner heartbeats with a
bearer token, and revocation invalidates that bearer token.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from yinshi import runner_agent

_RUNNER_NOISE_PUBLIC_KEY = "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I"
_DIFFERENT_RUNNER_NOISE_PUBLIC_KEY = "zo060cy2M-x7cMF4FKXHbs0CloUFDTRHRboFhw5YfVk"


def test_cloud_runner_registration_and_heartbeat(auth_client: TestClient) -> None:
    """A user can create a token, register a runner, and see it online."""
    create_response = auth_client.post(
        "/api/settings/runner",
        headers={"X-Forwarded-Host": "attacker.example"},
        json={"name": "AWS prod runner", "cloud_provider": "aws", "region": "us-west-2"},
    )
    assert create_response.status_code == 201
    create_payload = create_response.json()
    assert create_payload["runner"]["status"] == "pending"
    assert create_payload["runner"]["name"] == "AWS prod runner"
    assert create_payload["environment"]["YINSHI_CONTROL_URL"] == "http://testserver"
    assert create_payload["runner"]["capabilities"]["storage_profile"] == "aws_ebs_s3_files"
    assert create_payload["environment"]["YINSHI_RUNNER_STORAGE_PROFILE"] == "aws_ebs_s3_files"
    assert create_payload["environment"]["YINSHI_RUNNER_SQLITE_STORAGE"] == "runner_ebs"
    assert (
        create_payload["environment"]["YINSHI_RUNNER_SHARED_FILES_STORAGE"]
        == "s3_files_or_local_posix"
    )
    assert create_payload["environment"]["YINSHI_RUNNER_DATA_DIR"] == "/var/lib/yinshi"
    assert create_payload["environment"]["YINSHI_RUNNER_SQLITE_DIR"] == "/var/lib/yinshi/sqlite"
    assert (
        create_payload["environment"]["YINSHI_RUNNER_DATA_PROTECTION_KEY_FILE"]
        == "/var/lib/yinshi/sqlite/.yinshi-data-protection-key"
    )
    assert create_payload["environment"]["YINSHI_RUNNER_SHARED_FILES_DIR"] == "/mnt/yinshi-s3-files"
    assert create_payload["environment"]["YINSHI_RUNNER_TOKEN_FILE"].endswith("/runner-token")
    assert create_payload["registration_token"]

    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_payload["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {"podman": True, "shared_files_storage": "s3_files_mount"},
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/var/lib/yinshi/sqlite",
            "shared_files_dir": "/mnt/yinshi-s3-files",
            "storage_profile": "aws_ebs_s3_files",
            "noise_public_key": _RUNNER_NOISE_PUBLIC_KEY,
        },
    )
    assert register_response.status_code == 201
    register_payload = register_response.json()
    assert register_payload["status"] == "online"
    assert register_payload["runner_token"]

    reused_token_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_payload["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
        },
    )
    assert reused_token_response.status_code == 401

    heartbeat_response = auth_client.post(
        "/runner/heartbeat",
        headers={"Authorization": f"Bearer {register_payload['runner_token']}"},
        json={
            "runner_version": "0.1.1",
            "capabilities": {
                "podman": True,
                "aws_region": "us-west-2",
                "shared_files_storage": "s3_files_mount",
            },
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/var/lib/yinshi/sqlite",
            "shared_files_dir": "/mnt/yinshi-s3-files",
            "storage_profile": "aws_ebs_s3_files",
        },
    )
    assert heartbeat_response.status_code == 200
    assert heartbeat_response.json()["runner_id"] == register_payload["runner_id"]

    status_response = auth_client.get("/api/settings/runner")
    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["status"] == "online"
    assert status_payload["runner_version"] == "0.1.1"
    assert status_payload["noise_public_key"] == _RUNNER_NOISE_PUBLIC_KEY
    assert status_payload["capabilities"]["sqlite"] is True
    assert status_payload["capabilities"]["storage_profile"] == "aws_ebs_s3_files"
    assert status_payload["capabilities"]["storage_profile_experimental"] is False
    assert status_payload["capabilities"]["sqlite_storage"] == "runner_ebs"
    assert status_payload["capabilities"]["sqlite_dir"] == "/var/lib/yinshi/sqlite"
    assert status_payload["capabilities"]["shared_files_storage"] == "s3_files_mount"
    assert status_payload["capabilities"]["shared_files_dir"] == "/mnt/yinshi-s3-files"
    assert status_payload["capabilities"]["live_sqlite_on_shared_files"] is False
    assert status_payload["capabilities"]["aws_region"] == "us-west-2"


def test_runner_registration_log_excludes_private_values(
    auth_client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Registration logs use a fixed event without request or response identifiers."""
    create_response = auth_client.post(
        "/api/settings/runner",
        json={"name": "Private runner", "cloud_provider": "aws", "region": "us-east-1"},
    )
    assert create_response.status_code == 201
    registration_token = create_response.json()["registration_token"]
    provider_body = "provider-body-private"
    data_dir = "/private/tenant/data"
    sqlite_dir = "/private/tenant/sqlite"
    shared_files_dir = "/private/tenant/shared"
    caplog.clear()
    caplog.set_level(logging.INFO)

    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": registration_token,
            "runner_version": "0.2.0",
            "capabilities": {"provider_body": provider_body},
            "data_dir": data_dir,
            "sqlite_dir": sqlite_dir,
            "shared_files_dir": shared_files_dir,
            "storage_profile": "aws_ebs_s3_files",
            "noise_public_key": _RUNNER_NOISE_PUBLIC_KEY,
        },
    )

    assert register_response.status_code == 201
    response_body = register_response.json()
    tenant = getattr(auth_client, "yinshi_tenant")
    private_values = (
        registration_token,
        response_body["runner_token"],
        response_body["runner_id"],
        tenant.user_id,
        provider_body,
        data_dir,
        sqlite_dir,
        shared_files_dir,
    )
    for record in caplog.records:
        rendered_record = f"{record.getMessage()} {record.args!r}"
        assert all(value not in rendered_record for value in private_values)
    assert "Cloud runner registered" in caplog.text


def test_public_runner_rejects_managed_provider(auth_client: TestClient) -> None:
    """Public BYOC settings reject the managed provider."""
    response = auth_client.post(
        "/api/settings/runner",
        json={
            "name": "Wrong provider",
            "cloud_provider": "fly_sprites",
            "region": "ord",
            "storage_profile": "aws_ebs_s3_files",
        },
    )

    assert response.status_code == 422

    storage_response = auth_client.post(
        "/api/settings/runner",
        json={
            "name": "Wrong storage profile",
            "cloud_provider": "aws",
            "region": "us-east-1",
            "storage_profile": "fly_sprites_posix",
        },
    )
    assert storage_response.status_code == 422


def test_managed_registration_advertises_fly_sprites_posix_profile(
    auth_client: TestClient,
) -> None:
    """Managed registration advertises persistent local POSIX storage defaults."""
    from yinshi.services.runners import create_runner_registration

    tenant = getattr(auth_client, "yinshi_tenant")
    registration = create_runner_registration(
        tenant.user_id,
        name="Hosted Sprite runner",
        cloud_provider="fly_sprites",
        region="ord",
        storage_profile="fly_sprites_posix",
        control_url="https://control.example",
        runner_kind="managed",
    )

    capabilities = registration["runner"]["capabilities"]
    assert capabilities["storage_profile"] == "fly_sprites_posix"
    assert capabilities["storage_profile_experimental"] is False
    assert capabilities["sqlite_storage"] == "local_posix"
    assert capabilities["shared_files_storage"] == "local_posix"
    assert capabilities["sqlite_dir"] == "/var/lib/yinshi/sqlite"
    assert capabilities["shared_files_dir"] == "/var/lib/yinshi/files"
    assert capabilities["live_sqlite_on_shared_files"] is False
    environment = registration["environment"]
    assert environment["YINSHI_RUNNER_STORAGE_PROFILE"] == "fly_sprites_posix"
    assert environment["YINSHI_RUNNER_SQLITE_STORAGE"] == "local_posix"
    assert environment["YINSHI_RUNNER_SHARED_FILES_STORAGE"] == "local_posix"
    assert environment["YINSHI_RUNNER_SQLITE_DIR"] == "/var/lib/yinshi/sqlite"
    assert environment["YINSHI_RUNNER_SHARED_FILES_DIR"] == "/var/lib/yinshi/files"

    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": registration["registration_token"],
            "runner_version": "0.2.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
            "storage_profile": "fly_sprites_posix",
            "noise_public_key": _RUNNER_NOISE_PUBLIC_KEY,
        },
    )
    assert register_response.status_code == 201


def test_managed_registration_confirms_first_noise_identity(
    auth_client: TestClient,
) -> None:
    """First managed registration trusts and confirms its valid Noise identity."""
    from yinshi.services.runners import (
        create_runner_registration,
        get_managed_runner_for_user,
    )

    tenant = getattr(auth_client, "yinshi_tenant")
    registration = create_runner_registration(
        tenant.user_id,
        name="Hosted Sprite runner",
        cloud_provider="fly_sprites",
        region="ord",
        storage_profile="fly_sprites_posix",
        control_url="https://control.example",
        runner_kind="managed",
    )

    response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": registration["registration_token"],
            "runner_version": "0.2.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
            "storage_profile": "fly_sprites_posix",
            "noise_public_key": _RUNNER_NOISE_PUBLIC_KEY,
        },
    )

    assert response.status_code == 201
    managed_runner = get_managed_runner_for_user(tenant.user_id)
    assert managed_runner is not None
    assert managed_runner["noise_public_key"] == _RUNNER_NOISE_PUBLIC_KEY
    assert managed_runner["noise_key_confirmed"] is True


def test_managed_rotation_preserves_and_pins_noise_identity(
    auth_client: TestClient,
) -> None:
    """Managed rotation retains the trusted key and rejects identity changes."""
    from yinshi.services.runners import create_runner_registration

    tenant = getattr(auth_client, "yinshi_tenant")
    registration_arguments = {
        "name": "Hosted Sprite runner",
        "cloud_provider": "fly_sprites",
        "region": "ord",
        "storage_profile": "fly_sprites_posix",
        "control_url": "https://control.example",
        "runner_kind": "managed",
    }
    first = create_runner_registration(tenant.user_id, **registration_arguments)
    first_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": first["registration_token"],
            "runner_version": "0.2.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
            "storage_profile": "fly_sprites_posix",
            "noise_public_key": _RUNNER_NOISE_PUBLIC_KEY,
        },
    )
    assert first_response.status_code == 201

    rotated = create_runner_registration(tenant.user_id, **registration_arguments)
    assert rotated["runner"]["noise_public_key"] == _RUNNER_NOISE_PUBLIC_KEY
    assert rotated["runner"]["noise_key_confirmed"] is True

    changed_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": rotated["registration_token"],
            "runner_version": "0.3.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
            "storage_profile": "fly_sprites_posix",
            "noise_public_key": _DIFFERENT_RUNNER_NOISE_PUBLIC_KEY,
        },
    )
    assert changed_response.status_code == 401
    assert "runner_token" not in changed_response.json()

    current = create_runner_registration(tenant.user_id, **registration_arguments)
    assert current["runner"]["noise_public_key"] == _RUNNER_NOISE_PUBLIC_KEY
    assert current["runner"]["noise_key_confirmed"] is True


def test_fly_sprites_posix_rejects_sqlite_under_shared_files(
    auth_client: TestClient,
) -> None:
    """Fly Sprite profile rejects live SQLite below its shared files directory."""
    from yinshi.services.runners import create_runner_registration

    tenant = getattr(auth_client, "yinshi_tenant")
    registration = create_runner_registration(
        tenant.user_id,
        name="Hosted Sprite runner",
        cloud_provider="fly_sprites",
        region="ord",
        storage_profile="fly_sprites_posix",
        control_url="https://control.example",
        runner_kind="managed",
    )

    response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": registration["registration_token"],
            "runner_version": "0.2.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/var/lib/yinshi/files/sqlite",
            "shared_files_dir": "/var/lib/yinshi/files",
            "storage_profile": "fly_sprites_posix",
            "noise_public_key": _RUNNER_NOISE_PUBLIC_KEY,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "sqlite_dir must not live under shared_files_dir"


def test_managed_and_byoc_registrations_coexist_without_changing_public_settings(
    auth_client: TestClient,
) -> None:
    """Internal managed registration must not replace the public BYOC runner."""
    from yinshi.db import get_control_db
    from yinshi.services.runners import authenticate_runner_token, create_runner_registration

    tenant = getattr(auth_client, "yinshi_tenant")
    managed = create_runner_registration(
        tenant.user_id,
        name="Hosted Sprite runner",
        cloud_provider="fly_sprites",
        region="ord",
        storage_profile="aws_ebs_s3_files",
        control_url="https://control.example",
        runner_kind="managed",
    )
    assert managed["runner"]["kind"] == "managed"
    assert managed["runner"]["cloud_provider"] == "fly_sprites"
    managed_registration = auth_client.post(
        "/runner/register",
        json={
            "registration_token": managed["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
            "storage_profile": "aws_ebs_s3_files",
            "noise_public_key": _RUNNER_NOISE_PUBLIC_KEY,
        },
    )
    assert managed_registration.status_code == 201
    managed_runner_token = managed_registration.json()["runner_token"]
    assert authenticate_runner_token(managed_runner_token)["runner_id"] == managed["runner"]["id"]
    managed_heartbeat = auth_client.post(
        "/runner/heartbeat",
        headers={"Authorization": f"Bearer {managed_runner_token}"},
        json={
            "runner_version": "0.1.1",
            "capabilities": {"sqlite": True},
            "data_dir": "/var/lib/yinshi",
            "storage_profile": "aws_ebs_s3_files",
        },
    )
    assert managed_heartbeat.status_code == 200

    create_response = auth_client.post(
        "/api/settings/runner",
        json={"name": "AWS runner", "cloud_provider": "aws", "region": "us-east-1"},
    )
    assert create_response.status_code == 201
    byoc_runner_id = create_response.json()["runner"]["id"]
    assert "kind" not in create_response.json()["runner"]
    byoc_registration = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_response.json()["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
            "storage_profile": "aws_ebs_s3_files",
            "noise_public_key": _RUNNER_NOISE_PUBLIC_KEY,
        },
    )
    assert byoc_registration.status_code == 201
    confirmation = auth_client.post(
        "/api/settings/runner/noise-key/confirm",
        json={"noise_public_key": _RUNNER_NOISE_PUBLIC_KEY},
    )
    assert confirmation.status_code == 200
    capability = auth_client.post(
        "/api/settings/runner/capabilities",
        json={
            "initiator_public_key": "a8OCKiqn9OaYHWU4aSs83z5t-e6m7SaetB2TwidXt1o",
            "scopes": ["worker.health"],
            "max_session_bytes": 65_536,
        },
    )
    assert capability.status_code == 201
    assert capability.json()["runner_id"] == byoc_runner_id

    status_response = auth_client.get("/api/settings/runner")
    assert status_response.status_code == 200
    assert status_response.json()["id"] == byoc_runner_id
    assert status_response.json()["cloud_provider"] == "aws"
    assert "kind" not in status_response.json()

    revoke_response = auth_client.delete("/api/settings/runner")
    assert revoke_response.status_code == 204
    assert authenticate_runner_token(managed_runner_token)["runner_id"] == managed["runner"]["id"]

    with get_control_db() as database:
        rows = database.execute(
            """
            SELECT kind, cloud_provider, revoked_at
            FROM user_runners
            WHERE user_id = ?
            ORDER BY kind
            """,
            (tenant.user_id,),
        ).fetchall()
    assert [(row["kind"], row["cloud_provider"]) for row in rows] == [
        ("byoc", "aws"),
        ("managed", "fly_sprites"),
    ]
    assert rows[0]["revoked_at"] is not None
    assert rows[1]["revoked_at"] is None


def test_cloud_runner_rejects_sqlite_under_shared_files(auth_client: TestClient) -> None:
    """Runner registration rejects live SQLite paths on the shared file mount."""
    create_response = auth_client.post(
        "/api/settings/runner",
        json={"name": "AWS runner", "cloud_provider": "aws", "region": "us-east-1"},
    )
    assert create_response.status_code == 201

    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_response.json()["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/mnt/yinshi-s3-files/sqlite",
            "shared_files_dir": "/mnt/yinshi-s3-files",
        },
    )
    assert register_response.status_code == 400
    assert register_response.json()["detail"] == "sqlite_dir must not live under shared_files_dir"


def test_cloud_runner_revoke_invalidates_bearer_token(auth_client: TestClient) -> None:
    """Revoking a runner clears stored bearer material and rejects heartbeats."""
    create_response = auth_client.post(
        "/api/settings/runner",
        json={"name": "AWS runner", "cloud_provider": "aws", "region": "us-east-1"},
    )
    assert create_response.status_code == 201
    registration_token = create_response.json()["registration_token"]

    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": registration_token,
            "runner_version": "0.1.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
        },
    )
    assert register_response.status_code == 201
    runner_token = register_response.json()["runner_token"]

    revoke_response = auth_client.delete("/api/settings/runner")
    assert revoke_response.status_code == 204

    status_response = auth_client.get("/api/settings/runner")
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "revoked"

    heartbeat_response = auth_client.post(
        "/runner/heartbeat",
        headers={"Authorization": f"Bearer {runner_token}"},
        json={
            "runner_version": "0.1.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
        },
    )
    assert heartbeat_response.status_code == 401


def test_registration_does_not_revive_a_concurrently_revoked_runner(
    auth_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revoke that commits mid-registration must win over the stale write."""
    from yinshi.exceptions import RunnerRegistrationError
    from yinshi.services import runners as runners_service
    from yinshi.services.runners import (
        create_runner_registration,
        get_runner_for_user,
        register_runner,
        revoke_runner_for_user,
    )

    tenant = getattr(auth_client, "yinshi_tenant")
    registration = create_runner_registration(
        tenant.user_id,
        name="Racing runner",
        cloud_provider="aws",
        region="us-west-2",
        storage_profile="aws_ebs_s3_files",
        control_url="http://testserver",
    )
    real_profile_match = runners_service._requested_profile_matches

    def revoke_then_match(**kwargs):
        revoke_runner_for_user(tenant.user_id)
        return real_profile_match(**kwargs)

    monkeypatch.setattr(runners_service, "_requested_profile_matches", revoke_then_match)

    with pytest.raises(RunnerRegistrationError):
        register_runner(
            registration["registration_token"],
            runner_version="0.1.0",
            capabilities={},
            data_dir="/var/lib/yinshi",
            sqlite_dir="/var/lib/yinshi/sqlite",
            shared_files_dir="/mnt/yinshi-s3-files",
            storage_profile="aws_ebs_s3_files",
            noise_public_key=_RUNNER_NOISE_PUBLIC_KEY,
        )

    runner = get_runner_for_user(tenant.user_id)
    assert runner is not None
    assert runner["status"] == "revoked"
    assert runner["registered_at"] is None
    assert runner["last_heartbeat_at"] is None


def test_runner_heartbeat_requires_bearer_token(auth_client: TestClient) -> None:
    """The open heartbeat endpoint is still protected by runner bearer auth."""
    response = auth_client.post(
        "/runner/heartbeat",
        json={
            "runner_version": "0.1.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
        },
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Runner bearer token is required"


def test_cloud_runner_create_archil_profiles(auth_client: TestClient) -> None:
    """Runner token creation stores profile-specific Archil defaults before boot."""
    shared_response = auth_client.post(
        "/api/settings/runner",
        json={
            "name": "Archil shared files runner",
            "cloud_provider": "aws",
            "region": "us-east-1",
            "storage_profile": "archil_shared_files",
        },
    )
    assert shared_response.status_code == 201
    shared_payload = shared_response.json()
    assert shared_payload["runner"]["capabilities"]["storage_profile"] == "archil_shared_files"
    assert shared_payload["runner"]["capabilities"]["storage_profile_experimental"] is True
    assert shared_payload["runner"]["capabilities"]["sqlite_storage"] == "runner_ebs"
    assert shared_payload["runner"]["capabilities"]["shared_files_storage"] == "archil"
    assert shared_payload["environment"]["YINSHI_RUNNER_SHARED_FILES_DIR"] == "/mnt/archil/yinshi"

    all_posix_response = auth_client.post(
        "/api/settings/runner",
        json={
            "name": "Archil all POSIX runner",
            "cloud_provider": "aws",
            "region": "us-east-1",
            "storage_profile": "archil_all_posix",
        },
    )
    assert all_posix_response.status_code == 201
    all_posix_payload = all_posix_response.json()
    assert all_posix_payload["runner"]["capabilities"]["storage_profile"] == "archil_all_posix"
    assert all_posix_payload["runner"]["capabilities"]["sqlite_storage"] == "archil"
    assert all_posix_payload["runner"]["capabilities"]["shared_files_storage"] == "archil"
    assert all_posix_payload["runner"]["capabilities"]["live_sqlite_on_shared_files"] is True
    assert (
        all_posix_payload["environment"]["YINSHI_RUNNER_SQLITE_DIR"] == "/mnt/archil/yinshi/sqlite"
    )
    assert (
        all_posix_payload["environment"]["YINSHI_RUNNER_DATA_PROTECTION_KEY_FILE"]
        == "/mnt/archil/yinshi/sqlite/.yinshi-data-protection-key"
    )


def test_cloud_runner_rejects_unsupported_storage_profile(auth_client: TestClient) -> None:
    """Pydantic rejects unknown stable profile identifiers at the API boundary."""
    response = auth_client.post(
        "/api/settings/runner",
        json={
            "name": "Bad runner",
            "cloud_provider": "aws",
            "region": "us-east-1",
            "storage_profile": "not-a-profile",
        },
    )
    assert response.status_code == 422


def test_archil_profile_registration_requires_explicit_storage(auth_client: TestClient) -> None:
    """Archil profiles require runner-provided storage class evidence."""
    create_response = auth_client.post(
        "/api/settings/runner",
        json={
            "name": "Archil shared files runner",
            "cloud_provider": "aws",
            "region": "us-east-1",
            "storage_profile": "archil_shared_files",
        },
    )
    assert create_response.status_code == 201

    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_response.json()["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/var/lib/yinshi/sqlite",
            "shared_files_dir": "/mnt/archil/yinshi",
            "storage_profile": "archil_shared_files",
        },
    )
    assert register_response.status_code == 400
    assert register_response.json()["detail"] == (
        "sqlite_storage must be runner_ebs for archil_shared_files"
    )


def test_archil_shared_files_registers_with_ebs_sqlite(auth_client: TestClient) -> None:
    """Archil shared-files mode keeps live SQLite on runner EBS."""
    create_response = auth_client.post(
        "/api/settings/runner",
        json={
            "name": "Archil shared files runner",
            "cloud_provider": "aws",
            "region": "us-east-1",
            "storage_profile": "archil_shared_files",
        },
    )
    assert create_response.status_code == 201

    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_response.json()["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {
                "sqlite_storage": "runner_ebs",
                "shared_files_storage": "archil",
            },
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/var/lib/yinshi/sqlite",
            "shared_files_dir": "/mnt/archil/yinshi",
            "storage_profile": "archil_shared_files",
        },
    )
    assert register_response.status_code == 201

    status_response = auth_client.get("/api/settings/runner")
    assert status_response.status_code == 200
    capabilities = status_response.json()["capabilities"]
    assert capabilities["storage_profile"] == "archil_shared_files"
    assert capabilities["sqlite_storage"] == "runner_ebs"
    assert capabilities["shared_files_storage"] == "archil"
    assert capabilities["live_sqlite_on_shared_files"] is False


def test_archil_shared_files_rejects_sqlite_under_archil(auth_client: TestClient) -> None:
    """Archil shared-files mode still blocks live SQLite below the shared root."""
    create_response = auth_client.post(
        "/api/settings/runner",
        json={
            "name": "Archil shared files runner",
            "cloud_provider": "aws",
            "region": "us-east-1",
            "storage_profile": "archil_shared_files",
        },
    )
    assert create_response.status_code == 201

    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_response.json()["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {
                "sqlite_storage": "runner_ebs",
                "shared_files_storage": "archil",
            },
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/mnt/archil/yinshi/sqlite",
            "shared_files_dir": "/mnt/archil/yinshi",
            "storage_profile": "archil_shared_files",
        },
    )
    assert register_response.status_code == 400
    assert register_response.json()["detail"] == "sqlite_dir must not live under shared_files_dir"


def test_archil_all_posix_allows_sqlite_under_archil(auth_client: TestClient) -> None:
    """Archil all-POSIX mode explicitly allows live SQLite on Archil storage."""
    create_response = auth_client.post(
        "/api/settings/runner",
        json={
            "name": "Archil all POSIX runner",
            "cloud_provider": "aws",
            "region": "us-east-1",
            "storage_profile": "archil_all_posix",
        },
    )
    assert create_response.status_code == 201

    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_response.json()["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {
                "sqlite_storage": "archil",
                "shared_files_storage": "archil",
            },
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/mnt/archil/yinshi/sqlite",
            "shared_files_dir": "/mnt/archil/yinshi",
            "storage_profile": "archil_all_posix",
        },
    )
    assert register_response.status_code == 201

    status_response = auth_client.get("/api/settings/runner")
    assert status_response.status_code == 200
    capabilities = status_response.json()["capabilities"]
    assert capabilities["storage_profile"] == "archil_all_posix"
    assert capabilities["sqlite_storage"] == "archil"
    assert capabilities["shared_files_storage"] == "archil"
    assert capabilities["live_sqlite_on_shared_files"] is True


def test_archil_all_posix_requires_archil_storage(auth_client: TestClient) -> None:
    """All-POSIX profile rejects generic local or S3-compatible storage claims."""
    create_response = auth_client.post(
        "/api/settings/runner",
        json={
            "name": "Archil all POSIX runner",
            "cloud_provider": "aws",
            "region": "us-east-1",
            "storage_profile": "archil_all_posix",
        },
    )
    assert create_response.status_code == 201

    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_response.json()["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {
                "sqlite_storage": "archil",
                "shared_files_storage": "local_posix",
            },
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/mnt/archil/yinshi/sqlite",
            "shared_files_dir": "/mnt/archil/yinshi",
            "storage_profile": "archil_all_posix",
        },
    )
    assert register_response.status_code == 400
    assert "shared_files_storage must be one of archil" in register_response.json()["detail"]


def test_runner_heartbeat_rejects_storage_profile_drift(auth_client: TestClient) -> None:
    """A runner cannot change storage profile after registration."""
    create_response = auth_client.post(
        "/api/settings/runner",
        json={
            "name": "AWS runner",
            "cloud_provider": "aws",
            "region": "us-east-1",
            "storage_profile": "aws_ebs_s3_files",
        },
    )
    assert create_response.status_code == 201

    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_response.json()["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {"shared_files_storage": "s3_files_mount"},
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/var/lib/yinshi/sqlite",
            "shared_files_dir": "/mnt/yinshi-s3-files",
            "storage_profile": "aws_ebs_s3_files",
        },
    )
    assert register_response.status_code == 201

    heartbeat_response = auth_client.post(
        "/runner/heartbeat",
        headers={"Authorization": f"Bearer {register_response.json()['runner_token']}"},
        json={
            "runner_version": "0.1.0",
            "capabilities": {
                "sqlite_storage": "runner_ebs",
                "shared_files_storage": "archil",
            },
            "data_dir": "/var/lib/yinshi",
            "sqlite_dir": "/var/lib/yinshi/sqlite",
            "shared_files_dir": "/mnt/archil/yinshi",
            "storage_profile": "archil_shared_files",
        },
    )
    assert heartbeat_response.status_code == 400
    assert (
        heartbeat_response.json()["detail"] == "storage_profile must match requested runner profile"
    )


def _set_runner_agent_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point runner-agent paths at an isolated writable test directory."""
    monkeypatch.setenv("YINSHI_CONTROL_URL", "https://control.example")
    monkeypatch.setenv("YINSHI_RUNNER_TOKEN_FILE", str(tmp_path / "runner-token"))
    monkeypatch.setenv("YINSHI_RUNNER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YINSHI_RUNNER_SQLITE_DIR", str(tmp_path / "sqlite"))
    monkeypatch.setenv("YINSHI_RUNNER_SHARED_FILES_DIR", str(tmp_path / "shared"))


def test_runner_agent_defaults_to_aws_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty runner storage env advertises the safe AWS BYOC profile."""
    _set_runner_agent_env(monkeypatch, tmp_path)

    config = runner_agent.load_config()
    payload = runner_agent._runner_status_payload(config)

    assert payload["storage_profile"] == "aws_ebs_s3_files"
    assert payload["capabilities"]["storage_profile"] == "aws_ebs_s3_files"
    assert payload["capabilities"]["sqlite_storage"] == "runner_ebs"
    assert payload["capabilities"]["shared_files_storage"] == "local_posix"
    assert payload["capabilities"]["live_sqlite_on_shared_files"] is False


def test_runner_agent_registration_persists_noise_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Registration advertises one stable owner-only runner Noise identity."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_REGISTRATION_TOKEN", "r" * 32)

    config = runner_agent.load_config()
    first_payload = runner_agent._runner_registration_payload(config)
    second_payload = runner_agent._runner_registration_payload(config)

    assert first_payload["registration_token"] == "r" * 32
    assert first_payload["noise_public_key"] == second_payload["noise_public_key"]
    assert len(first_payload["noise_public_key"]) == 43
    assert config.noise_private_key_file == tmp_path / "data" / "runner-noise.key"
    assert config.noise_private_key_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_runner_agent_pins_control_plane_capability_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Registration persists one control key and heartbeat rejects key changes."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_REGISTRATION_TOKEN", "r" * 32)
    signing_key = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
    heartbeat_key = {"value": signing_key}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/runner/register":
            return httpx.Response(
                201,
                json={
                    "runner_id": "runner-1",
                    "runner_token": "runner-secret",
                    "capability_signing_public_key": signing_key,
                    "status": "online",
                },
            )
        return httpx.Response(
            200,
            json={
                "runner_id": "runner-1",
                "capability_signing_public_key": heartbeat_key["value"],
                "status": "online",
            },
        )

    config = runner_agent.load_config()
    async with httpx.AsyncClient(
        base_url=config.control_url,
        transport=httpx.MockTransport(handler),
    ) as client:
        token = await runner_agent._register(config, client)
        await runner_agent._heartbeat(config, client, token)
        heartbeat_key["value"] = "a8OCKiqn9OaYHWU4aSs83z5t-e6m7SaetB2TwidXt1o"
        with pytest.raises(RuntimeError, match="signing key changed"):
            await runner_agent._heartbeat(config, client, token)

    assert token == "runner-secret"
    assert config.capability_signing_key_file.read_text(encoding="ascii").strip() == signing_key
    assert config.capability_signing_key_file.stat().st_mode & 0o777 == 0o600


def test_runner_agent_advertises_fly_sprites_posix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fly Sprite agent advertises persistent local POSIX storage without extra env."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    digest = "a" * 64
    attestation = tmp_path / ".artifact-sha256"
    attestation.write_text(f"{digest}\n", encoding="ascii")
    attestation.chmod(0o600)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    monkeypatch.setenv("YINSHI_RUNNER_ARTIFACT_SHA256", digest)
    monkeypatch.setenv("YINSHI_RUNNER_ARTIFACT_ATTESTATION_FILE", str(attestation))
    monkeypatch.delenv("YINSHI_RUNNER_SQLITE_DIR")
    monkeypatch.delenv("YINSHI_RUNNER_SHARED_FILES_DIR")

    default_config = runner_agent.load_config()
    assert default_config.sqlite_dir == Path("/var/lib/yinshi/sqlite")
    assert default_config.shared_files_dir == Path("/var/lib/yinshi/files")

    monkeypatch.setenv("YINSHI_RUNNER_SQLITE_DIR", str(tmp_path / "sqlite"))
    monkeypatch.setenv("YINSHI_RUNNER_SHARED_FILES_DIR", str(tmp_path / "shared"))
    config = runner_agent.load_config()
    payload = runner_agent._runner_status_payload(config)

    assert payload["storage_profile"] == "fly_sprites_posix"
    assert payload["capabilities"]["storage_profile_experimental"] is False
    assert payload["capabilities"]["artifact_sha256"] == digest
    assert payload["capabilities"]["sqlite_storage"] == "local_posix"
    assert payload["capabilities"]["shared_files_storage"] == "local_posix"
    assert payload["capabilities"]["live_sqlite_on_shared_files"] is False


def test_runner_agent_advertises_archil_shared_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Archil shared-files env advertises Archil only for shared files."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "archil_shared_files")
    monkeypatch.setenv("YINSHI_RUNNER_SHARED_FILES_STORAGE", "archil")

    config = runner_agent.load_config()
    payload = runner_agent._runner_status_payload(config)

    assert payload["storage_profile"] == "archil_shared_files"
    assert payload["capabilities"]["sqlite_storage"] == "runner_ebs"
    assert payload["capabilities"]["shared_files_storage"] == "archil"
    assert payload["capabilities"]["live_sqlite_on_shared_files"] is False


def test_runner_agent_all_posix_allows_sqlite_under_archil(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Archil all-POSIX env allows SQLite beneath the shared Archil root."""
    shared_files_dir = tmp_path / "archil"
    sqlite_dir = shared_files_dir / "sqlite"
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "archil_all_posix")
    monkeypatch.setenv("YINSHI_RUNNER_SQLITE_STORAGE", "archil")
    monkeypatch.setenv("YINSHI_RUNNER_SHARED_FILES_STORAGE", "archil")
    monkeypatch.setenv("YINSHI_RUNNER_SQLITE_DIR", str(sqlite_dir))
    monkeypatch.setenv("YINSHI_RUNNER_SHARED_FILES_DIR", str(shared_files_dir))

    config = runner_agent.load_config()
    payload = runner_agent._runner_status_payload(config)

    assert payload["storage_profile"] == "archil_all_posix"
    assert payload["capabilities"]["sqlite_storage"] == "archil"
    assert payload["capabilities"]["shared_files_storage"] == "archil"
    assert payload["capabilities"]["live_sqlite_on_shared_files"] is True


def test_runner_agent_requires_archil_shared_storage_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Archil profiles fail before registration without explicit Archil evidence."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "archil_shared_files")

    with pytest.raises(RuntimeError, match="YINSHI_RUNNER_SHARED_FILES_STORAGE must be archil"):
        runner_agent.load_config()


def test_runner_agent_rejects_aws_sqlite_under_shared(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AWS profile fails before registration when SQLite is placed under shared files."""
    shared_files_dir = tmp_path / "shared"
    sqlite_dir = shared_files_dir / "sqlite"
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_SQLITE_DIR", str(sqlite_dir))
    monkeypatch.setenv("YINSHI_RUNNER_SHARED_FILES_DIR", str(shared_files_dir))

    config = runner_agent.load_config()
    with pytest.raises(RuntimeError, match="must not live under"):
        runner_agent._runner_status_payload(config)
