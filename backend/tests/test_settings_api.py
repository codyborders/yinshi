"""Tests for API key management endpoints."""

from fastapi.testclient import TestClient


def test_list_keys_empty(auth_client: TestClient):
    """GET /api/settings/keys should return empty list initially."""
    resp = auth_client.get("/api/settings/keys")
    assert resp.status_code == 200
    assert resp.json() == []


def test_add_and_list_key(auth_client: TestClient):
    """POST /api/settings/keys should store a key and list it."""
    resp = auth_client.post(
        "/api/settings/keys",
        json={"provider": "anthropic", "key": "sk-ant-test-key", "label": "My Key"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider"] == "anthropic"
    assert data["label"] == "My Key"
    assert "key" not in data  # Key value should never be returned

    # List should show it
    resp = auth_client.get("/api/settings/keys")
    assert len(resp.json()) == 1


def test_delete_key(auth_client: TestClient):
    """DELETE /api/settings/keys/:id should remove the key."""
    create_resp = auth_client.post(
        "/api/settings/keys",
        json={"provider": "minimax", "key": "sk-minimax-test", "label": ""},
    )
    key_id = create_resp.json()["id"]

    resp = auth_client.delete(
        f"/api/settings/keys/{key_id}",
    )
    assert resp.status_code == 204

    # Should be gone
    resp = auth_client.get("/api/settings/keys")
    assert len(resp.json()) == 0


def test_delete_key_not_found(auth_client: TestClient):
    """DELETE /api/settings/keys/:id with bad ID should 404."""
    resp = auth_client.delete(
        "/api/settings/keys/nonexistent",
    )
    assert resp.status_code == 404


def test_add_api_key_with_config_connection(auth_client: TestClient):
    """Structured api_key_with_config secrets should be accepted and not echoed back."""
    resp = auth_client.post(
        "/api/settings/connections",
        json={
            "provider": "azure-openai-responses",
            "auth_strategy": "api_key_with_config",
            "secret": {"apiKey": "sk-azure-test"},
            "label": "Azure",
            "config": {"baseUrl": "https://example.openai.azure.com"},
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["provider"] == "azure-openai-responses"
    assert body["config"] == {"baseUrl": "https://example.openai.azure.com"}
    assert "sk-azure-test" not in resp.text

    list_resp = auth_client.get("/api/settings/connections")
    assert list_resp.status_code == 200
    assert "sk-azure-test" not in list_resp.text
