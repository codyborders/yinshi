"""Verify opt-in managed Sprite runner lifecycle behavior."""

from __future__ import annotations

import asyncio
import json
import logging
import stat
import uuid
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
    attestation = tmp_path / ".artifact-sha256"
    attestation.write_text(f"{'a' * 64}\n", encoding="ascii")
    attestation.chmod(0o600)
    monkeypatch.setenv("YINSHI_RUNNER_ARTIFACT_SHA256", "a" * 64)
    monkeypatch.setenv("YINSHI_RUNNER_ARTIFACT_ATTESTATION_FILE", str(attestation))


def test_runner_agent_config_hides_registration_token_from_repr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Runner agent configuration should not reveal registration tokens."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_REGISTRATION_TOKEN", "runner-registration-secret")

    config = runner_agent.load_config()

    assert "runner-registration-secret" not in repr(config)


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


def test_managed_capability_preparation_rejects_probe_unlink_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Capability preparation rejects storage where its write probe cannot be removed."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")

    def fail_unlink(path: object, *, dir_fd: int | None = None) -> None:
        raise OSError("diagnostic unlink failure")

    monkeypatch.setattr(runner_agent.os, "unlink", fail_unlink)

    with pytest.raises(
        RuntimeError,
        match="Runner data directory failed read-after-write check",
    ):
        runner_agent._capabilities(runner_agent.load_config())


def test_managed_capability_preparation_preserves_primary_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Primary probe failure survives cleanup failures while every descriptor closes."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    config = runner_agent.load_config()
    original_close = runner_agent.os.close
    cleanup_calls: list[str] = []

    def fail_write(descriptor: int, value: bytes) -> int:
        raise OSError("diagnostic write failure")

    def fail_unlink(path: object, *, dir_fd: int | None = None) -> None:
        cleanup_calls.append("unlink")
        raise OSError("diagnostic unlink failure")

    def close_then_fail(descriptor: int) -> None:
        cleanup_calls.append("close")
        original_close(descriptor)
        raise OSError("diagnostic close failure")

    monkeypatch.setattr(runner_agent.os, "write", fail_write)
    monkeypatch.setattr(runner_agent.os, "unlink", fail_unlink)
    monkeypatch.setattr(runner_agent.os, "close", close_then_fail)

    with pytest.raises(
        RuntimeError,
        match="Runner data directory failed read-after-write check",
    ):
        runner_agent._capabilities(config)

    assert cleanup_calls == ["close", "unlink", "close"]


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
    assert config.data_protection_key_file == config.sqlite_dir / ".yinshi-data-protection-key"


async def test_runner_agent_stops_loop_diagnostics_after_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Runner startup failures should cancel diagnostics after arming them first."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    config = runner_agent.load_config()
    calls: list[str] = []

    class FakeWatchdog:
        def start(self) -> None:
            calls.append("start")

        async def run(self) -> None:
            calls.append("run")
            await asyncio.Event().wait()

        def stop(self) -> None:
            calls.append("stop")

    def fail_token_read(_path: Path) -> str | None:
        calls.append("token")
        raise RuntimeError("token read failed")

    monkeypatch.setattr(runner_agent, "EventLoopWatchdog", FakeWatchdog)
    monkeypatch.setattr(runner_agent, "_read_runner_token", fail_token_read)

    with pytest.raises(RuntimeError, match="token read failed"):
        await runner_agent.run_agent(config)

    assert calls[0:2] == ["start", "token"]
    assert calls[-1] == "stop"


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


def test_runner_agent_task_lease_keeps_relay_alive_without_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Task leasing must not terminate the control relay while the service runs."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    monkeypatch.setenv("YINSHI_RUNNER_SPRITE_TASK_LEASE", "enabled")

    config = runner_agent.load_config()

    assert config.sprite_task_lease is True
    assert config.relay_idle_timeout_seconds is None


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
async def test_runner_relay_dispatches_other_transfers_while_one_rpc_is_blocked() -> None:
    """One slow transfer must not block independent relay sessions."""
    slow_transfer_id = uuid.uuid4()
    fast_transfer_id = uuid.uuid4()
    release_slow = asyncio.Event()
    fast_response_sent = asyncio.Event()
    stop_receive = asyncio.Event()

    class Runtime:
        active_transfer_ids = (str(slow_transfer_id), str(fast_transfer_id))

        async def handle_binary(self, message: bytes, *, current_time: int) -> bytes:
            del current_time
            transfer_id = uuid.UUID(bytes=message[:16])
            if transfer_id == slow_transfer_id:
                await release_slow.wait()
            return message[:16] + b"-response"

    class WebSocket:
        def __init__(self) -> None:
            self.messages: asyncio.Queue[bytes] = asyncio.Queue()
            self.messages.put_nowait(slow_transfer_id.bytes + b"slow")
            self.messages.put_nowait(fast_transfer_id.bytes + b"fast")

        async def recv(self) -> bytes:
            if self.messages.empty():
                await stop_receive.wait()
                raise RuntimeError("stop")
            return await self.messages.get()

        async def send(self, message: bytes) -> None:
            if message.startswith(fast_transfer_id.bytes):
                fast_response_sent.set()

        async def close(self, *, code: int, reason: str) -> None:
            return None

    consumer = asyncio.create_task(
        runner_agent._consume_runner_relay_messages(
            Runtime(),  # type: ignore[arg-type]
            WebSocket(),  # type: ignore[arg-type]
        )
    )
    try:
        await asyncio.wait_for(fast_response_sent.wait(), timeout=0.1)
    finally:
        release_slow.set()
        stop_receive.set()
    with pytest.raises(RuntimeError, match="Runner relay protocol rejected"):
        await consumer


@pytest.mark.asyncio
async def test_runner_relay_accepts_next_frame_after_response_is_delivered() -> None:
    """One transfer may continue after delivery while the prior send completes."""
    transfer_id = uuid.uuid4()
    first_response_delivered = asyncio.Event()
    second_request_received = asyncio.Event()
    release_first_send = asyncio.Event()
    second_response_sent = asyncio.Event()
    stop_receive = asyncio.Event()

    class Runtime:
        active_transfer_ids = (str(transfer_id),)

        async def handle_binary(self, message: bytes, *, current_time: int) -> bytes:
            del current_time
            return message[:16] + message[16:] + b"-response"

    class WebSocket:
        def __init__(self) -> None:
            self.receive_count = 0
            self.sent: list[bytes] = []

        async def recv(self) -> bytes:
            self.receive_count += 1
            if self.receive_count == 1:
                return transfer_id.bytes + b"first"
            if self.receive_count == 2:
                await first_response_delivered.wait()
                second_request_received.set()
                return transfer_id.bytes + b"second"
            await stop_receive.wait()
            raise RuntimeError("stop")

        async def send(self, message: bytes) -> None:
            self.sent.append(message)
            if len(self.sent) == 1:
                first_response_delivered.set()
                await release_first_send.wait()
            elif len(self.sent) == 2:
                second_response_sent.set()

        async def close(self, *, code: int, reason: str) -> None:
            return None

    websocket = WebSocket()
    consumer = asyncio.create_task(
        runner_agent._consume_runner_relay_messages(
            Runtime(),  # type: ignore[arg-type]
            websocket,  # type: ignore[arg-type]
        )
    )
    await asyncio.wait_for(second_request_received.wait(), timeout=0.1)
    release_first_send.set()
    await asyncio.wait_for(second_response_sent.wait(), timeout=0.1)
    stop_receive.set()
    with pytest.raises(RuntimeError, match="Runner relay protocol rejected"):
        await consumer

    assert websocket.sent == [
        transfer_id.bytes + b"first-response",
        transfer_id.bytes + b"second-response",
    ]


@pytest.mark.asyncio
async def test_runner_relay_completed_sender_cannot_release_newer_operation() -> None:
    """A stale sender must not clear a newer operation's busy ownership."""
    transfer_id = uuid.uuid4()
    first_response_delivered = asyncio.Event()
    release_first_send = asyncio.Event()
    first_send_finished = asyncio.Event()
    second_operation_started = asyncio.Event()
    second_operation_cancelled = asyncio.Event()
    third_operation_started = asyncio.Event()

    class Runtime:
        active_transfer_ids = (str(transfer_id),)

        async def handle_binary(self, message: bytes, *, current_time: int) -> bytes:
            del current_time
            payload = message[16:]
            if payload == b"second":
                second_operation_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    second_operation_cancelled.set()
            if payload == b"third":
                third_operation_started.set()
            return message[:16] + payload + b"-response"

    class WebSocket:
        def __init__(self) -> None:
            self.receive_count = 0

        async def recv(self) -> bytes:
            self.receive_count += 1
            if self.receive_count == 1:
                return transfer_id.bytes + b"first"
            if self.receive_count == 2:
                await first_response_delivered.wait()
                return transfer_id.bytes + b"second"
            if self.receive_count == 3:
                await second_operation_started.wait()
                release_first_send.set()
                await first_send_finished.wait()
                await asyncio.sleep(0)
                return transfer_id.bytes + b"third"
            await third_operation_started.wait()
            raise RuntimeError("stop")

        async def send(self, message: bytes) -> None:
            if message.endswith(b"first-response"):
                first_response_delivered.set()
                await release_first_send.wait()
                first_send_finished.set()

        async def close(self, *, code: int, reason: str) -> None:
            return None

    with pytest.raises(RuntimeError, match="Runner relay protocol rejected"):
        await asyncio.wait_for(
            runner_agent._consume_runner_relay_messages(
                Runtime(),  # type: ignore[arg-type]
                WebSocket(),  # type: ignore[arg-type]
            ),
            timeout=0.1,
        )

    assert second_operation_cancelled.is_set()
    assert not third_operation_started.is_set()


@pytest.mark.asyncio
async def test_runner_relay_close_cancels_active_transfer_before_retirement() -> None:
    """A browser close must stop active work before releasing its transfer."""
    transfer_id = uuid.uuid4()
    operation_started = asyncio.Event()
    operation_cancelled = asyncio.Event()
    control_applied = asyncio.Event()

    class Runtime:
        active_transfer_ids = (str(transfer_id),)

        async def handle_binary(self, message: bytes, *, current_time: int) -> bytes:
            del message, current_time
            operation_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                operation_cancelled.set()

        async def handle_control(self, message: str) -> None:
            assert json.loads(message) == {
                "transfer_id": str(transfer_id),
                "type": "close",
            }
            assert operation_cancelled.is_set()
            control_applied.set()

    class WebSocket:
        def __init__(self) -> None:
            self.receive_count = 0

        async def recv(self) -> bytes | str:
            self.receive_count += 1
            if self.receive_count == 1:
                return transfer_id.bytes + b"request"
            if self.receive_count == 2:
                await operation_started.wait()
                return json.dumps({"transfer_id": str(transfer_id), "type": "close"})
            raise RuntimeError("stop")

        async def send(self, message: bytes | str) -> None:
            raise AssertionError(f"unexpected relay response: {message!r}")

        async def close(self, *, code: int, reason: str) -> None:
            return None

    with pytest.raises(RuntimeError, match="Runner relay protocol rejected"):
        await runner_agent._consume_runner_relay_messages(
            Runtime(),  # type: ignore[arg-type]
            WebSocket(),  # type: ignore[arg-type]
        )

    assert control_applied.is_set()


@pytest.mark.asyncio
async def test_runner_relay_quiesce_cancels_active_transfers_before_ack() -> None:
    """Maintenance acknowledgement must follow cancellation of active work."""
    transfer_id = uuid.uuid4()
    job_id = uuid.uuid4()
    operation_started = asyncio.Event()
    operation_cancelled = asyncio.Event()

    class Runtime:
        active_transfer_ids = (str(transfer_id),)

        async def handle_binary(self, message: bytes, *, current_time: int) -> bytes:
            del message, current_time
            operation_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                operation_cancelled.set()

        async def handle_control(self, message: str) -> str:
            assert json.loads(message) == {"job_id": str(job_id), "type": "quiesce"}
            assert operation_cancelled.is_set()
            return json.dumps({"job_id": str(job_id), "type": "quiesced"})

    class WebSocket:
        def __init__(self) -> None:
            self.receive_count = 0
            self.sent: list[str] = []

        async def recv(self) -> bytes | str:
            self.receive_count += 1
            if self.receive_count == 1:
                return transfer_id.bytes + b"request"
            if self.receive_count == 2:
                await operation_started.wait()
                return json.dumps({"job_id": str(job_id), "type": "quiesce"})
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

    assert websocket.sent == [json.dumps({"job_id": str(job_id), "type": "quiesced"})]


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
            self.acquire_count = 0
            self.closed = False
            self.instances.append(self)

        async def acquire(self) -> None:
            self.acquire_count += 1

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
        assert TaskLease.instances[0].acquire_count == 1
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
    assert TaskLease.instances[0].acquire_count == 1
    assert TaskLease.instances[0].closed is True


@pytest.mark.asyncio
async def test_runner_relay_loop_wires_broker_client_to_worker_manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The relay loop passes a control client that builds a resolver for RunnerWorkerManager."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    monkeypatch.setenv("YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("YINSHI_RUNNER_SPRITE_TASK_LEASE", "enabled")
    config = runner_agent.load_config()

    class TaskLease:
        instances: list[TaskLease] = []

        def __init__(self) -> None:
            self.acquire_count = 0
            self.closed = False
            self.instances.append(self)

        async def acquire(self) -> None:
            self.acquire_count += 1

        async def aclose(self) -> None:
            self.closed = True

    class NoiseKeypair:
        private_key = b"r" * 32

    captured_kwargs: dict[str, object] = {}
    serve_calls = 0

    class WorkerManager:
        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.clear()
            captured_kwargs.update(kwargs)

    broker_calls: list[tuple[object, str, str]] = []

    async def mock_request_github_access(
        client: object,
        runner_token: str,
        remote_url: str,
    ) -> object:
        broker_calls.append((client, runner_token, remote_url))
        return None

    async def serve(
        relay_config: object,
        runner_token: str,
        worker_manager: object,
        task_lease: object,
    ) -> bool:
        nonlocal serve_calls
        serve_calls += 1
        return True

    async def unexpected_sleep(delay: float) -> None:
        raise AssertionError(f"unexpected reconnect sleep: {delay}")

    fake_client = type("FakeClient", (), {"base_url": "http://localhost:8000"})()

    monkeypatch.setattr(
        runner_agent,
        "load_or_create_runner_noise_keypair",
        lambda path: NoiseKeypair(),
    )
    monkeypatch.setattr(runner_agent, "RunnerWorkerManager", WorkerManager)
    monkeypatch.setattr(runner_agent, "SpriteTaskLease", TaskLease)
    monkeypatch.setattr(runner_agent, "_serve_runner_relay_connection", serve)
    monkeypatch.setattr(runner_agent.asyncio, "sleep", unexpected_sleep)
    monkeypatch.setattr(
        runner_agent,
        "_request_runner_github_access",
        mock_request_github_access,
    )

    await runner_agent._runner_relay_loop(config, "runner-token", fake_client)

    assert serve_calls == 1
    resolver = captured_kwargs.get("github_clone_access_resolver")
    assert callable(resolver)
    await resolver("https://github.com/codyborders/my-pi.git")
    assert len(broker_calls) == 1
    recorded_client, recorded_token, recorded_url = broker_calls[0]
    assert recorded_client is fake_client
    assert recorded_token == "runner-token"
    assert recorded_url == "https://github.com/codyborders/my-pi.git"


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
            self.acquired = False
            self.closed = False
            TaskLease.instance = self

        async def acquire(self) -> None:
            self.acquired = True

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
    assert TaskLease.instance.acquired is True
    assert TaskLease.instance.closed is True


@pytest.mark.asyncio
async def test_runner_relay_loop_closes_task_client_after_baseline_acquire_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed baseline hold closes its client before relay serving starts."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_STORAGE_PROFILE", "fly_sprites_posix")
    monkeypatch.setenv("YINSHI_RUNNER_SPRITE_TASK_LEASE", "enabled")
    config = runner_agent.load_config()

    class TaskLease:
        instance: TaskLease | None = None

        def __init__(self) -> None:
            self.closed = False
            TaskLease.instance = self

        async def acquire(self) -> None:
            raise RuntimeError("lease unavailable")

        async def aclose(self) -> None:
            self.closed = True

    async def unexpected_serve(*args: object) -> bool:
        raise AssertionError("relay serving must not start")

    monkeypatch.setattr(runner_agent, "SpriteTaskLease", TaskLease)
    monkeypatch.setattr(
        runner_agent,
        "_serve_runner_relay_connection",
        unexpected_serve,
    )

    with pytest.raises(RuntimeError, match="lease unavailable"):
        await runner_agent._runner_relay_loop(config, "runner-token")

    assert TaskLease.instance is not None
    assert TaskLease.instance.closed is True


def _heartbeat_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build one heartbeat status failure without exposing response content."""
    request = httpx.Request("POST", "https://control.example/runner/heartbeat")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        "heartbeat failed",
        request=request,
        response=response,
    )


@pytest.mark.asyncio
async def test_heartbeat_loop_retries_transient_server_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One transient server failure must not terminate recurring heartbeats."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    config = runner_agent.load_config()
    heartbeat_calls = 0
    sleep_delays: list[float] = []

    async def heartbeat(*args: object) -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 1:
            raise _heartbeat_status_error(502)
        if heartbeat_calls == 3:
            raise asyncio.CancelledError

    async def sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(runner_agent, "_heartbeat", heartbeat)
    monkeypatch.setattr(runner_agent.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await runner_agent._heartbeat_loop(config, object(), "runner-token")  # type: ignore[arg-type]

    assert heartbeat_calls == 3
    assert sleep_delays == [1.0, config.heartbeat_interval_s]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 503, 599])
async def test_heartbeat_loop_retries_transient_http_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status_code: int,
) -> None:
    """Rate limits and server failures retry without terminating the loop."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    config = runner_agent.load_config()
    heartbeat_calls = 0
    sleep_delays: list[float] = []

    async def heartbeat(*args: object) -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 1:
            raise _heartbeat_status_error(status_code)
        raise asyncio.CancelledError

    async def sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(runner_agent, "_heartbeat", heartbeat)
    monkeypatch.setattr(runner_agent.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await runner_agent._heartbeat_loop(config, object(), "runner-token")  # type: ignore[arg-type]

    assert heartbeat_calls == 2
    assert sleep_delays == [1.0]


@pytest.mark.asyncio
async def test_heartbeat_loop_retries_network_failure_without_logging_request(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Network failures retry with generic logs that omit request details."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    config = runner_agent.load_config()
    heartbeat_calls = 0
    sleep_delays: list[float] = []
    secret_url = "https://control.example/runner/heartbeat?token=do-not-log"

    async def heartbeat(*args: object) -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 1:
            request = httpx.Request("POST", secret_url)
            raise httpx.ConnectError("private network detail", request=request)
        raise asyncio.CancelledError

    async def sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(runner_agent, "_heartbeat", heartbeat)
    monkeypatch.setattr(runner_agent.asyncio, "sleep", sleep)
    caplog.set_level(logging.WARNING)

    with pytest.raises(asyncio.CancelledError):
        await runner_agent._heartbeat_loop(config, object(), "runner-token")  # type: ignore[arg-type]

    assert heartbeat_calls == 2
    assert sleep_delays == [1.0]
    assert "network error" in caplog.text
    assert "do-not-log" not in caplog.text
    assert "private network detail" not in caplog.text
    assert secret_url not in caplog.text


@pytest.mark.asyncio
async def test_heartbeat_loop_fails_closed_for_decoding_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Response decoding failures remain fatal without sleeping or retrying."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    config = runner_agent.load_config()
    failure = httpx.DecodingError(
        "invalid heartbeat encoding",
        request=httpx.Request("POST", "https://control.example/runner/heartbeat"),
    )
    heartbeat_calls = 0

    async def heartbeat(*args: object) -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        raise failure

    async def unexpected_sleep(delay: float) -> None:
        raise AssertionError(f"unexpected heartbeat sleep: {delay}")

    monkeypatch.setattr(runner_agent, "_heartbeat", heartbeat)
    monkeypatch.setattr(runner_agent.asyncio, "sleep", unexpected_sleep)

    with pytest.raises(httpx.DecodingError) as error_info:
        await runner_agent._heartbeat_loop(config, object(), "runner-token")  # type: ignore[arg-type]

    assert error_info.value is failure
    assert heartbeat_calls == 1


@pytest.mark.asyncio
async def test_heartbeat_loop_bounds_and_resets_retry_backoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Heartbeat retry delay remains bounded and resets after one success."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YINSHI_RUNNER_HEARTBEAT_INTERVAL_S", "17")
    config = runner_agent.load_config()
    heartbeat_calls = 0
    sleep_delays: list[float] = []

    async def heartbeat(*args: object) -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls <= 7 or heartbeat_calls == 9:
            raise _heartbeat_status_error(502)
        if heartbeat_calls == 10:
            raise asyncio.CancelledError

    async def sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(runner_agent, "_heartbeat", heartbeat)
    monkeypatch.setattr(runner_agent.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await runner_agent._heartbeat_loop(config, object(), "runner-token")  # type: ignore[arg-type]

    assert heartbeat_calls == 10
    assert sleep_delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 17.0, 1.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (401, "Runner token was rejected by the control plane"),
        (400, "Runner heartbeat was rejected by the control plane"),
        (403, "Runner heartbeat was rejected by the control plane"),
        (404, "Runner heartbeat was rejected by the control plane"),
    ],
)
async def test_heartbeat_loop_rejects_fatal_http_status_with_sanitized_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status_code: int,
    message: str,
) -> None:
    """Authentication and other client failures remain fatal and sanitized."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    config = runner_agent.load_config()

    async def heartbeat(*args: object) -> None:
        raise _heartbeat_status_error(status_code)

    async def unexpected_sleep(delay: float) -> None:
        raise AssertionError(f"unexpected heartbeat sleep: {delay}")

    monkeypatch.setattr(runner_agent, "_heartbeat", heartbeat)
    monkeypatch.setattr(runner_agent.asyncio, "sleep", unexpected_sleep)

    with pytest.raises(RuntimeError, match=f"^{message}$") as error_info:
        await runner_agent._heartbeat_loop(config, object(), "runner-token")  # type: ignore[arg-type]

    assert error_info.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        ValueError("invalid heartbeat response"),
        RuntimeError("Control capability signing key changed unexpectedly"),
    ],
)
async def test_heartbeat_loop_fails_closed_for_non_http_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Exception,
) -> None:
    """Body validation and signing-key failures remain fatal without retries."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    config = runner_agent.load_config()

    async def heartbeat(*args: object) -> None:
        raise failure

    async def unexpected_sleep(delay: float) -> None:
        raise AssertionError(f"unexpected heartbeat sleep: {delay}")

    monkeypatch.setattr(runner_agent, "_heartbeat", heartbeat)
    monkeypatch.setattr(runner_agent.asyncio, "sleep", unexpected_sleep)

    with pytest.raises(type(failure), match=f"^{str(failure)}$") as error_info:
        await runner_agent._heartbeat_loop(config, object(), "runner-token")  # type: ignore[arg-type]

    assert error_info.value is failure


@pytest.mark.asyncio
async def test_run_agent_keeps_relay_owned_during_transient_heartbeat_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A transient heartbeat outage must not cancel relay worker ownership."""
    _set_runner_agent_env(monkeypatch, tmp_path)
    config = runner_agent.load_config()
    heartbeat_recovered = asyncio.Event()
    hold_heartbeat = asyncio.Event()
    relay_started = asyncio.Event()
    relay_cancelled = asyncio.Event()
    heartbeat_calls = 0

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    async def heartbeat(*args: object) -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 1:
            raise _heartbeat_status_error(502)
        heartbeat_recovered.set()
        await hold_heartbeat.wait()

    async def relay(*args: object) -> None:
        relay_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            relay_cancelled.set()

    async def sleep(delay: float) -> None:
        assert delay == 1.0

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
    monkeypatch.setattr(runner_agent, "_heartbeat", heartbeat)
    monkeypatch.setattr(runner_agent, "_runner_relay_loop", relay)
    monkeypatch.setattr(runner_agent.asyncio, "sleep", sleep)

    agent_task = asyncio.create_task(runner_agent.run_agent(config))
    await asyncio.wait_for(relay_started.wait(), timeout=0.2)
    await asyncio.wait_for(heartbeat_recovered.wait(), timeout=0.2)
    assert not agent_task.done()
    assert not relay_cancelled.is_set()

    agent_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await agent_task

    assert relay_cancelled.is_set()


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


@pytest.mark.asyncio
async def test_request_runner_github_access_success() -> None:
    """The broker client sends bearer auth and returns canonical fields."""
    from yinshi.models import RunnerGitHubAccessOut

    canonical = RunnerGitHubAccessOut(
        clone_url="https://github.com/codyborders/my-pi.git",
        access_token="ghp_fake",
        installation_id=1234,
        repository_installation_id=5678,
        manage_url="https://github.com/apps/yinshi/installations/1234",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url.path) == "/runner/github-access"
        assert request.headers.get("Authorization") == "Bearer runner-token-123"
        body = json.loads(request.content)
        assert body["remote_url"] == "https://github.com/codyborders/my-pi.git"
        return httpx.Response(200, json=canonical.model_dump(mode="json"))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        result = await runner_agent._request_runner_github_access(
            client, "runner-token-123", "https://github.com/codyborders/my-pi.git"
        )

    assert result is not None
    assert result.clone_url == "https://github.com/codyborders/my-pi.git"
    assert result.access_token == "ghp_fake"
    assert result.installation_id == 1234
    assert result.repository_installation_id == 5678
    assert result.manage_url == "https://github.com/apps/yinshi/installations/1234"


@pytest.mark.asyncio
async def test_request_runner_github_access_malformed_response() -> None:
    """Malformed JSON in 200 response raises safe GitHubAppError."""
    from yinshi.exceptions import GitHubAppError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not valid json{{")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        with pytest.raises(GitHubAppError) as exc_info:
            await runner_agent._request_runner_github_access(
                client, "runner-token-123", "https://github.com/owner/repo.git"
            )
    assert "GitHub integration error" in str(exc_info.value)


@pytest.mark.asyncio
async def test_request_runner_github_access_400_error() -> None:
    """Structured 400 response reconstructs GitHubAccessError with all fields."""
    from yinshi.exceptions import GitHubAccessError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "detail": {
                    "code": "install_not_found",
                    "message": "Repository not installed",
                    "connect_url": "https://github.com/apps/yinshi",
                    "manage_url": "https://github.com/settings/installations",
                }
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        with pytest.raises(GitHubAccessError) as exc_info:
            await runner_agent._request_runner_github_access(
                client, "runner-token-123", "https://github.com/owner/repo.git"
            )
    error = exc_info.value
    assert error.code == "install_not_found"
    assert str(error) == "Repository not installed"
    assert error.connect_url == "https://github.com/apps/yinshi"
    assert error.manage_url == "https://github.com/settings/installations"


@pytest.mark.asyncio
async def test_request_runner_github_access_rejects_malformed_400_response() -> None:
    """Unexpected structured-error fields fail closed."""
    from yinshi.exceptions import GitHubAppError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "detail": {
                    "code": "install_not_found",
                    "message": "Repository not installed",
                    "connect_url": None,
                    "manage_url": None,
                    "unexpected": "field",
                }
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        with pytest.raises(GitHubAppError) as exc_info:
            await runner_agent._request_runner_github_access(
                client, "runner-token-123", "https://github.com/owner/repo.git"
            )

    assert type(exc_info.value) is GitHubAppError
    assert str(exc_info.value) == "GitHub integration error"
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_request_runner_github_access_rejects_oversized_400_response() -> None:
    """Oversized structured errors fail before their fields are parsed."""
    from yinshi.exceptions import GitHubAppError

    oversized_message = "x" * 66000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "detail": {
                    "code": "install_not_found",
                    "message": oversized_message,
                    "connect_url": None,
                    "manage_url": None,
                }
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        with pytest.raises(GitHubAppError) as exc_info:
            await runner_agent._request_runner_github_access(
                client, "runner-token-123", "https://github.com/owner/repo.git"
            )

    assert type(exc_info.value) is GitHubAppError
    assert str(exc_info.value) == "GitHub integration error"


@pytest.mark.asyncio
async def test_request_runner_github_access_rejects_oversized_response() -> None:
    """Oversized broker responses raise a safe GitHubAppError."""
    from yinshi.exceptions import GitHubAppError
    from yinshi.models import RunnerGitHubAccessOut

    canonical = RunnerGitHubAccessOut(
        clone_url="https://github.com/owner/repo.git",
        access_token="ghp_fake",
        installation_id=1234,
        repository_installation_id=5678,
        manage_url=None,
    )
    body = canonical.model_dump_json()
    padding = " " * 66000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=(body + padding).encode())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        with pytest.raises(GitHubAppError) as exc_info:
            await runner_agent._request_runner_github_access(
                client, "runner-token-123", "https://github.com/owner/repo.git"
            )
    assert "GitHub integration error" in str(exc_info.value)
