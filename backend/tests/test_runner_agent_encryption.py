"""Verify runner-agent user storage encryption configuration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from yinshi import runner_agent


def _set_runner_agent_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point runner-agent paths at one isolated directory."""
    monkeypatch.setenv("YINSHI_CONTROL_URL", "https://control.example")
    monkeypatch.setenv("YINSHI_RUNNER_TOKEN_FILE", str(tmp_path / "runner-token"))
    monkeypatch.setenv("YINSHI_RUNNER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YINSHI_RUNNER_SQLITE_DIR", str(tmp_path / "sqlite"))
    monkeypatch.setenv("YINSHI_RUNNER_SHARED_FILES_DIR", str(tmp_path / "shared"))


def test_runner_agent_defaults_user_storage_encryption_to_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """BYOC runner configuration keeps user storage enforcement disabled by default."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.delenv("YINSHI_RUNNER_USER_DATA_ENCRYPTION", raising=False)

    config = runner_agent.load_config()

    assert config.user_data_encryption == "disabled"


def test_runner_agent_accepts_explicit_disabled_user_storage_encryption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """BYOC runner configuration accepts an explicit disabled mode."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_USER_DATA_ENCRYPTION", "disabled")

    config = runner_agent.load_config()

    assert config.user_data_encryption == "disabled"


def test_runner_agent_rejects_empty_user_storage_encryption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An explicitly empty managed-storage mode fails closed."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_USER_DATA_ENCRYPTION", "")

    with pytest.raises(
        RuntimeError,
        match="YINSHI_RUNNER_USER_DATA_ENCRYPTION must be disabled or required",
    ):
        runner_agent.load_config()


def test_runner_agent_rejects_invalid_user_storage_encryption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Runner configuration rejects unsupported managed-storage modes."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_USER_DATA_ENCRYPTION", "optional")

    with pytest.raises(
        RuntimeError,
        match="YINSHI_RUNNER_USER_DATA_ENCRYPTION must be disabled or required",
    ):
        runner_agent.load_config()


@pytest.mark.asyncio
async def test_registration_and_heartbeat_logs_exclude_private_runner_data(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Runner HTTP lifecycle logs contain fixed events, not private values."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    registration_token = "registration-token-private"
    runner_token = "bearer-token-private"
    runner_id = "runner-id-private"
    user_id = "user-id-private"
    provider_body = "provider-body-private"
    monkeypatch.setenv("YINSHI_REGISTRATION_TOKEN", registration_token)
    signing_key = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"

    async def handler(request: httpx.Request) -> httpx.Response:
        status_code = 201 if request.url.path == "/runner/register" else 200
        return httpx.Response(
            status_code,
            json={
                "runner_id": runner_id,
                "runner_token": runner_token,
                "user_id": user_id,
                "provider_body": provider_body,
                "capability_signing_public_key": signing_key,
                "status": "online",
            },
        )

    config = runner_agent.load_config()
    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(
        base_url=config.control_url,
        transport=httpx.MockTransport(handler),
    ) as client:
        issued_token = await runner_agent._register(config, client)
        await runner_agent._heartbeat(config, client, issued_token)

    private_values = (
        registration_token,
        runner_token,
        runner_id,
        user_id,
        provider_body,
        str(config.data_dir),
        str(config.sqlite_dir),
        str(config.shared_files_dir),
    )
    for record in caplog.records:
        rendered_record = f"{record.getMessage()} {record.args!r}"
        assert all(value not in rendered_record for value in private_values)
    assert "Registered Yinshi cloud runner" in caplog.text
    assert "Heartbeat accepted for Yinshi cloud runner" in caplog.text


@pytest.mark.asyncio
async def test_runner_agent_passes_required_user_storage_mode_to_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Managed guest configuration reaches the worker manager unchanged."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_USER_DATA_ENCRYPTION", "required")
    manager_arguments: dict[str, Any] = {}

    @dataclass(frozen=True)
    class NoiseKeypair:
        private_key: bytes = b"r" * 32

    class WorkerManager:
        def __init__(self, **kwargs: Any) -> None:
            manager_arguments.update(kwargs)

    async def stop_relay(*args: object) -> None:
        raise RuntimeError("stop test relay")

    monkeypatch.setattr(
        runner_agent,
        "load_or_create_runner_noise_keypair",
        lambda path: NoiseKeypair(),
    )
    monkeypatch.setattr(runner_agent, "RunnerWorkerManager", WorkerManager)
    monkeypatch.setattr(runner_agent, "_serve_runner_relay_connection", stop_relay)

    config = runner_agent.load_config()
    with pytest.raises(RuntimeError, match="stop test relay"):
        await runner_agent._runner_relay_loop(config, "runner-token")

    assert config.user_data_encryption == "required"
    assert manager_arguments["user_data_encryption"] == "required"
