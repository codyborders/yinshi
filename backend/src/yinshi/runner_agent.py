"""Minimal cloud runner agent that registers and heartbeats to Yinshi.

The runner process is intentionally small: it proves that user-owned compute
and POSIX storage are reachable before higher-level job dispatch is enabled.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)
RUNNER_VERSION = "0.1.0"
_DEFAULT_CONTROL_URL = "http://localhost:8000"
_DEFAULT_DATA_DIR = "/var/lib/yinshi"
_DEFAULT_SQLITE_DIR = f"{_DEFAULT_DATA_DIR}/sqlite"
_DEFAULT_SHARED_FILES_DIR = "/mnt/yinshi-s3-files"
_DEFAULT_TOKEN_FILE = "/var/lib/yinshi/runner-token"
_DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
_REQUEST_TIMEOUT_S = 15.0
_REGISTRATION_TOKEN_ENV_PREFIX = "YINSHI_REGISTRATION_TOKEN="


@dataclass(frozen=True, slots=True)
class RunnerAgentConfig:
    """Environment-derived configuration for the cloud runner agent."""

    control_url: str
    registration_token: str | None
    runner_token_file: Path
    data_dir: Path
    sqlite_dir: Path
    shared_files_dir: Path
    heartbeat_interval_s: float
    env_file: Path | None


def _env_text(name: str, default: str | None = None) -> str | None:
    """Read and normalize an optional environment value."""
    value = os.environ.get(name, default)
    if value is None:
        return None
    normalized_value = value.strip()
    if not normalized_value:
        return None
    return normalized_value


def _env_float(name: str, default: float) -> float:
    """Read a positive float from the environment with explicit validation."""
    raw_value = _env_text(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _env_path(name: str, default: str) -> Path:
    """Read a required absolute filesystem path from the environment."""
    path_text = _env_text(name, default)
    if path_text is None:
        raise RuntimeError(f"{name} must not be empty")
    path = Path(path_text)
    if not path.is_absolute():
        raise RuntimeError(f"{name} must be an absolute path")
    if ".." in path.parts:
        raise RuntimeError(f"{name} must not contain parent directory references")
    return path


def load_config() -> RunnerAgentConfig:
    """Build runner agent config from environment variables."""
    control_url = _env_text("YINSHI_CONTROL_URL", _DEFAULT_CONTROL_URL)
    assert control_url is not None, "default control URL must be non-empty"
    runner_token_file = _env_path("YINSHI_RUNNER_TOKEN_FILE", _DEFAULT_TOKEN_FILE)
    data_dir = _env_path("YINSHI_RUNNER_DATA_DIR", _DEFAULT_DATA_DIR)
    sqlite_dir = _env_path("YINSHI_RUNNER_SQLITE_DIR", _DEFAULT_SQLITE_DIR)
    shared_files_dir = _env_path("YINSHI_RUNNER_SHARED_FILES_DIR", _DEFAULT_SHARED_FILES_DIR)
    env_file_text = _env_text("YINSHI_RUNNER_ENV_FILE")
    env_file = Path(env_file_text) if env_file_text else None
    return RunnerAgentConfig(
        control_url=control_url.rstrip("/"),
        registration_token=_env_text("YINSHI_REGISTRATION_TOKEN"),
        runner_token_file=runner_token_file,
        data_dir=data_dir,
        sqlite_dir=sqlite_dir,
        shared_files_dir=shared_files_dir,
        heartbeat_interval_s=_env_float(
            "YINSHI_RUNNER_HEARTBEAT_INTERVAL_S",
            _DEFAULT_HEARTBEAT_INTERVAL_S,
        ),
        env_file=env_file,
    )


def _probe_writable_directory(directory: Path, label: str) -> None:
    """Create and probe a POSIX directory required by the runner."""
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise RuntimeError(f"Runner {label} path is not a directory: {directory}")

    probe_path = directory / ".yinshi-runner-write-check"
    probe_path.write_text("ok\n", encoding="utf-8")
    if probe_path.read_text(encoding="utf-8") != "ok\n":
        raise RuntimeError(f"Runner {label} directory failed read-after-write check")
    probe_path.unlink(missing_ok=True)


def _shared_files_storage(shared_files_dir: Path) -> str:
    """Describe whether the shared file path is a mounted filesystem."""
    if shared_files_dir.is_mount():
        return "s3_files_mount"
    return "local_posix"


def _validate_storage_layout(config: RunnerAgentConfig) -> None:
    """Reject layouts that would put live SQLite on the shared file mount."""
    try:
        config.sqlite_dir.relative_to(config.shared_files_dir)
    except ValueError:
        return
    raise RuntimeError(
        "YINSHI_RUNNER_SQLITE_DIR must not live under YINSHI_RUNNER_SHARED_FILES_DIR"
    )


def _capabilities(config: RunnerAgentConfig) -> dict[str, Any]:
    """Return storage and execution capabilities advertised to the control plane."""
    _validate_storage_layout(config)
    _probe_writable_directory(config.data_dir, "data")
    _probe_writable_directory(config.sqlite_dir, "sqlite")
    _probe_writable_directory(config.shared_files_dir, "shared files")
    return {
        "posix_storage": True,
        "sqlite": True,
        "git_worktrees": True,
        "pi_sidecar": True,
        "data_dir": str(config.data_dir),
        "sqlite_dir": str(config.sqlite_dir),
        "shared_files_dir": str(config.shared_files_dir),
        "sqlite_storage": "runner_ebs",
        "shared_files_storage": _shared_files_storage(config.shared_files_dir),
        "live_sqlite_on_shared_files": False,
    }


def _read_runner_token(token_file: Path) -> str | None:
    """Read a previously issued runner bearer token from disk."""
    if not token_file.exists():
        return None
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(f"Runner token file is empty: {token_file}")
    return token


def _write_runner_token(token_file: Path, runner_token: str) -> None:
    """Persist the runner bearer token with owner-only permissions."""
    if not runner_token.strip():
        raise RuntimeError("Runner token must not be empty")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(f"{runner_token}\n", encoding="utf-8")
    token_file.chmod(0o600)


def _scrub_registration_token(env_file: Path | None) -> None:
    """Remove the consumed one-time token from the systemd environment file."""
    if env_file is None:
        return
    if not env_file.exists():
        return
    lines = env_file.read_text(encoding="utf-8").splitlines()
    filtered_lines = [line for line in lines if not line.startswith(_REGISTRATION_TOKEN_ENV_PREFIX)]
    if filtered_lines == lines:
        return
    env_file.write_text("\n".join(filtered_lines) + "\n", encoding="utf-8")
    env_file.chmod(0o600)


async def _register(config: RunnerAgentConfig, client: httpx.AsyncClient) -> str:
    """Register this runner and return the issued bearer token."""
    if config.registration_token is None:
        raise RuntimeError("YINSHI_REGISTRATION_TOKEN is required until a runner token file exists")
    payload = {
        "registration_token": config.registration_token,
        "runner_version": RUNNER_VERSION,
        "capabilities": _capabilities(config),
        "data_dir": str(config.data_dir),
        "sqlite_dir": str(config.sqlite_dir),
        "shared_files_dir": str(config.shared_files_dir),
    }
    response = await client.post("/runner/register", json=payload)
    response.raise_for_status()
    body = response.json()
    runner_token = body.get("runner_token")
    if not isinstance(runner_token, str) or not runner_token.strip():
        raise RuntimeError("Runner registration response did not include a bearer token")
    _write_runner_token(config.runner_token_file, runner_token)
    _scrub_registration_token(config.env_file)
    logger.info("Registered Yinshi cloud runner %s", body.get("runner_id", "unknown"))
    return runner_token


async def _heartbeat(
    config: RunnerAgentConfig,
    client: httpx.AsyncClient,
    runner_token: str,
) -> None:
    """Send one heartbeat to the control plane."""
    payload = {
        "runner_version": RUNNER_VERSION,
        "capabilities": _capabilities(config),
        "data_dir": str(config.data_dir),
        "sqlite_dir": str(config.sqlite_dir),
        "shared_files_dir": str(config.shared_files_dir),
    }
    response = await client.post(
        "/runner/heartbeat",
        json=payload,
        headers={"Authorization": f"Bearer {runner_token}"},
    )
    response.raise_for_status()
    body = response.json()
    logger.info("Heartbeat accepted for Yinshi cloud runner %s", body.get("runner_id"))


async def run_agent(config: RunnerAgentConfig) -> None:
    """Run the cloud runner registration and heartbeat loop forever."""
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
    async with httpx.AsyncClient(
        base_url=config.control_url,
        timeout=_REQUEST_TIMEOUT_S,
        limits=limits,
    ) as client:
        runner_token = _read_runner_token(config.runner_token_file)
        if runner_token is None:
            runner_token = await _register(config, client)

        while True:
            try:
                await _heartbeat(config, client, runner_token)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    raise RuntimeError("Runner token was rejected by the control plane") from exc
                raise
            await asyncio.sleep(config.heartbeat_interval_s)


def main() -> None:
    """Load configuration and run the cloud runner agent."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    config = load_config()
    logger.info(
        "Starting Yinshi cloud runner agent against %s with SQLite dir %s and shared files dir %s",
        config.control_url,
        config.sqlite_dir,
        config.shared_files_dir,
    )
    asyncio.run(run_agent(config))


if __name__ == "__main__":
    main()
