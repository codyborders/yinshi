"""Tests for BYOK key enforcement and usage logging."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.factories import create_full_stack, parse_sse_events


# --- Fixtures ---


@pytest.fixture
def control_env(tmp_path, monkeypatch):
    """Set up env for isolated control DB access."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("DISABLE_AUTH", "true")

    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_control_db, init_db

    init_db()
    init_control_db()

    yield
    get_settings.cache_clear()


@pytest.fixture
def test_user(control_env):
    """Create a user with DEK in the control DB."""
    from yinshi.services.accounts import resolve_or_create_user

    return resolve_or_create_user(
        provider="google",
        provider_user_id="test-google-id",
        email="test@example.com",
        display_name="Test User",
    )


# --- Unit tests: cost estimation ---


def test_estimate_cost_minimax():
    """MiniMax cost should be calculated from token counts."""
    from yinshi.services.keys import estimate_cost_cents

    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    cost = estimate_cost_cents("minimax", usage)
    # $0.30/M input + $1.20/M output = 150 cents
    assert cost == pytest.approx(150.0)


def test_estimate_cost_minimax_with_cache():
    """MiniMax cost should include cache token costs."""
    from yinshi.services.keys import estimate_cost_cents

    usage = {
        "input_tokens": 500_000,
        "output_tokens": 200_000,
        "cache_read_tokens": 1_000_000,
        "cache_write_tokens": 100_000,
    }
    cost = estimate_cost_cents("minimax", usage)
    # 500k input: 15c, 200k output: 24c, 1M cache_read: 3c, 100k cache_write: 0.375c
    assert cost == pytest.approx(42.375)


def test_estimate_cost_non_minimax():
    """Non-minimax providers return 0 because only MiniMax usage is estimated."""
    from yinshi.services.keys import estimate_cost_cents

    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert estimate_cost_cents("anthropic", usage) == 0.0


# --- Unit tests: key resolution ---


def test_resolve_user_api_key_round_trip(test_user):
    """Stored BYOK key should decrypt correctly."""
    from yinshi.db import get_control_db
    from yinshi.services.crypto import encrypt_api_key
    from yinshi.services.keys import get_user_dek, resolve_user_api_key

    user_id = test_user.user_id
    dek = get_user_dek(user_id)

    encrypted = encrypt_api_key("sk-test-anthropic-key", dek)
    with get_control_db() as db:
        db.execute(
            "INSERT INTO api_keys (user_id, provider, encrypted_key, label) "
            "VALUES (?, ?, ?, ?)",
            (user_id, "anthropic", encrypted, "test"),
        )
        db.commit()

    assert resolve_user_api_key(user_id, "anthropic") == "sk-test-anthropic-key"
    assert resolve_user_api_key(user_id, "minimax") is None


def test_record_usage_writes_usage_log_without_mutating_credit(test_user):
    """Usage logging should not consume shared credit for authenticated prompts."""
    from yinshi.db import get_control_db
    from yinshi.services.keys import record_usage

    record_usage(
        user_id=test_user.user_id,
        session_id="test-session-1",
        provider="minimax",
        model="MiniMax-M2.7",
        usage={
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
        key_source="byok",
    )
    with get_control_db() as db:
        usage_row = db.execute(
            "SELECT provider, model, key_source, cost_cents FROM usage_log WHERE session_id = ?",
            ("test-session-1",),
        ).fetchone()
        user_row = db.execute(
            "SELECT credit_used_cents FROM users WHERE id = ?",
            (test_user.user_id,),
        ).fetchone()

    assert usage_row["provider"] == "minimax"
    assert usage_row["model"] == "MiniMax-M2.7"
    assert usage_row["key_source"] == "byok"
    assert usage_row["cost_cents"] == pytest.approx(30.0)
    assert user_row["credit_used_cents"] == 0


def test_get_user_dek_lazy_generates_for_null_dek(control_env):
    """get_user_dek should generate and store a DEK for users with NULL encrypted_dek."""
    from yinshi.db import get_control_db
    from yinshi.services.keys import get_user_dek

    # Create a user manually with NULL encrypted_dek (simulates pre-encryption account)
    user_id = "legacy-user-no-dek"
    with get_control_db() as db:
        db.execute(
            "INSERT INTO users (id, email, encrypted_dek) VALUES (?, ?, NULL)",
            (user_id, "legacy@example.com"),
        )
        db.commit()

    # Should succeed (lazy-generate DEK) instead of raising
    dek = get_user_dek(user_id)
    assert isinstance(dek, bytes)
    assert len(dek) == 32

    # Verify DEK was persisted
    with get_control_db() as db:
        row = db.execute(
            "SELECT encrypted_dek FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    assert row["encrypted_dek"] is not None

    # Second call should return the same DEK
    dek2 = get_user_dek(user_id)
    assert dek == dek2


# --- Unit tests: resolve_api_key_for_prompt ---


def test_resolve_api_key_for_prompt_byok(test_user):
    """BYOK key should be returned when available."""
    from yinshi.db import get_control_db
    from yinshi.services.crypto import encrypt_api_key
    from yinshi.services.keys import get_user_dek, resolve_api_key_for_prompt

    user_id = test_user.user_id
    dek = get_user_dek(user_id)
    encrypted = encrypt_api_key("sk-byok-key", dek)

    with get_control_db() as db:
        db.execute(
            "INSERT INTO api_keys (user_id, provider, encrypted_key) VALUES (?, ?, ?)",
            (user_id, "anthropic", encrypted),
        )
        db.commit()

    api_key, key_source = resolve_api_key_for_prompt(user_id, "anthropic")
    assert api_key == "sk-byok-key"
    assert key_source == "byok"


def test_resolve_api_key_for_prompt_requires_minimax_key(test_user):
    """MiniMax prompts should fail without a saved BYOK key."""
    from yinshi.exceptions import KeyNotFoundError
    from yinshi.services.keys import resolve_api_key_for_prompt

    with pytest.raises(KeyNotFoundError, match="No API key found for minimax"):
        resolve_api_key_for_prompt(test_user.user_id, "minimax")


def test_resolve_api_key_for_prompt_requires_non_minimax_key(test_user):
    """Anthropic prompts should fail without a saved BYOK key."""
    from yinshi.exceptions import KeyNotFoundError
    from yinshi.services.keys import resolve_api_key_for_prompt

    with pytest.raises(KeyNotFoundError, match="No API key found for anthropic"):
        resolve_api_key_for_prompt(test_user.user_id, "anthropic")


# --- Integration tests: prompt endpoint with BYOK ---


def _make_byok_mock_sidecar(
    query_events,
    resolve_provider="minimax",
    resolve_model_id="MiniMax-M2.7",
):
    """Build a mock SidecarClient for BYOK prompt tests."""
    mock = AsyncMock()
    model_ref = f"{resolve_provider}/{resolve_model_id}"
    mock.resolve_model = AsyncMock(
        return_value={"provider": resolve_provider, "model": model_ref}
    )
    mock.resolve_provider_auth = AsyncMock(
        return_value={
            "provider": resolve_provider,
            "auth": "sk-user-minimax-key" if resolve_provider == "minimax" else None,
            "model_ref": model_ref,
            "runtime_api_key": "sk-user-minimax-key" if resolve_provider == "minimax" else None,
            "model_config": None,
        }
    )
    mock.warmup = AsyncMock()
    mock.disconnect = AsyncMock()

    async def fake_query(
        sid,
        prompt,
        model=None,
        cwd=None,
        provider_auth=None,
        provider_config=None,
        agent_dir=None,
        settings_payload=None,
    ):
        for event in query_events:
            yield event

    mock.query = fake_query
    return mock


@pytest.fixture
def tenant_prompt_env(
    auth_client: TestClient,
    git_repo: str,
) -> dict[str, object]:
    """Full tenant-mode environment for prompt BYOK tests."""
    stack = create_full_stack(auth_client, git_repo, name="test")
    tenant = getattr(auth_client, "yinshi_tenant")
    return {
        "client": auth_client,
        "session_id": stack["session"]["id"],
        "user_id": tenant.user_id,
    }


def test_prompt_requires_saved_minimax_key(tenant_prompt_env):
    """MiniMax prompts should fail before a BYOK key is stored."""
    env = tenant_prompt_env
    mock = _make_byok_mock_sidecar([], resolve_provider="minimax")

    with patch("yinshi.api.stream.create_sidecar_connection", return_value=mock):
        resp = env["client"].post(
            f"/api/sessions/{env['session_id']}/prompt",
            json={"prompt": "hello"},
        )

    assert resp.status_code == 402
    mock.warmup.assert_not_called()


def test_prompt_uses_byok_key_when_stored(tenant_prompt_env):
    """BYOK key should be used instead of platform key when available."""
    env = tenant_prompt_env

    from yinshi.db import get_control_db
    from yinshi.services.crypto import encrypt_api_key
    from yinshi.services.keys import get_user_dek

    dek = get_user_dek(env["user_id"])
    encrypted = encrypt_api_key("sk-user-minimax-key", dek)
    with get_control_db() as db:
        db.execute(
            "INSERT INTO api_keys (user_id, provider, encrypted_key) VALUES (?, ?, ?)",
            (env["user_id"], "minimax", encrypted),
        )
        db.commit()

    result_events = [
        {
            "type": "message",
            "data": {
                "type": "result",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "provider": "minimax",
            },
        },
    ]
    mock = _make_byok_mock_sidecar(result_events, resolve_provider="minimax")

    with patch("yinshi.api.stream.create_sidecar_connection", return_value=mock):
        resp = env["client"].post(
            f"/api/sessions/{env['session_id']}/prompt",
            json={"prompt": "hello"},
        )

    assert resp.status_code == 200
    mock.warmup.assert_called_once()
    provider_auth = mock.warmup.call_args.kwargs["provider_auth"]
    assert provider_auth["provider"] == "minimax"
    assert provider_auth["authStrategy"] == "api_key"
    assert provider_auth["secret"] == "sk-user-minimax-key"


def test_prompt_402_for_non_minimax_without_byok(tenant_prompt_env):
    """402 for anthropic model without BYOK key."""
    env = tenant_prompt_env
    mock = _make_byok_mock_sidecar(
        [], resolve_provider="anthropic", resolve_model_id="claude-sonnet-4-20250514"
    )

    with patch("yinshi.api.stream.create_sidecar_connection", return_value=mock):
        resp = env["client"].post(
            f"/api/sessions/{env['session_id']}/prompt",
            json={"prompt": "hello", "model": "sonnet"},
        )

    assert resp.status_code == 402


def test_prompt_dev_mode_no_enforcement(
    db_path, tmp_path, monkeypatch, git_repo
):
    """Dev mode (DISABLE_AUTH=true) should not enforce BYOK."""
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("ENCRYPTION_PEPPER", "a" * 64)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("ALLOWED_REPO_BASE", str(tmp_path))

    from yinshi.config import get_settings

    get_settings.cache_clear()

    from yinshi.db import init_db

    init_db()

    from yinshi.main import app

    with TestClient(app) as client:
        repo = client.post(
            "/api/repos", json={"name": "test", "local_path": git_repo}
        ).json()
        ws = client.post(f"/api/repos/{repo['id']}/workspaces", json={}).json()
        sess = client.post(
            f"/api/workspaces/{ws['id']}/sessions", json={}
        ).json()

        async def fake_query(
            sid,
            prompt,
            model=None,
            cwd=None,
            provider_auth=None,
            provider_config=None,
            agent_dir=None,
            settings_payload=None,
        ):
            yield {"type": "message", "data": {"type": "result", "usage": {}}}

        mock = AsyncMock()
        mock.warmup = AsyncMock()
        mock.disconnect = AsyncMock()
        mock.query = fake_query

        with patch(
            "yinshi.api.stream.create_sidecar_connection", return_value=mock
        ):
            resp = client.post(
                f"/api/sessions/{sess['id']}/prompt",
                json={"prompt": "hello"},
            )

        assert resp.status_code == 200

    get_settings.cache_clear()
