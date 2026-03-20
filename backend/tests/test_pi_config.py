"""Tests for Pi config import and runtime metadata flows.

These tests exercise the public settings API and verify the observable
filesystem and control-plane side effects: scrubbing, instruction mirroring,
stored settings payloads, category toggles, GitHub background import, sync,
removal, and container gating.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.conftest import reset_rate_limiter


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
        archive.writestr("auth.json", '{"secret": true}')
        archive.writestr(".git/HEAD", "ref: refs/heads/main\n")
        archive.writestr("agent/extensions/hook.js", "export default {};\n")
        archive.writestr(
            "agent/settings.json",
            json.dumps(
                {
                    "retry": {"enabled": False},
                    "packages": ["pi-skills"],
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
    _enable_container_support()
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
    _enable_container_support()
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


def test_prompt_does_not_forward_pi_settings_when_runtime_is_inactive(
    auth_client: TestClient,
    git_repo: str,
) -> None:
    """Prompt execution should not forward Pi settings when container isolation is off."""
    from tests.factories import create_full_stack, make_mock_sidecar

    _enable_container_support()
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
    _enable_container_support()

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
    _enable_container_support()

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

    _enable_container_support()
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
