"""Tests for Pi config import and runtime metadata flows.

These tests exercise the public settings API and verify the observable
filesystem and control-plane side effects: scrubbing, instruction mirroring,
stored settings payloads, category toggles, GitHub background import, sync,
removal, and runtime activation.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.conftest import reset_rate_limiter


def _activate_container_runtime() -> None:
    """Mutate cached settings so Pi runtime is active for runtime-path tests."""
    from yinshi.config import get_settings
    from yinshi.main import app

    settings = get_settings()
    settings.container_enabled = True

    class _TestContainerManager:
        """Provide the minimal container interface needed by runtime tests."""

        async def ensure_container(self, user_id: str, data_dir: str) -> SimpleNamespace:
            del user_id, data_dir
            return SimpleNamespace(socket_path="/tmp/test-tenant-sidecar.sock")

        def touch(self, user_id: str) -> None:
            del user_id

        async def destroy_all(self) -> None:
            return None

    app.state.container_manager = _TestContainerManager()


def _build_pi_archive() -> bytes:
    """Create an in-memory Pi config archive with root instructions and settings."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AGENTS.md", "Global instructions")
        archive.writestr("PYTHON.md", "Ignored custom instructions")
        archive.writestr("auth.json", '{"secret": true}')
        archive.writestr("agent/.env", "OPENAI_API_KEY=sk-upload-secret\n")
        archive.writestr("agent/credentials.json", '{"apiKey": "sk-upload-credentials"}')
        archive.writestr(".git/HEAD", "ref: refs/heads/main\n")
        archive.writestr("agent/extensions/hook.js", "export default {};\n")
        archive.writestr(
            "agent/settings.json",
            json.dumps(
                {
                    "retry": {"enabled": False},
                    "packages": ["pi-skills"],
                    "provider": {
                        "apiKey": "sk-upload-provider",
                        "accessToken": "upload-access-token",
                        "nested": {"clientSecret": "nested-secret"},
                        "baseUrl": "https://api.example.com",
                    },
                }
            ),
        )
        archive.writestr("agent/models.json", json.dumps({"providers": []}))
    return buffer.getvalue()


def _get_user_settings_row(user_id: str) -> dict[str, Any] | None:
    """Return the stored user_settings row for a user."""
    from yinshi.db import get_control_db

    with get_control_db() as db:
        row = db.execute(
            "SELECT pi_settings_json, pi_settings_enabled FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def _create_fake_pi_clone(dest: str, *, instruction_name: str = "CLAUDE.md") -> str:
    """Write a fake cloned Pi config tree at the destination path."""
    dest_path = Path(dest)
    (dest_path / "agent" / "prompts").mkdir(parents=True, exist_ok=True)
    (dest_path / ".env.production").write_text(
        "ANTHROPIC_API_KEY=sk-github-secret\n", encoding="utf-8"
    )
    (dest_path / "agent" / "settings.json").write_text(
        json.dumps(
            {
                "retry": {"enabled": False},
                "provider": {
                    "apiKey": "sk-github-provider",
                    "refreshToken": "github-refresh-token",
                    "region": "us",
                },
            }
        ),
        encoding="utf-8",
    )
    (dest_path / "agent" / "oauth.json").write_text(
        json.dumps({"accessToken": "oauth-secret"}),
        encoding="utf-8",
    )
    (dest_path / instruction_name).write_text("Imported instructions", encoding="utf-8")
    (dest_path / ".git").mkdir(exist_ok=True)
    return dest


def _store_minimax_api_key(user_id: str) -> None:
    """Store one test API key so prompt execution can resolve provider auth."""
    from yinshi.db import get_control_db
    from yinshi.services.crypto import encrypt_api_key
    from yinshi.services.keys import get_user_dek

    dek = get_user_dek(user_id)
    encrypted_key = encrypt_api_key("sk-user-minimax-key", dek)
    with get_control_db() as db:
        db.execute(
            "INSERT INTO api_keys (user_id, provider, encrypted_key) VALUES (?, ?, ?)",
            (user_id, "minimax", encrypted_key),
        )
        db.commit()


def test_get_pi_config_returns_404_when_missing(auth_client: TestClient) -> None:
    """GET /api/settings/pi-config should return 404 before any import."""
    response = auth_client.get("/api/settings/pi-config")
    assert response.status_code == 404


def test_upload_pi_config_scrubs_and_stores_metadata(auth_client: TestClient) -> None:
    """Upload should scrub unsafe files, mirror instructions, and persist settings."""
    tenant = getattr(auth_client, "yinshi_tenant")

    response = auth_client.post(
        "/api/settings/pi-config/upload",
        files={"file": ("pi-config.zip", _build_pi_archive(), "application/zip")},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["available_categories"] == ["extensions", "settings", "models", "instructions"]
    assert payload["enabled_categories"] == payload["available_categories"]

    config_root = Path(tenant.data_dir) / "pi-config"
    assert not (config_root / "auth.json").exists()
    assert not (config_root / "agent" / ".env").exists()
    assert not (config_root / "agent" / "credentials.json").exists()
    assert not (config_root / ".git").exists()
    assert (config_root / "AGENTS.md").is_file()
    assert (config_root / "agent" / "AGENTS.md").is_file()
    assert not (config_root / "agent" / "PYTHON.md").exists()
    scrubbed_settings = json.loads(
        (config_root / "agent" / "settings.json").read_text(encoding="utf-8")
    )
    assert scrubbed_settings == {
        "provider": {"baseUrl": "https://api.example.com", "nested": {}},
        "retry": {"enabled": False},
    }

    user_settings_row = _get_user_settings_row(tenant.user_id)
    assert user_settings_row is not None
    assert user_settings_row["pi_settings_enabled"] == 1
    stored_settings = json.loads(user_settings_row["pi_settings_json"])
    assert stored_settings == {
        "provider": {"baseUrl": "https://api.example.com", "nested": {}},
        "retry": {"enabled": False},
    }


def test_upload_pi_config_rejects_oversized_files_before_import(
    auth_client: TestClient,
    monkeypatch,
) -> None:
    """Upload should reject oversized archives before calling the import service."""
    from yinshi.api import settings as settings_api

    import_mock = AsyncMock()
    monkeypatch.setattr(settings_api, "_MAX_UPLOAD_BYTES", 8)
    monkeypatch.setattr(settings_api, "import_from_upload", import_mock)

    response = auth_client.post(
        "/api/settings/pi-config/upload",
        files={"file": ("pi-config.zip", b"123456789", "application/zip")},
    )

    assert response.status_code == 413
    import_mock.assert_not_called()


def test_update_pi_config_categories_renames_paths_and_disables_settings(
    auth_client: TestClient,
) -> None:
    """PATCH should rename disabled paths and stop forwarding imported settings."""
    tenant = getattr(auth_client, "yinshi_tenant")
    upload_response = auth_client.post(
        "/api/settings/pi-config/upload",
        files={"file": ("pi-config.zip", _build_pi_archive(), "application/zip")},
    )
    assert upload_response.status_code == 201

    response = auth_client.patch(
        "/api/settings/pi-config/categories",
        json={"enabled_categories": []},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled_categories"] == []

    config_root = Path(tenant.data_dir) / "pi-config"
    assert (config_root / "agent" / "extensions.disabled").is_dir()
    assert (config_root / "agent" / "settings.json.disabled").is_file()
    assert (config_root / "agent" / "AGENTS.md.disabled").is_file()

    user_settings_row = _get_user_settings_row(tenant.user_id)
    assert user_settings_row is not None
    assert user_settings_row["pi_settings_enabled"] == 0


def test_github_import_clones_in_background_and_keeps_git_metadata(
    auth_client: TestClient,
) -> None:
    """GitHub import should return cloning immediately and finish with mirrored files."""
    tenant = getattr(auth_client, "yinshi_tenant")

    async def fake_clone_repo(url: str, dest: str, access_token: str | None = None) -> str:
        assert url == "https://github.com/example/pi-config.git"
        assert access_token is None
        return _create_fake_pi_clone(dest)

    with patch("yinshi.services.pi_config.clone_repo", side_effect=fake_clone_repo):
        response = auth_client.post(
            "/api/settings/pi-config/github",
            json={"repo_url": "https://github.com/example/pi-config.git"},
        )

    assert response.status_code == 201
    assert response.json()["status"] == "cloning"

    config_response = auth_client.get("/api/settings/pi-config")
    assert config_response.status_code == 200
    payload = config_response.json()
    assert payload["status"] == "ready"
    assert payload["available_categories"] == ["prompts", "settings", "instructions"]

    config_root = Path(tenant.data_dir) / "pi-config"
    assert (config_root / ".git").is_dir()
    assert (config_root / "agent" / "CLAUDE.md").is_file()
    assert not (config_root / ".env.production").exists()
    assert not (config_root / "agent" / "oauth.json").exists()
    scrubbed_settings = json.loads(
        (config_root / "agent" / "settings.json").read_text(encoding="utf-8")
    )
    assert scrubbed_settings == {
        "provider": {"region": "us"},
        "retry": {"enabled": False},
    }


def test_prompt_forwards_agent_dir_and_settings_payload(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Prompt execution should forward the imported agentDir and sanitized settings."""
    from tests.factories import create_full_stack, make_mock_sidecar

    _activate_container_runtime()
    tenant = getattr(auth_client, "yinshi_tenant")
    upload_response = auth_client.post(
        "/api/settings/pi-config/upload",
        files={"file": ("pi-config.zip", _build_pi_archive(), "application/zip")},
    )
    assert upload_response.status_code == 201

    stack = create_full_stack(auth_client, git_repo, name="prompt-test")
    session_id = stack["session"]["id"]

    _store_minimax_api_key(tenant.user_id)

    async def fake_query(
        sid: str,
        prompt: str,
        model: str | None = None,
        cwd: str | None = None,
        api_key: str | None = None,
        agent_dir: str | None = None,
        settings_payload: dict[str, object] | None = None,
    ):
        yield {"type": "message", "data": {"type": "result", "usage": {}}}

    mock_sidecar = make_mock_sidecar(fake_query)
    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=mock_sidecar,
    ):
        response = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "run the imported config"},
        )

    assert response.status_code == 200
    assert mock_sidecar.warmup.call_args.kwargs["agent_dir"] == "/data/pi-config/agent"
    assert mock_sidecar.warmup.call_args.kwargs["settings_payload"] == {
        "provider": {"baseUrl": "https://api.example.com", "nested": {}},
        "retry": {"enabled": False},
    }


def test_repo_agents_override_preserves_imported_runtime_state(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Repo-level AGENTS should overlay on top of the imported Pi runtime."""
    from tests.factories import create_full_stack, make_mock_sidecar

    _activate_container_runtime()
    tenant = getattr(auth_client, "yinshi_tenant")
    upload_response = auth_client.post(
        "/api/settings/pi-config/upload",
        files={"file": ("pi-config.zip", _build_pi_archive(), "application/zip")},
    )
    assert upload_response.status_code == 201

    stack = create_full_stack(
        auth_client,
        git_repo,
        name="repo-override",
        agents_md="Repo instructions",
    )
    session_id = stack["session"]["id"]
    _store_minimax_api_key(tenant.user_id)

    async def fake_query(
        sid: str,
        prompt: str,
        model: str | None = None,
        cwd: str | None = None,
        api_key: str | None = None,
        agent_dir: str | None = None,
        settings_payload: dict[str, object] | None = None,
    ):
        del sid, prompt, model, cwd, api_key, agent_dir, settings_payload
        yield {"type": "message", "data": {"type": "result", "usage": {}}}

    mock_sidecar = make_mock_sidecar(fake_query)
    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=mock_sidecar,
    ):
        response = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "run the repo override"},
        )

    assert response.status_code == 200
    assert mock_sidecar.warmup.call_args.kwargs["agent_dir"] == (
        f"/data/pi-runtime/sessions/{session_id}/agent"
    )
    assert mock_sidecar.warmup.call_args.kwargs["settings_payload"] == {
        "provider": {"baseUrl": "https://api.example.com", "nested": {}},
        "retry": {"enabled": False},
    }

    runtime_agent_dir = Path(tenant.data_dir) / "pi-runtime" / "sessions" / session_id / "agent"
    assert runtime_agent_dir.is_dir()
    assert (runtime_agent_dir / "AGENTS.md").read_text(encoding="utf-8") == "Repo instructions"
    assert (runtime_agent_dir / "extensions").is_symlink()
    assert (runtime_agent_dir / "models.json").is_symlink()

    imported_agent_dir = Path(tenant.data_dir) / "pi-config" / "agent"
    assert (imported_agent_dir / "AGENTS.md").read_text(encoding="utf-8") == "Global instructions"


def test_repo_agents_override_without_pi_config_creates_minimal_runtime(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Repo-level AGENTS should still reach the sidecar without a global Pi config."""
    from tests.factories import create_full_stack, make_mock_sidecar

    _activate_container_runtime()
    tenant = getattr(auth_client, "yinshi_tenant")
    stack = create_full_stack(
        auth_client,
        git_repo,
        name="repo-only-override",
        agents_md="Repo only instructions",
    )
    session_id = stack["session"]["id"]
    _store_minimax_api_key(tenant.user_id)

    async def fake_query(
        sid: str,
        prompt: str,
        model: str | None = None,
        cwd: str | None = None,
        api_key: str | None = None,
        agent_dir: str | None = None,
        settings_payload: dict[str, object] | None = None,
    ):
        del sid, prompt, model, cwd, api_key, agent_dir, settings_payload
        yield {"type": "message", "data": {"type": "result", "usage": {}}}

    mock_sidecar = make_mock_sidecar(fake_query)
    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=mock_sidecar,
    ):
        response = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "run the repo instructions only"},
        )

    assert response.status_code == 200
    assert mock_sidecar.warmup.call_args.kwargs["agent_dir"] == (
        f"/data/pi-runtime/sessions/{session_id}/agent"
    )
    assert mock_sidecar.warmup.call_args.kwargs["settings_payload"] is None

    runtime_agent_dir = Path(tenant.data_dir) / "pi-runtime" / "sessions" / session_id / "agent"
    assert runtime_agent_dir.is_dir()
    assert [path.name for path in runtime_agent_dir.iterdir()] == ["AGENTS.md"]
    assert (runtime_agent_dir / "AGENTS.md").read_text(encoding="utf-8") == (
        "Repo only instructions"
    )


def test_repo_agents_override_uses_distinct_session_runtime_dirs(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Each session should get a distinct runtime overlay directory."""
    from tests.factories import create_full_stack, make_mock_sidecar

    _activate_container_runtime()
    tenant = getattr(auth_client, "yinshi_tenant")
    _store_minimax_api_key(tenant.user_id)

    first_stack = create_full_stack(
        auth_client,
        git_repo,
        name="first-override",
        agents_md="First repo instructions",
    )
    second_stack = create_full_stack(
        auth_client,
        git_repo,
        name="second-override",
        agents_md="Second repo instructions",
    )

    async def fake_query(
        sid: str,
        prompt: str,
        model: str | None = None,
        cwd: str | None = None,
        api_key: str | None = None,
        agent_dir: str | None = None,
        settings_payload: dict[str, object] | None = None,
    ):
        del sid, prompt, model, cwd, api_key, agent_dir, settings_payload
        yield {"type": "message", "data": {"type": "result", "usage": {}}}

    first_sidecar = make_mock_sidecar(fake_query)
    second_sidecar = make_mock_sidecar(fake_query)

    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=first_sidecar,
    ):
        first_response = auth_client.post(
            f"/api/sessions/{first_stack['session']['id']}/prompt",
            json={"prompt": "first prompt"},
        )
    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=second_sidecar,
    ):
        second_response = auth_client.post(
            f"/api/sessions/{second_stack['session']['id']}/prompt",
            json={"prompt": "second prompt"},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_sidecar.warmup.call_args.kwargs["agent_dir"] != (
        second_sidecar.warmup.call_args.kwargs["agent_dir"]
    )

    first_agent_dir = (
        Path(tenant.data_dir) / "pi-runtime" / "sessions" / first_stack["session"]["id"] / "agent"
    )
    second_agent_dir = (
        Path(tenant.data_dir) / "pi-runtime" / "sessions" / second_stack["session"]["id"] / "agent"
    )

    assert first_agent_dir != second_agent_dir
    assert (first_agent_dir / "AGENTS.md").read_text(encoding="utf-8") == (
        "First repo instructions"
    )
    assert (second_agent_dir / "AGENTS.md").read_text(encoding="utf-8") == (
        "Second repo instructions"
    )


def test_sync_pi_config_refreshes_categories_and_instruction_mirror(
    auth_client: TestClient,
) -> None:
    """Sync should refresh mirrored instructions and newly discovered categories."""
    tenant = getattr(auth_client, "yinshi_tenant")
    config_root = Path(tenant.data_dir) / "pi-config"
    remote_state = {"prompts": False}

    async def fake_clone_repo(url: str, dest: str, access_token: str | None = None) -> str:
        dest_path = Path(dest)
        (dest_path / "agent").mkdir(parents=True, exist_ok=True)
        (dest_path / ".git").mkdir(exist_ok=True)
        (dest_path / "AGENTS.md").write_text("Synced instructions", encoding="utf-8")
        (dest_path / "agent" / "settings.json").write_text(
            json.dumps({"retry": {"enabled": False}}),
            encoding="utf-8",
        )
        if remote_state["prompts"]:
            (dest_path / "agent" / "prompts").mkdir(parents=True, exist_ok=True)
            (dest_path / "agent" / "prompts" / "plan.md").write_text(
                "Prompt text",
                encoding="utf-8",
            )
        return dest

    with patch("yinshi.services.pi_config.clone_repo", side_effect=fake_clone_repo):
        create_response = auth_client.post(
            "/api/settings/pi-config/github",
            json={"repo_url": "https://github.com/example/pi-config.git"},
        )
    assert create_response.status_code == 201

    (config_root / "agent" / "AGENTS.md").unlink(missing_ok=True)
    remote_state["prompts"] = True
    with (
        patch("yinshi.services.pi_config.clone_repo", side_effect=fake_clone_repo),
        patch(
            "yinshi.services.pi_config._run_git",
            new=AsyncMock(return_value=""),
        ),
    ):
        sync_response = auth_client.post("/api/settings/pi-config/sync")

    assert sync_response.status_code == 200
    payload = sync_response.json()
    assert payload["status"] == "ready"
    assert payload["available_categories"] == ["prompts", "settings", "instructions"]
    assert (config_root / "agent" / "AGENTS.md").is_file()


def test_sync_pi_config_reapplies_disabled_categories_without_collision(
    auth_client: TestClient,
) -> None:
    """Sync should recreate disabled artifacts instead of failing on stale .disabled paths."""
    tenant = getattr(auth_client, "yinshi_tenant")
    config_root = Path(tenant.data_dir) / "pi-config"

    async def fake_clone_repo(url: str, dest: str, access_token: str | None = None) -> str:
        dest_path = Path(dest)
        (dest_path / "agent").mkdir(parents=True, exist_ok=True)
        (dest_path / ".git").mkdir(exist_ok=True)
        (dest_path / "AGENTS.md").write_text("Synced instructions", encoding="utf-8")
        (dest_path / "agent" / "settings.json").write_text(
            json.dumps({"retry": {"enabled": False}}),
            encoding="utf-8",
        )
        return dest

    with patch("yinshi.services.pi_config.clone_repo", side_effect=fake_clone_repo):
        create_response = auth_client.post(
            "/api/settings/pi-config/github",
            json={"repo_url": "https://github.com/example/pi-config.git"},
        )
    assert create_response.status_code == 201

    disable_response = auth_client.patch(
        "/api/settings/pi-config/categories",
        json={"enabled_categories": []},
    )
    assert disable_response.status_code == 200
    assert (config_root / "agent" / "settings.json.disabled").is_file()
    assert (config_root / "agent" / "AGENTS.md.disabled").is_file()

    with (
        patch("yinshi.services.pi_config.clone_repo", side_effect=fake_clone_repo),
        patch(
            "yinshi.services.pi_config._run_git",
            new=AsyncMock(return_value=""),
        ),
    ):
        sync_response = auth_client.post("/api/settings/pi-config/sync")

    assert sync_response.status_code == 200
    payload = sync_response.json()
    assert payload["enabled_categories"] == []
    assert (config_root / "agent" / "settings.json.disabled").is_file()
    assert (config_root / "agent" / "AGENTS.md.disabled").is_file()


def test_sync_pi_config_removes_stale_instruction_mirror_when_source_is_deleted(
    auth_client: TestClient,
) -> None:
    """Sync should delete the mirrored instruction file when the root source disappears."""
    tenant = getattr(auth_client, "yinshi_tenant")
    config_root = Path(tenant.data_dir) / "pi-config"
    remote_state = {"instructions": True}

    async def fake_clone_repo(url: str, dest: str, access_token: str | None = None) -> str:
        dest_path = Path(dest)
        (dest_path / "agent").mkdir(parents=True, exist_ok=True)
        (dest_path / ".git").mkdir(exist_ok=True)
        (dest_path / "agent" / "settings.json").write_text(
            json.dumps({"retry": {"enabled": False}}),
            encoding="utf-8",
        )
        instruction_path = dest_path / "AGENTS.md"
        if remote_state["instructions"]:
            instruction_path.write_text("Synced instructions", encoding="utf-8")
        else:
            instruction_path.unlink(missing_ok=True)
        return dest

    with patch("yinshi.services.pi_config.clone_repo", side_effect=fake_clone_repo):
        create_response = auth_client.post(
            "/api/settings/pi-config/github",
            json={"repo_url": "https://github.com/example/pi-config.git"},
        )
    assert create_response.status_code == 201
    assert (config_root / "agent" / "AGENTS.md").is_file()

    remote_state["instructions"] = False
    with (
        patch("yinshi.services.pi_config.clone_repo", side_effect=fake_clone_repo),
        patch(
            "yinshi.services.pi_config._run_git",
            new=AsyncMock(return_value=""),
        ),
    ):
        sync_response = auth_client.post("/api/settings/pi-config/sync")

    assert sync_response.status_code == 200
    payload = sync_response.json()
    assert payload["available_categories"] == ["settings"]
    assert not (config_root / "agent" / "AGENTS.md").exists()
    assert not (config_root / "agent" / "AGENTS.md.disabled").exists()


def test_delete_pi_config_removes_files_and_settings(auth_client: TestClient) -> None:
    """DELETE should remove the imported config directory and stored settings row."""
    tenant = getattr(auth_client, "yinshi_tenant")
    upload_response = auth_client.post(
        "/api/settings/pi-config/upload",
        files={"file": ("pi-config.zip", _build_pi_archive(), "application/zip")},
    )
    assert upload_response.status_code == 201

    delete_response = auth_client.delete("/api/settings/pi-config")
    assert delete_response.status_code == 204

    config_root = Path(tenant.data_dir) / "pi-config"
    assert not config_root.exists()
    assert _get_user_settings_row(tenant.user_id) is None


def test_prompt_does_not_forward_pi_settings_when_runtime_is_inactive(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Prompt execution should not forward Pi settings when container isolation is off."""
    from tests.factories import create_full_stack, make_mock_sidecar

    _activate_container_runtime()
    tenant = getattr(auth_client, "yinshi_tenant")
    upload_response = auth_client.post(
        "/api/settings/pi-config/upload",
        files={"file": ("pi-config.zip", _build_pi_archive(), "application/zip")},
    )
    assert upload_response.status_code == 201

    from yinshi.config import get_settings

    settings = get_settings()
    original_container_enabled = settings.container_enabled
    settings.container_enabled = False

    stack = create_full_stack(auth_client, git_repo, name="prompt-test")
    session_id = stack["session"]["id"]

    from yinshi.db import get_control_db
    from yinshi.services.crypto import encrypt_api_key
    from yinshi.services.keys import get_user_dek

    dek = get_user_dek(tenant.user_id)
    encrypted_key = encrypt_api_key("sk-user-minimax-key", dek)
    with get_control_db() as db:
        db.execute(
            "INSERT INTO api_keys (user_id, provider, encrypted_key) VALUES (?, ?, ?)",
            (tenant.user_id, "minimax", encrypted_key),
        )
        db.commit()

    async def fake_query(
        sid: str,
        prompt: str,
        model: str | None = None,
        cwd: str | None = None,
        api_key: str | None = None,
        agent_dir: str | None = None,
        settings_payload: dict[str, object] | None = None,
    ):
        yield {"type": "message", "data": {"type": "result", "usage": {}}}

    mock_sidecar = make_mock_sidecar(fake_query)
    try:
        with patch(
            "yinshi.api.stream.create_sidecar_connection",
            return_value=mock_sidecar,
        ):
            response = auth_client.post(
                f"/api/sessions/{session_id}/prompt",
                json={"prompt": "run without pi runtime"},
            )
    finally:
        settings.container_enabled = original_container_enabled

    assert response.status_code == 200
    assert mock_sidecar.warmup.call_args.kwargs["agent_dir"] is None
    assert mock_sidecar.warmup.call_args.kwargs["settings_payload"] is None


def test_upload_pi_config_rate_limit_returns_429(auth_client: TestClient) -> None:
    """Pi config uploads should be limited per authenticated user."""
    reset_rate_limiter()
    for index in range(10):
        response = auth_client.post(
            "/api/settings/pi-config/upload",
            files={
                "file": (
                    f"pi-config-{index}.zip",
                    _build_pi_archive(),
                    "application/zip",
                )
            },
        )
        assert response.status_code in {201, 409}

    limited_response = auth_client.post(
        "/api/settings/pi-config/upload",
        files={"file": ("pi-config.zip", _build_pi_archive(), "application/zip")},
    )

    assert limited_response.status_code == 429
    reset_rate_limiter()


def test_upload_validation_error_remains_user_visible(auth_client: TestClient) -> None:
    """Safe upload validation errors should still pass through to the API caller."""
    response = auth_client.post(
        "/api/settings/pi-config/upload",
        files={"file": ("pi-config.zip", b"not-a-zip", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded file is not a zip archive"


def test_github_import_failure_message_is_sanitized(auth_client: TestClient) -> None:
    """Background import failures should store a generic user-facing error message."""
    from yinshi.services.pi_config import (
        _finalize_github_import,
        _insert_pi_config_row,
        get_pi_config,
    )

    tenant = getattr(auth_client, "yinshi_tenant")
    _insert_pi_config_row(
        tenant.user_id,
        source_type="github",
        source_label="example",
        repo_url="https://github.com/example/pi-config.git",
        status="cloning",
        available_categories=[],
        enabled_categories=[],
    )

    async def fake_clone_repo(url: str, dest: str, access_token: str | None = None) -> str:
        raise RuntimeError("git clone leaked /private/tmp/path")

    with patch("yinshi.services.pi_config.clone_repo", side_effect=fake_clone_repo):
        asyncio.run(
            _finalize_github_import(
                tenant.user_id,
                tenant.data_dir,
                "https://github.com/example/pi-config.git",
                "example",
                None,
            )
        )

    payload = get_pi_config(tenant.user_id)
    assert payload is not None
    assert payload["status"] == "error"
    assert payload["error_message"] == "Import failed. Check server logs for details."


def test_sync_failure_message_is_sanitized(auth_client: TestClient) -> None:
    """Pi config sync failures should store a generic user-facing error message."""
    from yinshi.services.pi_config import _insert_pi_config_row, get_pi_config, sync_pi_config

    tenant = getattr(auth_client, "yinshi_tenant")
    _insert_pi_config_row(
        tenant.user_id,
        source_type="github",
        source_label="example",
        repo_url="https://github.com/example/pi-config.git",
        status="ready",
        available_categories=[],
        enabled_categories=[],
    )

    async def fake_clone_repo(url: str, dest: str, access_token: str | None = None) -> str:
        raise RuntimeError("git clone leaked /private/tmp/path")

    with patch("yinshi.services.pi_config.clone_repo", side_effect=fake_clone_repo):
        try:
            asyncio.run(sync_pi_config(tenant.user_id, tenant.data_dir))
        except RuntimeError:
            pass

    payload = get_pi_config(tenant.user_id)
    assert payload is not None
    assert payload["status"] == "error"
    assert payload["error_message"] == "Sync failed. Check server logs for details."


def test_extract_archive_counts_actual_decompressed_bytes(tmp_path) -> None:
    """Extraction should enforce limits using bytes read from the stream."""
    from yinshi.exceptions import PiConfigError
    from yinshi.services import pi_config

    class FakeMember:
        """Minimal archive member for the zip extraction test."""

        def __init__(self) -> None:
            self.filename = "agent/settings.json"
            self.file_size = 1
            self.external_attr = 0

        def is_dir(self) -> bool:
            return False

    class FakeArchive:
        """Archive whose stream expands beyond the trusted header size."""

        def __enter__(self) -> "FakeArchive":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def infolist(self) -> list[FakeMember]:
            return [FakeMember()]

        def open(self, member: FakeMember) -> io.BytesIO:
            return io.BytesIO(b"x" * 32)

    temp_root = tmp_path / "extract-root"
    temp_root.mkdir()

    with (
        patch("yinshi.services.pi_config.zipfile.ZipFile", return_value=FakeArchive()),
        patch(
            "yinshi.services.pi_config._MAX_EXTRACTED_BYTES",
            16,
        ),
    ):
        try:
            pi_config._extract_archive(b"PK\x03\x04", temp_root)
        except PiConfigError as exc:
            assert str(exc) == "Archive expands beyond the allowed size limit"
        else:
            raise AssertionError("Expected PiConfigError for oversized extracted content")
