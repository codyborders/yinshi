"""Tests hosted desktop authorization request validation through the public API."""

from __future__ import annotations

import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.conftest import DEFAULT_TEST_HEADERS

PKCE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
REQUEST_STATE = "desktop_state_0123456789abcdef"
CALLBACK_URI = "http://127.0.0.1:43123/auth/desktop/callback"


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
