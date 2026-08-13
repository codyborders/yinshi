"""Verify opt-in managed Sprite runner lifecycle behavior."""

from __future__ import annotations

import asyncio
import logging
import stat
from pathlib import Path
from typing import Any

import pytest

from yinshi import runner_agent


def _set_runner_agent_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point runner-agent paths at one isolated directory."""
    monkeypatch.setenv("YINSHI_CONTROL_URL", "https://control.example")
    monkeypatch.setenv("YINSHI_RUNNER_TOKEN_FILE", str(tmp_path / "runner-token"))
    monkeypatch.setenv("YINSHI_RUNNER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YINSHI_RUNNER_SQLITE_DIR", str(tmp_path / "sqlite"))
    monkeypatch.setenv("YINSHI_RUNNER_SHARED_FILES_DIR", str(tmp_path / "shared"))
    attestation = tmp_path / ".artifact-sha256"
    attestation.write_text(f"{'a' * 64}\n", encoding="ascii")
    attestation.chmod(0o600)
    monkeypatch.setenv("YINSHI_RUNNER_ARTIFACT_SHA256", "a" * 64)
    monkeypatch.setenv("YINSHI_RUNNER_ARTIFACT_ATTESTATION_FILE", str(attestation))


@pytest.mark.parametrize("existing_mode", [None, 0o755])
def test_managed_capability_preparation_enforces_owner_only_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    existing_mode: int | None,
) -> None:
    """Capability preparation creates or corrects managed storage to mode 0700."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    paths = tuple(tmp_path / name for name in ("data", "sqlite", "shared"))
    if existing_mode is not None:
        for path in paths:
            path.mkdir(mode=existing_mode)
            path.chmod(existing_mode)

    runner_agent._capabilities(runner_agent.load_config())

    assert [stat.S_IMODE(path.stat().st_mode) for path in paths] == [0o700] * 3
    assert all(not (path / ".yinshi-runner-write-check").exists() for path in paths)

    target = tmp_path / "target"
    target.write_text("unchanged\n", encoding="utf-8")
    planted_probe = paths[0] / ".yinshi-runner-write-check"
    planted_probe.symlink_to(target)

    runner_agent._capabilities(runner_agent.load_config())

    assert target.read_text(encoding="utf-8") == "unchanged\n"
    assert planted_probe.is_symlink()


def test_managed_capability_preparation_rejects_replaced_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Capability preparation must reject a managed directory symlink."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (tmp_path / "data").symlink_to(replacement, target_is_directory=True)

    with pytest.raises(RuntimeError, match="Runner data path is not a directory"):
        runner_agent._capabilities(runner_agent.load_config())


def test_runner_agent_managed_lifecycle_defaults_are_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """BYOC configuration retains permanent relay behavior without local tasks."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.delenv("YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("YINSHI_RUNNER_SPRITE_TASK_LEASE", raising=False)

    config = runner_agent.load_config()

    assert config.relay_idle_timeout_seconds is None
    assert config.sprite_task_lease is False


def test_runner_agent_startup_log_excludes_private_paths(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup logs retain safe context without runner filesystem paths."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    config = runner_agent.load_config()

    async def run_agent(started_config: runner_agent.RunnerAgentConfig) -> None:
        assert started_config is config

    monkeypatch.setattr(runner_agent, "load_config", lambda: config)
    monkeypatch.setattr(runner_agent, "run_agent", run_agent)
    caplog.set_level(logging.INFO)

    runner_agent.main()

    private_paths = (
        str(config.data_dir),
        str(config.sqlite_dir),
        str(config.shared_files_dir),
        str(config.runner_token_file),
    )
    for record in caplog.records:
        rendered_record = f"{record.getMessage()} {record.args!r}"
        assert all(path not in rendered_record for path in private_paths)
    assert config.control_url in caplog.text
    assert config.storage_profile in caplog.text


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (
            "YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS",
            "",
            "must be a positive finite number",
        ),
        (
            "YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS",
            "0",
            "must be a positive finite number",
        ),
        (
            "YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS",
            "nan",
            "must be a positive finite number",
        ),
        (
            "YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS",
            "inf",
            "must be a positive finite number",
        ),
        (
            "YINSHI_RUNNER_SPRITE_TASK_LEASE",
            "",
            "must be disabled or enabled",
        ),
        (
            "YINSHI_RUNNER_SPRITE_TASK_LEASE",
            "true",
            "must be disabled or enabled",
        ),
        (
            "YINSHI_RUNNER_SPRITE_TASK_LEASE",
            "ENABLED",
            "must be disabled or enabled",
        ),
    ],
)
def test_runner_agent_rejects_invalid_managed_lifecycle_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    """Managed settings reject empty, non-finite, and non-canonical values."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=message):
        runner_agent.load_config()


def test_runner_agent_task_lease_requires_fly_sprites_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Task lease cannot run outside the managed Fly Sprites profile."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS", "120.5")
    monkeypatch.setenv("YINSHI_RUNNER_SPRITE_TASK_LEASE", "enabled")

    with pytest.raises(RuntimeError, match="requires fly_sprites_posix"):
        runner_agent.load_config()


def test_runner_agent_task_lease_requires_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Task lease must have an idle limit for managed shutdown."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    monkeypatch.setenv("YINSHI_RUNNER_SPRITE_TASK_LEASE", "enabled")

    with pytest.raises(RuntimeError, match="requires YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS"):
        runner_agent.load_config()


def test_fly_runner_requires_artifact_attestation_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Managed Fly startup fails before registration without artifact settings."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    monkeypatch.delenv("YINSHI_RUNNER_ARTIFACT_SHA256")

    with pytest.raises(RuntimeError, match="YINSHI_RUNNER_ARTIFACT_SHA256 is required"):
        runner_agent.load_config()


def test_fly_runner_requires_artifact_attestation_file_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Managed Fly startup requires an absolute artifact attestation path."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    monkeypatch.setenv("YINSHI_RUNNER_ARTIFACT_SHA256", "a" * 64)
    monkeypatch.delenv("YINSHI_RUNNER_ARTIFACT_ATTESTATION_FILE")

    with pytest.raises(
        RuntimeError,
        match="YINSHI_RUNNER_ARTIFACT_ATTESTATION_FILE is required",
    ):
        runner_agent.load_config()


@pytest.mark.parametrize("digest", ["A" * 64, "abc", "0" * 63])
def test_fly_runner_rejects_noncanonical_artifact_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    digest: str,
) -> None:
    """Managed Fly startup accepts only canonical lowercase SHA-256 text."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    monkeypatch.setenv("YINSHI_RUNNER_ARTIFACT_SHA256", digest)

    with pytest.raises(RuntimeError, match="64 lowercase hexadecimal"):
        runner_agent.load_config()


def test_runner_agent_accepts_explicit_managed_lifecycle_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Canonical managed settings enable idle shutdown and task leases."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    digest = "a" * 64
    attestation = tmp_path / ".artifact-sha256"
    attestation.write_text(f"{digest}\n", encoding="ascii")
    attestation.chmod(0o600)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    monkeypatch.setenv("YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS", "120.5")
    monkeypatch.setenv("YINSHI_RUNNER_SPRITE_TASK_LEASE", "enabled")
    monkeypatch.setenv("YINSHI_RUNNER_ARTIFACT_SHA256", digest)
    monkeypatch.setenv("YINSHI_RUNNER_ARTIFACT_ATTESTATION_FILE", str(attestation))

    config = runner_agent.load_config()

    assert config.relay_idle_timeout_seconds == 120.5
    assert config.sprite_task_lease is True
    assert config.artifact_sha256 == digest
    assert config.artifact_attestation_file == attestation


@pytest.mark.asyncio
async def test_runner_relay_sends_quiesced_control_acknowledgement() -> None:
    """Runner should return maintenance acknowledgements over its authenticated socket."""

    class Runtime:
        active_transfer_ids: tuple[str, ...] = ()

        async def handle_control(self, message: str) -> str | None:
            assert message == '{"job_id":"job","type":"quiesce"}'
            return '{"job_id":"job","type":"quiesced"}'

    class WebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def recv(self) -> str:
            if not self.sent:
                return '{"job_id":"job","type":"quiesce"}'
            raise RuntimeError("stop")

        async def send(self, message: str) -> None:
            self.sent.append(message)

        async def close(self, *, code: int, reason: str) -> None:
            return None

    websocket = WebSocket()
    with pytest.raises(RuntimeError, match="Runner relay protocol rejected"):
        await runner_agent._consume_runner_relay_messages(
            Runtime(),  # type: ignore[arg-type]
            websocket,  # type: ignore[arg-type]
        )

    assert websocket.sent == ['{"job_id":"job","type":"quiesced"}']


@pytest.mark.asyncio
async def test_runner_relay_messages_expire_while_no_transfer_is_open() -> None:
    """Managed relay consumption returns after its idle limit."""

    class Runtime:
        active_transfer_ids: tuple[str, ...] = ()

    class WebSocket:
        async def recv(self) -> Any:
            await asyncio.Event().wait()

    expired = await runner_agent._consume_runner_relay_messages(
        Runtime(),  # type: ignore[arg-type]
        WebSocket(),  # type: ignore[arg-type]
        idle_timeout_seconds=0.01,
    )

    assert expired is True


@pytest.mark.asyncio
async def test_runner_relay_connection_reports_idle_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Connection serving forwards its idle limit and closes runtime state."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS", "12.5")
    config = runner_agent.load_config()
    observed_timeouts: list[float | None] = []
    observed_task_leases: list[object] = []

    class Runtime:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class Connection:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    runtimes: list[Runtime] = []
    task_lease = object()

    def runtime_factory(
        relay_config: object,
        worker_manager: object,
        runtime_task_lease: object,
    ) -> Runtime:
        runtime = Runtime()
        runtimes.append(runtime)
        observed_task_leases.append(runtime_task_lease)
        return runtime

    async def consume(
        relay_runtime: object,
        websocket: object,
        *,
        idle_timeout_seconds: float | None = None,
    ) -> bool:
        observed_timeouts.append(idle_timeout_seconds)
        return True

    monkeypatch.setattr(runner_agent, "_runner_relay_runtime", runtime_factory)
    monkeypatch.setattr(runner_agent, "connect", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(runner_agent, "_consume_runner_relay_messages", consume)

    for _ in range(2):
        expired = await runner_agent._serve_runner_relay_connection(
            config,
            "runner-token",
            object(),  # type: ignore[arg-type]
            task_lease,  # type: ignore[arg-type]
        )
        assert expired is True

    assert observed_timeouts == [12.5, 12.5]
    assert observed_task_leases == [task_lease, task_lease]
    assert len(runtimes) == 2
    assert all(runtime.closed for runtime in runtimes)


@pytest.mark.asyncio
async def test_runner_relay_loop_returns_immediately_after_idle_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Intentional idle expiry stops managed relay reconnection and closes its task client."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    monkeypatch.setenv("YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("YINSHI_RUNNER_SPRITE_TASK_LEASE", "enabled")
    config = runner_agent.load_config()
    serve_calls = 0

    class TaskLease:
        instances: list[TaskLease] = []

        def __init__(self) -> None:
            self.closed = False
            self.instances.append(self)

        async def aclose(self) -> None:
            self.closed = True

    class NoiseKeypair:
        private_key = b"r" * 32

    class WorkerManager:
        def __init__(self, **kwargs: object) -> None:
            pass

    async def serve(
        relay_config: object,
        runner_token: str,
        worker_manager: object,
        task_lease: object,
    ) -> bool:
        nonlocal serve_calls
        serve_calls += 1
        assert task_lease is TaskLease.instances[0]
        return True

    async def unexpected_sleep(delay: float) -> None:
        raise AssertionError(f"unexpected reconnect sleep: {delay}")

    monkeypatch.setattr(
        runner_agent,
        "load_or_create_runner_noise_keypair",
        lambda path: NoiseKeypair(),
    )
    monkeypatch.setattr(runner_agent, "RunnerWorkerManager", WorkerManager)
    monkeypatch.setattr(runner_agent, "SpriteTaskLease", TaskLease)
    monkeypatch.setattr(runner_agent, "_serve_runner_relay_connection", serve)
    monkeypatch.setattr(runner_agent.asyncio, "sleep", unexpected_sleep)

    await runner_agent._runner_relay_loop(config, "runner-token")

    assert serve_calls == 1
    assert len(TaskLease.instances) == 1
    assert TaskLease.instances[0].closed is True


@pytest.mark.asyncio
async def test_runner_relay_loop_closes_task_client_on_fatal_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fatal relay errors close the managed task client."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    monkeypatch.setenv("YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("YINSHI_RUNNER_SPRITE_TASK_LEASE", "enabled")
    config = runner_agent.load_config()

    class TaskLease:
        instance: TaskLease | None = None

        def __init__(self) -> None:
            self.closed = False
            TaskLease.instance = self

        async def aclose(self) -> None:
            self.closed = True

    class NoiseKeypair:
        private_key = b"r" * 32

    class WorkerManager:
        def __init__(self, **kwargs: object) -> None:
            pass

    async def serve(*args: object) -> bool:
        raise RuntimeError("fatal")

    monkeypatch.setattr(
        runner_agent,
        "load_or_create_runner_noise_keypair",
        lambda path: NoiseKeypair(),
    )
    monkeypatch.setattr(runner_agent, "RunnerWorkerManager", WorkerManager)
    monkeypatch.setattr(runner_agent, "SpriteTaskLease", TaskLease)
    monkeypatch.setattr(runner_agent, "_serve_runner_relay_connection", serve)

    with pytest.raises(RuntimeError, match="fatal"):
        await runner_agent._runner_relay_loop(config, "runner-token")

    assert TaskLease.instance is not None
    assert TaskLease.instance.closed is True


@pytest.mark.asyncio
async def test_run_agent_stops_heartbeat_after_idle_relay_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Managed idle completion must stop heartbeat before agent returns."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    config = runner_agent.load_config()
    heartbeat_cancelled = asyncio.Event()

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    async def heartbeat(*args: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            heartbeat_cancelled.set()

    async def relay(*args: object) -> None:
        return None

    monkeypatch.setattr(runner_agent.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(runner_agent, "_read_runner_token", lambda path: "runner-token")
    monkeypatch.setattr(
        runner_agent,
        "_read_owner_only_text_file",
        lambda path, label: "A" * 43,
    )
    monkeypatch.setattr(
        runner_agent,
        "_validate_capability_signing_public_key",
        lambda value: value,
    )
    monkeypatch.setattr(runner_agent, "_heartbeat_loop", heartbeat)
    monkeypatch.setattr(runner_agent, "_runner_relay_loop", relay)

    await asyncio.wait_for(runner_agent.run_agent(config), timeout=0.2)

    assert heartbeat_cancelled.is_set()
