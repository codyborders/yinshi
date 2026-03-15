"""Scenario-driven backend journey tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.factories import create_full_stack, make_mock_sidecar, parse_sse_events


def _make_streaming_sidecar(
    assistant_text: str,
    *,
    provider: str = "minimax",
    usage: dict[str, int] | None = None,
) -> AsyncMock:
    """Create a sidecar mock that streams assistant text and a final result."""

    async def fake_query(
        session_id: str,
        prompt: str,
        model: str | None = None,
        cwd: str | None = None,
        api_key: str | None = None,
    ):
        yield {
            "type": "message",
            "data": {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": assistant_text}],
                },
            },
        }
        yield {
            "type": "message",
            "data": {
                "type": "result",
                "usage": usage or {"input_tokens": 100, "output_tokens": 50},
                "provider": provider,
            },
        }

    return make_mock_sidecar(fake_query)


def _make_model_resolver(
    *,
    provider: str = "minimax",
    model: str = "MiniMax-M2.5-highspeed",
) -> AsyncMock:
    """Create a sidecar mock used only for model resolution."""
    mock = AsyncMock()
    mock.resolve_model = AsyncMock(return_value={"provider": provider, "model": model})
    mock.disconnect = AsyncMock()
    return mock


def test_new_user_onboarding_journey(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """A new user can create the full stack, prompt, stream, and persist history."""
    stack = create_full_stack(noauth_client, git_repo, name="test-repo")
    workspace = stack["workspace"]
    session = stack["session"]

    assert workspace["name"] == workspace["branch"]

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=_make_streaming_sidecar("Hello from the agent"),
    ):
        response = noauth_client.post(
            f"/api/sessions/{session['id']}/prompt",
            json={"prompt": "Fix the login page authentication bug"},
        )

    assert response.status_code == 200
    events = parse_sse_events(response.text)
    assert [event["type"] for event in events] == ["assistant", "result"]

    messages = noauth_client.get(f"/api/sessions/{session['id']}/messages").json()
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert "Hello from the agent" in messages[1]["content"]

    updated_workspace = noauth_client.get(
        f"/api/repos/{stack['repo']['id']}/workspaces",
    ).json()[0]
    assert updated_workspace["name"] != updated_workspace["branch"]
    assert "login" in updated_workspace["name"].lower()

    session_state = noauth_client.get(f"/api/sessions/{session['id']}").json()
    assert session_state["status"] == "idle"


def test_error_resilience_journey(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """A failed stream should save partial content and allow the next prompt."""
    stack = create_full_stack(noauth_client, git_repo, name="test-repo")
    session_id = stack["session"]["id"]

    async def failing_query(
        session_id: str,
        prompt: str,
        model: str | None = None,
        cwd: str | None = None,
        api_key: str | None = None,
    ):
        yield {
            "type": "message",
            "data": {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "partial reply"}],
                },
            },
        }
        raise ConnectionError("sidecar died")

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=make_mock_sidecar(failing_query),
    ):
        response = noauth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "Break halfway"},
        )

    assert response.status_code == 200
    first_events = parse_sse_events(response.text)
    assert [event["type"] for event in first_events] == ["assistant", "error"]

    messages = noauth_client.get(f"/api/sessions/{session_id}/messages").json()
    assert messages[-1]["role"] == "assistant"
    assert "partial reply" in messages[-1]["content"]
    assert noauth_client.get(f"/api/sessions/{session_id}").json()["status"] == "idle"

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=_make_streaming_sidecar("Recovered reply"),
    ):
        retry = noauth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "Try again"},
        )

    assert retry.status_code == 200
    retry_events = parse_sse_events(retry.text)
    assert [event["type"] for event in retry_events] == ["assistant", "result"]

    messages = noauth_client.get(f"/api/sessions/{session_id}/messages").json()
    assistant_messages = [message for message in messages if message["role"] == "assistant"]
    assert len(assistant_messages) == 2
    assert "Recovered reply" in assistant_messages[-1]["content"]
    assert noauth_client.get(f"/api/sessions/{session_id}").json()["status"] == "idle"


def test_multi_tenant_isolation_journey(
    auth_client_factory,
    git_repo: str,
) -> None:
    """Tenant users should only see their own repos and sessions."""
    alice = auth_client_factory(email="alice@example.com", provider_user_id="alice-google")
    bob = auth_client_factory(email="bob@example.com", provider_user_id="bob-google")

    alice_stack = create_full_stack(alice, git_repo, name="alice-repo")

    assert bob.get("/api/repos").json() == []
    assert bob.get(f"/api/repos/{alice_stack['repo']['id']}").status_code == 404

    bob_stack = create_full_stack(bob, git_repo, name="bob-repo")

    assert [repo["name"] for repo in alice.get("/api/repos").json()] == ["alice-repo"]
    assert [repo["name"] for repo in bob.get("/api/repos").json()] == ["bob-repo"]
    assert alice.get(f"/api/repos/{bob_stack['repo']['id']}").status_code == 404


def test_authenticated_byok_fallback_journey(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Prompt auth flow should switch between platform and BYOK keys correctly."""
    stack = create_full_stack(auth_client, git_repo, name="test-repo")
    session_id = stack["session"]["id"]

    first_stream = _make_streaming_sidecar("Using platform credit")
    second_stream = _make_streaming_sidecar("Using BYOK")
    third_stream = _make_streaming_sidecar("Back on platform credit")

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        side_effect=[
            _make_model_resolver(),
            first_stream,
            _make_model_resolver(),
            second_stream,
            _make_model_resolver(),
            third_stream,
        ],
    ):
        first = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "Use the default platform key"},
        )
        assert first.status_code == 200
        assert [event["type"] for event in parse_sse_events(first.text)] == ["assistant", "result"]

        create_key = auth_client.post(
            "/api/settings/keys",
            json={
                "provider": "minimax",
                "key": "sk-user-minimax-key",
                "label": "Primary MiniMax",
            },
        )
        assert create_key.status_code == 201
        assert "key" not in create_key.json()

        listed_keys = auth_client.get("/api/settings/keys")
        assert listed_keys.status_code == 200
        assert "sk-user-minimax-key" not in listed_keys.text

        second = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "Use my saved key"},
        )
        assert second.status_code == 200
        assert [event["type"] for event in parse_sse_events(second.text)] == ["assistant", "result"]

        key_id = create_key.json()["id"]
        delete_key = auth_client.delete(f"/api/settings/keys/{key_id}")
        assert delete_key.status_code == 204

        third = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "Fall back to the platform key"},
        )
        assert third.status_code == 200
        assert [event["type"] for event in parse_sse_events(third.text)] == ["assistant", "result"]

    assert first_stream.warmup.call_args.kwargs["api_key"] == "platform-minimax-key"
    assert second_stream.warmup.call_args.kwargs["api_key"] == "sk-user-minimax-key"
    assert third_stream.warmup.call_args.kwargs["api_key"] == "platform-minimax-key"


def test_credit_exhaustion_journey(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Credit exhaustion should block platform prompts until a BYOK key is added."""
    stack = create_full_stack(auth_client, git_repo, name="test-repo")
    session_id = stack["session"]["id"]
    tenant = getattr(auth_client, "yinshi_tenant")

    from yinshi.db import get_control_db
    from yinshi.services.keys import get_credit_remaining_cents

    with get_control_db() as db:
        db.execute(
            "UPDATE users SET credit_limit_cents = 1, credit_used_cents = 0 WHERE id = ?",
            (tenant.user_id,),
        )
        db.commit()

    first_stream = _make_streaming_sidecar(
        "Spent the last free credit",
        usage={"input_tokens": 1_000_000, "output_tokens": 0},
    )
    third_stream = _make_streaming_sidecar("BYOK still works")

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        side_effect=[
            _make_model_resolver(),
            first_stream,
            _make_model_resolver(),
            _make_model_resolver(),
            third_stream,
        ],
    ):
        first = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "Spend the remaining credit"},
        )
        assert first.status_code == 200
        assert get_credit_remaining_cents(tenant.user_id) == 0

        exhausted = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "This should fail without BYOK"},
        )
        assert exhausted.status_code == 402

        add_key = auth_client.post(
            "/api/settings/keys",
            json={
                "provider": "minimax",
                "key": "sk-credit-recovery-key",
                "label": "Recovery key",
            },
        )
        assert add_key.status_code == 201

        recovered = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "This should work with BYOK"},
        )
        assert recovered.status_code == 200
        assert [event["type"] for event in parse_sse_events(recovered.text)] == ["assistant", "result"]

    assert third_stream.warmup.call_args.kwargs["api_key"] == "sk-credit-recovery-key"


def test_workspace_lifecycle_journey(
    noauth_client: TestClient,
    git_repo: str,
) -> None:
    """Archiving, restoring, and deleting a repo should clean up DB rows and worktrees."""
    stack = create_full_stack(noauth_client, git_repo, name="test-repo")
    repo_id = stack["repo"]["id"]
    workspace_id = stack["workspace"]["id"]
    session_id = stack["session"]["id"]
    workspace_path = Path(stack["workspace"]["path"])

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=_make_streaming_sidecar("Lifecycle history"),
    ):
        prompt = noauth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "Create some persisted history"},
        )
    assert prompt.status_code == 200
    assert workspace_path.exists()

    archived = noauth_client.patch(
        f"/api/workspaces/{workspace_id}",
        json={"state": "archived"},
    )
    assert archived.status_code == 200
    assert archived.json()["state"] == "archived"

    restored = noauth_client.patch(
        f"/api/workspaces/{workspace_id}",
        json={"state": "ready"},
    )
    assert restored.status_code == 200
    assert restored.json()["state"] == "ready"

    deleted = noauth_client.delete(f"/api/repos/{repo_id}")
    assert deleted.status_code == 204
    assert not workspace_path.exists()
    assert noauth_client.get(f"/api/repos/{repo_id}").status_code == 404
    assert noauth_client.get(f"/api/sessions/{session_id}/messages").status_code == 404

    from yinshi.db import get_db

    with get_db() as db:
        counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("repos", "workspaces", "sessions", "messages")
        }
    assert counts == {"repos": 0, "workspaces": 0, "sessions": 0, "messages": 0}
