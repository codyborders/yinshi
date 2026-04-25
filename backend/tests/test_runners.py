"""Exercise cloud runner registration, heartbeat, revocation, and status APIs.

The tests cover the control-plane lifecycle without launching cloud resources: a
user creates a one-time token, a runner consumes it, the runner heartbeats with a
bearer token, and revocation invalidates that bearer token.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


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
    assert create_payload["environment"]["YINSHI_RUNNER_DATA_DIR"] == "/var/lib/yinshi"
    assert create_payload["environment"]["YINSHI_RUNNER_TOKEN_FILE"].endswith("/runner-token")
    assert create_payload["registration_token"]

    register_response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": create_payload["registration_token"],
            "runner_version": "0.1.0",
            "capabilities": {"podman": True},
            "data_dir": "/var/lib/yinshi",
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
            "capabilities": {"podman": True, "aws_region": "us-west-2"},
            "data_dir": "/var/lib/yinshi",
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
    assert status_payload["capabilities"]["aws_region"] == "us-west-2"


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
