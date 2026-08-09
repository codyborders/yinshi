"""Verify runner pairing and short-lived signed dispatch capabilities.

API tests exercise account ownership and explicit key confirmation. Direct
verification tests alter signed bytes and time to cover runner-side rejection.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from yinshi.services.runner_capabilities import (
    runner_capability_signing_public_key,
    verify_runner_capability,
)
from yinshi.services.runner_relay import (
    RunnerRelayAuthorizationError,
    claim_runner_transfer_grant,
)

_RUNNER_PUBLIC_KEY = "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I"
_CLIENT_PUBLIC_KEY = "a8OCKiqn9OaYHWU4aSs83z5t-e6m7SaetB2TwidXt1o"


def _register_runner(auth_client: TestClient) -> dict[str, object]:
    create_response = auth_client.post(
        "/api/settings/runner",
        json={"name": "Private runner", "cloud_provider": "aws", "region": "us-west-2"},
    )
    assert create_response.status_code == 201
    registration_token = create_response.json()["registration_token"]
    response = auth_client.post(
        "/runner/register",
        json={
            "registration_token": registration_token,
            "runner_version": "0.2.0",
            "capabilities": {},
            "data_dir": "/var/lib/yinshi",
            "storage_profile": "aws_ebs_s3_files",
            "noise_public_key": _RUNNER_PUBLIC_KEY,
        },
    )
    assert response.status_code == 201
    return response.json()


def test_runner_pairing_gates_capability_issuance(auth_client: TestClient) -> None:
    """Dispatch stays blocked until user confirms the exact runner fingerprint."""
    registration = _register_runner(auth_client)
    capability_request = {
        "initiator_public_key": _CLIENT_PUBLIC_KEY,
        "scopes": ["worker.health", "repository.read"],
        "max_session_bytes": 1_048_576,
    }

    blocked_response = auth_client.post(
        "/api/settings/runner/capabilities",
        json=capability_request,
    )
    assert blocked_response.status_code == 409
    assert blocked_response.json()["detail"] == "Runner Noise key must be confirmed"

    status_response = auth_client.get("/api/settings/runner")
    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["noise_public_key"] == _RUNNER_PUBLIC_KEY
    assert status_payload["noise_key_confirmed"] is False
    assert status_payload["noise_key_fingerprint"].startswith("SHA256:")

    wrong_confirmation = auth_client.post(
        "/api/settings/runner/noise-key/confirm",
        json={"noise_public_key": _CLIENT_PUBLIC_KEY},
    )
    assert wrong_confirmation.status_code == 409

    confirmation = auth_client.post(
        "/api/settings/runner/noise-key/confirm",
        json={"noise_public_key": _RUNNER_PUBLIC_KEY},
    )
    assert confirmation.status_code == 200
    assert confirmation.json()["noise_key_confirmed"] is True

    capability_response = auth_client.post(
        "/api/settings/runner/capabilities",
        json=capability_request,
    )
    assert capability_response.status_code == 201
    payload = capability_response.json()
    assert payload["runner_public_key"] == _RUNNER_PUBLIC_KEY
    assert payload["runner_id"] == registration["runner_id"]
    assert payload["protocol"] == "yinshi-runner-v1"
    assert payload["relay_url"] == (f"ws://testserver/api/runner/relay/{payload['transfer_id']}")
    assert "capability" not in payload["relay_url"]
    assert payload["expires_at"] > payload["issued_at"]
    assert payload["expires_at"] - payload["issued_at"] == 300

    signing_public_key = base64.urlsafe_b64decode(runner_capability_signing_public_key() + "=")
    verified = verify_runner_capability(
        payload["capability"],
        signing_public_key=signing_public_key,
        expected_runner_id=str(registration["runner_id"]),
        expected_runner_public_key=_RUNNER_PUBLIC_KEY,
        current_time=payload["issued_at"],
    )
    assert verified is not None
    assert verified.user_id == auth_client.yinshi_tenant.user_id
    assert verified.transfer_id == payload["transfer_id"]
    assert verified.initiator_public_key == _CLIENT_PUBLIC_KEY
    assert verified.scopes == ("repository.read", "worker.health")
    assert verified.max_session_bytes == 1_048_576

    with pytest.raises(RunnerRelayAuthorizationError, match="does not match"):
        claim_runner_transfer_grant(
            payload["transfer_id"],
            payload["capability"] + "changed",
            current_time=payload["issued_at"],
        )
    grant = claim_runner_transfer_grant(
        payload["transfer_id"],
        payload["capability"],
        current_time=payload["issued_at"],
    )
    assert grant.runner_id == registration["runner_id"]
    assert grant.max_session_bytes == 1_048_576
    with pytest.raises(RunnerRelayAuthorizationError, match="already claimed"):
        claim_runner_transfer_grant(
            payload["transfer_id"],
            payload["capability"],
            current_time=payload["issued_at"],
        )


def test_runner_capability_rejects_tampering_and_expiry(auth_client: TestClient) -> None:
    """Runner verifier rejects changed signatures and expired grants."""
    registration = _register_runner(auth_client)
    confirmation = auth_client.post(
        "/api/settings/runner/noise-key/confirm",
        json={"noise_public_key": _RUNNER_PUBLIC_KEY},
    )
    assert confirmation.status_code == 200
    low_order_response = auth_client.post(
        "/api/settings/runner/capabilities",
        json={
            "initiator_public_key": "A" * 43,
            "scopes": ["worker.health"],
            "max_session_bytes": 65_536,
        },
    )
    assert low_order_response.status_code == 400
    assert "usable X25519 key" in low_order_response.json()["detail"]

    response = auth_client.post(
        "/api/settings/runner/capabilities",
        json={
            "initiator_public_key": _CLIENT_PUBLIC_KEY,
            "scopes": ["worker.health"],
            "max_session_bytes": 65_536,
        },
    )
    assert response.status_code == 201
    payload = response.json()
    token = payload["capability"]
    signing_public_key = base64.urlsafe_b64decode(runner_capability_signing_public_key() + "=")

    changed_last_character = "A" if token[-1] != "A" else "B"
    tampered_token = token[:-1] + changed_last_character
    assert (
        verify_runner_capability(
            tampered_token,
            signing_public_key=signing_public_key,
            expected_runner_id=str(registration["runner_id"]),
            expected_runner_public_key=_RUNNER_PUBLIC_KEY,
            current_time=payload["issued_at"],
        )
        is None
    )
    assert (
        verify_runner_capability(
            token,
            signing_public_key=signing_public_key,
            expected_runner_id=str(registration["runner_id"]),
            expected_runner_public_key=_RUNNER_PUBLIC_KEY,
            current_time=payload["expires_at"],
        )
        is None
    )
