"""Tests for authenticated session history cache keys."""

import base64
import re
from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.conftest import DEFAULT_TEST_SECRET

_CANONICAL_BASE64URL_32 = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _decode_key(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=")


def test_history_cache_key_requires_authentication(client: TestClient) -> None:
    """Unauthenticated callers cannot obtain cache key material."""
    response = client.get("/api/runtime/history-cache-key")

    assert response.status_code == 401


def test_history_cache_key_is_stable_canonical_and_not_cacheable(
    auth_client: TestClient,
) -> None:
    """One user receives one stable, bounded AES key without secret leakage."""
    first = auth_client.get("/api/runtime/history-cache-key")
    second = auth_client.get("/api/runtime/history-cache-key")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["pragma"] == "no-cache"
    body = first.json()
    assert set(body) == {"version", "user_id", "key_id", "key"}
    assert body["version"] == 1
    assert body["user_id"] == auth_client.yinshi_tenant.user_id
    assert re.fullmatch(r"[0-9a-f]{16}", body["key_id"])
    assert _CANONICAL_BASE64URL_32.fullmatch(body["key"])
    assert len(_decode_key(body["key"])) == 32
    assert DEFAULT_TEST_SECRET not in first.text


def test_history_cache_keys_are_separated_by_user(
    auth_client_factory: Callable[..., TestClient],
) -> None:
    """Domain-separated derivation produces different keys for different users."""
    first_client = auth_client_factory(
        email="first@example.com", provider_user_id="history-cache-first"
    )
    second_client = auth_client_factory(
        email="second@example.com", provider_user_id="history-cache-second"
    )

    first = first_client.get("/api/runtime/history-cache-key").json()
    second = second_client.get("/api/runtime/history-cache-key").json()

    assert first["user_id"] != second["user_id"]
    assert first["key_id"] != second["key_id"]
    assert first["key"] != second["key"]
