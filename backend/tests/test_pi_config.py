"""Tests for Pi config import and runtime metadata flows.

These tests exercise the public settings API and verify the observable
filesystem and control-plane side effects: scrubbing, instruction mirroring,
stored settings payloads, category toggles, GitHub background import, sync,
removal, and container gating.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def _enable_container_support() -> None:
    """Mutate cached settings so Pi config endpoints are available."""
    from yinshi.config import get_settings

    settings = get_settings()
    settings.container_enabled = True


def _build_pi_archive() -> bytes:
    """Create an in-memory Pi config archive with root instructions and settings."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AGENTS.md", "Global instructions")
        archive.writestr("PYTHON.md", "Ignored custom instructions")
        archive.writestr("auth.json", "{\"secret\": true}")
        archive.writestr(".git/HEAD", "ref: refs/heads/main\n")
        archive.writestr("agent/extensions/hook.js", "export default {};\n")
        archive.writestr(
            "agent/settings.json",
            json.dumps({
                "retry": {"enabled": False},
                "packages": ["pi-skills"],
            }),
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
    (dest_path / "agent" / "settings.json").write_text(
        json.dumps({"retry": {"enabled": False}}),
        encoding="utf-8",
    )
    (dest_path / instruction_name).write_text("Imported instructions", encoding="utf-8")
    (dest_path / ".git").mkdir(exist_ok=True)
    return dest


def test_get_pi_config_returns_404_when_missing(auth_client: TestClient) -> None:
    """GET /api/settings/pi-config should return 404 before any import."""
    response = auth_client.get("/api/settings/pi-config")
    assert response.status_code == 404


def test_upload_pi_config_scrubs_and_stores_metadata(auth_client: TestClient) -> None:
    """Upload should scrub unsafe files, mirror instructions, and persist settings."""
    _enable_container_support()
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
    assert not (config_root / ".git").exists()
    assert (config_root / "AGENTS.md").is_file()
    assert (config_root / "agent" / "AGENTS.md").is_file()
    assert not (config_root / "agent" / "PYTHON.md").exists()

    user_settings_row = _get_user_settings_row(tenant.user_id)
    assert user_settings_row is not None
    assert user_settings_row["pi_settings_enabled"] == 1
    stored_settings = json.loads(user_settings_row["pi_settings_json"])
    assert stored_settings == {"retry": {"enabled": False}}


def test_update_pi_config_categories_renames_paths_and_disables_settings(
    auth_client: TestClient,
) -> None:
    """PATCH should rename disabled paths and stop forwarding imported settings."""
    _enable_container_support()
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
    _enable_container_support()
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


def test_prompt_forwards_agent_dir_and_settings_payload(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Prompt execution should forward the imported agentDir and sanitized settings."""
    from tests.factories import create_full_stack, make_mock_sidecar

    _enable_container_support()
    tenant = getattr(auth_client, "yinshi_tenant")
    upload_response = auth_client.post(
        "/api/settings/pi-config/upload",
        files={"file": ("pi-config.zip", _build_pi_archive(), "application/zip")},
    )
    assert upload_response.status_code == 201

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
    with patch(
        "yinshi.api.stream.create_sidecar_connection",
        return_value=mock_sidecar,
    ):
        response = auth_client.post(
            f"/api/sessions/{session_id}/prompt",
            json={"prompt": "run the imported config"},
        )

    assert response.status_code == 200
    assert mock_sidecar.warmup.call_args.kwargs["agent_dir"] == str(
        Path(tenant.data_dir) / "pi-config" / "agent"
    )
    assert mock_sidecar.warmup.call_args.kwargs["settings_payload"] == {
        "retry": {"enabled": False},
    }


def test_sync_pi_config_refreshes_categories_and_instruction_mirror(
    auth_client: TestClient,
) -> None:
    """Sync should refresh mirrored instructions and newly discovered categories."""
    _enable_container_support()
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
    with patch("yinshi.services.pi_config.clone_repo", side_effect=fake_clone_repo), patch(
        "yinshi.services.pi_config._run_git",
        new=AsyncMock(return_value=""),
    ):
        sync_response = auth_client.post("/api/settings/pi-config/sync")

    assert sync_response.status_code == 200
    payload = sync_response.json()
    assert payload["status"] == "ready"
    assert payload["available_categories"] == ["prompts", "settings", "instructions"]
    assert (config_root / "agent" / "AGENTS.md").is_file()


def test_delete_pi_config_removes_files_and_settings(auth_client: TestClient) -> None:
    """DELETE should remove the imported config directory and stored settings row."""
    _enable_container_support()
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


def test_pi_config_import_rejected_when_container_support_is_disabled(
    auth_client: TestClient,
) -> None:
    """Import should be rejected until container isolation is enabled."""
    response = auth_client.post(
        "/api/settings/pi-config/upload",
        files={"file": ("pi-config.zip", _build_pi_archive(), "application/zip")},
    )
    assert response.status_code == 409
    assert "container isolation" in response.json()["detail"]
