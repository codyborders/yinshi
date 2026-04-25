"""Exercise cloud runner registration, heartbeat, revocation, and status APIs.

The tests cover the control-plane lifecycle without launching cloud resources: a
user creates a one-time token, a runner consumes it, the runner heartbeats with a
bearer token, and revocation invalidates that bearer token.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yinshi import runner_agent


def test_cloud_runner_registration_and_heartbeat(auth_client: TestClient) -> None:
    """A user can create a token, register a runner, and see it online."""
    create_response = auth_client.post(
        "/api/settings/runner",
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
    assert status_payload["capabilities"]["sqlite"] is True
    assert status_payload["capabilities"]["storage_profile"] == "aws_ebs_s3_files"
    assert status_payload["capabilities"]["storage_profile_experimental"] is False
    assert status_payload["capabilities"]["sqlite_storage"] == "runner_ebs"
    assert status_payload["capabilities"]["sqlite_dir"] == "/var/lib/yinshi/sqlite"
    assert status_payload["capabilities"]["shared_files_storage"] == "s3_files_mount"
    assert status_payload["capabilities"]["shared_files_dir"] == "/mnt/yinshi-s3-files"
    assert status_payload["capabilities"]["live_sqlite_on_shared_files"] is False
    assert status_payload["capabilities"]["aws_region"] == "us-west-2"


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
