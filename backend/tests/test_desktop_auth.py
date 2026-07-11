"""Tests hosted desktop authorization request validation through the public API."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient

from tests.conftest import DEFAULT_TEST_HEADERS

PKCE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
PKCE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
REQUEST_STATE = "desktop_state_0123456789abcdef"
CALLBACK_URI = "http://127.0.0.1:43123/auth/desktop/callback"


def _decode_base64url(value: str) -> bytes:
    """Decode one unpadded compact-token segment for independent verification."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def _create_approved_code(client: TestClient) -> str:
    """Create and browser-approve one desktop request through public routes."""
    created_response = client.post(
        "/auth/desktop/requests",
        json={
            "redirect_uri": CALLBACK_URI,
            "code_challenge": PKCE_CHALLENGE,
            "state": REQUEST_STATE,
        },
        headers=DEFAULT_TEST_HEADERS,
    )
    assert created_response.status_code == 201
    authorize_path = urlsplit(created_response.json()["authorize_url"]).path
    approved_response = client.get(authorize_path, follow_redirects=False)
    assert approved_response.status_code == 307
    callback_query = parse_qs(urlsplit(approved_response.headers["location"]).query)
    return callback_query["code"][0]


def _exchange_code(client: TestClient, code: str, verifier: str = PKCE_VERIFIER):
    """Submit one desktop token request with stable test-device metadata."""
    return client.post(
        "/auth/desktop/token",
        json={
            "authorization_code": code,
            "code_verifier": verifier,
            "device_name": "Test Mac",
        },
        headers=DEFAULT_TEST_HEADERS,
    )


def test_create_desktop_authorization_request(noauth_client: TestClient) -> None:
    """Valid PKCE and loopback input should create one opaque short-lived request."""
    response = noauth_client.post(
        "/auth/desktop/requests",
        json={
            "redirect_uri": CALLBACK_URI,
            "code_challenge": PKCE_CHALLENGE,
            "state": REQUEST_STATE,
        },
        headers=DEFAULT_TEST_HEADERS,
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"request_id", "authorize_url", "expires_at"}
    assert len(body["request_id"]) >= 32
    authorize_url = urlsplit(body["authorize_url"])
    assert authorize_url.path == f"/auth/desktop/authorize/{body['request_id']}"
    assert authorize_url.query == ""
    assert authorize_url.fragment == ""

    from yinshi.db import get_control_db

    request_digest = hashlib.sha256(body["request_id"].encode("utf-8")).hexdigest()
    with get_control_db() as database:
        row = database.execute(
            "SELECT * FROM desktop_authorization_requests WHERE request_id_hash = ?",
            (request_digest,),
        ).fetchone()
    assert row is not None
    assert row["request_id_hash"] == request_digest
    assert row["redirect_uri"] == CALLBACK_URI
    assert row["code_challenge"] == PKCE_CHALLENGE
    assert row["state"] == REQUEST_STATE
    assert row["expires_at"] > int(time.time())
    assert body["request_id"] not in "|".join(str(value) for value in row)


def test_authorize_desktop_request_issues_one_hashed_callback_code(
    auth_client_factory,
) -> None:
    """A signed-in browser should approve a request once without storing its raw code."""
    client = auth_client_factory(
        email="desktop-user@example.com",
        provider_user_id="desktop-user-id",
    )
    created_response = client.post(
        "/auth/desktop/requests",
        json={
            "redirect_uri": CALLBACK_URI,
            "code_challenge": PKCE_CHALLENGE,
            "state": REQUEST_STATE,
        },
        headers=DEFAULT_TEST_HEADERS,
    )
    assert created_response.status_code == 201
    created = created_response.json()
    authorize_path = urlsplit(created["authorize_url"]).path

    response = client.get(authorize_path, follow_redirects=False)

    assert response.status_code == 307
    callback = urlsplit(response.headers["location"])
    assert f"{callback.scheme}://{callback.netloc}{callback.path}" == CALLBACK_URI
    callback_query = parse_qs(callback.query)
    assert callback_query["state"] == [REQUEST_STATE]
    authorization_code = callback_query["code"][0]
    assert len(authorization_code) >= 32

    from yinshi.db import get_control_db

    request_digest = hashlib.sha256(created["request_id"].encode("utf-8")).hexdigest()
    with get_control_db() as database:
        row = database.execute(
            "SELECT * FROM desktop_authorization_requests WHERE request_id_hash = ?",
            (request_digest,),
        ).fetchone()
    assert row is not None
    assert row["user_id"] == getattr(client, "yinshi_tenant").user_id
    assert (
        row["authorization_code_hash"]
        == hashlib.sha256(authorization_code.encode("utf-8")).hexdigest()
    )
    assert authorization_code not in "|".join(str(value) for value in row)

    repeated_response = client.get(authorize_path, follow_redirects=False)
    assert repeated_response.status_code == 409


def test_exchange_desktop_code_issues_hashed_device_credentials(
    auth_client_factory,
) -> None:
    """A valid PKCE exchange should consume the code and issue scoped credentials."""
    client = auth_client_factory(
        email="token-user@example.com",
        provider_user_id="token-user-id",
    )
    authorization_code = _create_approved_code(client)

    response = _exchange_code(client, authorization_code)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "token_type",
        "access_token",
        "access_token_expires_at",
        "refresh_token",
        "refresh_token_expires_at",
        "account_lease",
        "account_lease_expires_at",
        "device_id",
        "signing_public_key",
        "user",
    }
    assert body["token_type"] == "Bearer"
    assert body["access_token"].count(".") == 2
    assert len(body["refresh_token"]) >= 32
    assert body["account_lease"].count(".") == 2
    public_key = Ed25519PublicKey.from_public_bytes(_decode_base64url(body["signing_public_key"]))
    expected_tokens = (
        (body["access_token"], "YINSHI-ACCESS", body["access_token_expires_at"]),
        (body["account_lease"], "YINSHI-LEASE", body["account_lease_expires_at"]),
    )
    for token, expected_type, expected_expiry in expected_tokens:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        public_key.verify(
            _decode_base64url(encoded_signature),
            f"{encoded_header}.{encoded_payload}".encode("ascii"),
        )
        header = json.loads(_decode_base64url(encoded_header))
        payload = json.loads(_decode_base64url(encoded_payload))
        assert header == {"alg": "EdDSA", "typ": expected_type, "v": 1}
        assert payload["sub"] == getattr(client, "yinshi_tenant").user_id
        assert payload["device_id"] == body["device_id"]
        assert payload["exp"] == expected_expiry

    assert body["user"]["id"] == getattr(client, "yinshi_tenant").user_id
    assert body["user"]["email"] == "token-user@example.com"

    from yinshi.db import get_control_db

    code_hash = hashlib.sha256(authorization_code.encode("utf-8")).hexdigest()
    refresh_hash = hashlib.sha256(body["refresh_token"].encode("utf-8")).hexdigest()
    with get_control_db() as database:
        request_row = database.execute(
            "SELECT consumed_at FROM desktop_authorization_requests "
            "WHERE authorization_code_hash = ?",
            (code_hash,),
        ).fetchone()
        device_row = database.execute(
            "SELECT * FROM desktop_devices WHERE id = ?",
            (body["device_id"],),
        ).fetchone()
    assert request_row is not None
    assert request_row["consumed_at"] is not None
    assert device_row is not None
    assert device_row["refresh_token_hash"] == refresh_hash
    assert body["refresh_token"] not in "|".join(str(value) for value in device_row)


def test_exchange_desktop_code_rolls_back_when_device_storage_fails(
    auth_client_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credential storage failure should leave the one-time code available for retry."""
    client = auth_client_factory(
        email="atomic-user@example.com",
        provider_user_id="atomic-user-id",
    )
    authorization_code = _create_approved_code(client)
    user_id = getattr(client, "yinshi_tenant").user_id
    collision_id = "fixed-device-id"

    from yinshi.db import get_control_db
    from yinshi.main import app
    from yinshi.services import desktop_auth

    with get_control_db() as database:
        database.execute(
            """
            INSERT INTO desktop_devices
            (id, user_id, name, created_at, refresh_token_hash, refresh_token_expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (collision_id, user_id, "Existing Mac", 1, "f" * 64, 9999999999),
        )
        database.commit()

    original_token_hex = desktop_auth.secrets.token_hex
    monkeypatch.setattr(desktop_auth.secrets, "token_hex", lambda _: collision_id)
    with TestClient(app, raise_server_exceptions=False) as failure_client:
        failure_client.cookies.update(client.cookies)
        failure_client.headers.update(DEFAULT_TEST_HEADERS)
        failure_response = _exchange_code(failure_client, authorization_code)
    monkeypatch.setattr(desktop_auth.secrets, "token_hex", original_token_hex)

    code_hash = hashlib.sha256(authorization_code.encode("utf-8")).hexdigest()
    with get_control_db() as database:
        request_row = database.execute(
            "SELECT consumed_at FROM desktop_authorization_requests "
            "WHERE authorization_code_hash = ?",
            (code_hash,),
        ).fetchone()
    assert failure_response.status_code == 503
    assert request_row is not None
    assert request_row["consumed_at"] is None

    retry_response = _exchange_code(client, authorization_code)
    assert retry_response.status_code == 200


def test_exchange_desktop_code_does_not_consume_on_pkce_mismatch(
    auth_client_factory,
) -> None:
    """A wrong verifier should fail without preventing a later valid exchange."""
    client = auth_client_factory(
        email="pkce-user@example.com",
        provider_user_id="pkce-user-id",
    )
    authorization_code = _create_approved_code(client)

    mismatch_response = _exchange_code(client, authorization_code, verifier="x" * 43)
    valid_response = _exchange_code(client, authorization_code)

    assert mismatch_response.status_code == 400
    assert valid_response.status_code == 200


def test_exchange_desktop_code_rejects_replay(auth_client_factory) -> None:
    """A consumed authorization code must not issue a second device credential."""
    client = auth_client_factory(
        email="replay-user@example.com",
        provider_user_id="replay-user-id",
    )
    authorization_code = _create_approved_code(client)

    first_response = _exchange_code(client, authorization_code)
    replay_response = _exchange_code(client, authorization_code)

    assert first_response.status_code == 200
    assert replay_response.status_code == 409


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://127.0.0.1:43123/auth/desktop/callback",
        "http://localhost:43123/auth/desktop/callback",
        "http://example.com:43123/auth/desktop/callback",
        "http://127.0.0.1/auth/desktop/callback",
        "http://127.0.0.1:43123/wrong/callback",
        "http://127.0.0.1:43123/auth/desktop/callback?input=private",
        "http://127.0.0.1:43123/auth/desktop/callback#fragment",
    ],
)
def test_create_desktop_authorization_request_rejects_unsafe_callback(
    noauth_client: TestClient,
    redirect_uri: str,
) -> None:
    """Desktop callbacks must target the exact numbered IPv4 loopback endpoint."""
    response = noauth_client.post(
        "/auth/desktop/requests",
        json={
            "redirect_uri": redirect_uri,
            "code_challenge": PKCE_CHALLENGE,
            "state": REQUEST_STATE,
        },
        headers=DEFAULT_TEST_HEADERS,
    )

    assert response.status_code == 422


def test_create_desktop_authorization_request_rejects_malformed_pkce(
    noauth_client: TestClient,
) -> None:
    """Only a complete base64url SHA-256 challenge should be accepted."""
    response = noauth_client.post(
        "/auth/desktop/requests",
        json={
            "redirect_uri": CALLBACK_URI,
            "code_challenge": "short+invalid",
            "state": REQUEST_STATE,
        },
        headers=DEFAULT_TEST_HEADERS,
    )

    assert response.status_code == 422
