"""Tests for managed Sprite guest installation."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import pytest

from yinshi.services.sprites import ServiceRecord, ServiceState

ARTIFACT = b"managed artifact"
ARTIFACT_SHA256 = hashlib.sha256(ARTIFACT).hexdigest()
CLAIM_ENVIRONMENT = {
    "YINSHI_CONTROL_URL": "https://control.example",
    "YINSHI_REGISTRATION_TOKEN": "token with 'quotes' and spaces",
    "YINSHI_RUNNER_STORAGE_PROFILE": "fly_sprites_posix",
    "YINSHI_RUNNER_SQLITE_STORAGE": "local_posix",
    "YINSHI_RUNNER_SHARED_FILES_STORAGE": "local_posix",
    "YINSHI_RUNNER_DATA_DIR": "/var/lib/yinshi",
    "YINSHI_RUNNER_SQLITE_DIR": "/var/lib/yinshi/sqlite",
    "YINSHI_RUNNER_SHARED_FILES_DIR": "/var/lib/yinshi/files",
    "YINSHI_RUNNER_TOKEN_FILE": "/var/lib/yinshi/runner-token",
    "YINSHI_RUNNER_NOISE_KEY_FILE": "/var/lib/yinshi/runner-noise.key",
    "YINSHI_RUNNER_CAPABILITY_SIGNING_KEY_FILE": ("/var/lib/yinshi/control-capability-signing.pub"),
    "YINSHI_RUNNER_REPLAY_DATABASE_FILE": ("/var/lib/yinshi/runner-capability-replay.sqlite3"),
    "YINSHI_RUNNER_ENV_FILE": "/etc/yinshi-runner.env",
}


def _invalid_claim_environments() -> list[dict[str, Any]]:
    missing = dict(CLAIM_ENVIRONMENT)
    missing.pop("YINSHI_CONTROL_URL")
    extra = {**CLAIM_ENVIRONMENT, "UNEXPECTED": "value"}
    wrong_profile = {**CLAIM_ENVIRONMENT, "YINSHI_RUNNER_STORAGE_PROFILE": "other"}
    empty = {**CLAIM_ENVIRONMENT, "YINSHI_REGISTRATION_TOKEN": ""}
    non_string = {**CLAIM_ENVIRONMENT, "YINSHI_REGISTRATION_TOKEN": 7}
    too_long = {**CLAIM_ENVIRONMENT, "YINSHI_REGISTRATION_TOKEN": "x" * 4097}
    nul = {**CLAIM_ENVIRONMENT, "YINSHI_CONTROL_URL": "https://control.example\0bad"}
    carriage_return = {**CLAIM_ENVIRONMENT, "YINSHI_REGISTRATION_TOKEN": "token\rbad"}
    line_feed = {**CLAIM_ENVIRONMENT, "YINSHI_CONTROL_URL": "https://control.example\nbad"}
    return [
        missing,
        extra,
        wrong_profile,
        empty,
        non_string,
        too_long,
        nul,
        carriage_return,
        line_feed,
    ]


@dataclass
class WrittenFile:
    path: str
    content: bytes
    mode: str
    mkdir: bool


class FakeSpritesClient:
    """Record installer calls and return configured bootstrap states."""

    def __init__(
        self,
        statuses: tuple[str, ...] = ("stopped",),
        *,
        fail_operation: str | None = None,
        fail_call: int = 1,
        failure: BaseException | None = None,
        bootstrap_error: str | None = None,
    ) -> None:
        self.statuses = list(statuses)
        self.files: list[WrittenFile] = []
        self.services: list[dict[str, object]] = []
        self.fail_operation = fail_operation
        self.fail_call = fail_call
        self.failure = failure
        self.bootstrap_error = bootstrap_error
        self.operation_calls: dict[str, int] = {}

    def _fail_if_requested(self, operation: str) -> None:
        calls = self.operation_calls.get(operation, 0) + 1
        self.operation_calls[operation] = calls
        if self.fail_operation == operation and self.fail_call == calls:
            if self.failure is not None:
                raise self.failure
            raise RuntimeError("provider secret detail")

    async def write_file(
        self,
        name: str,
        *,
        path: str,
        content: bytes,
        mode: str,
        mkdir: bool,
    ) -> None:
        assert name == "yinshi-managed"
        self._fail_if_requested("write_file")
        self.files.append(WrittenFile(path, content, mode, mkdir))

    async def configure_service(self, name: str, **kwargs: object) -> None:
        assert name == "yinshi-managed"
        self._fail_if_requested("configure_service")
        self.services.append(kwargs)

    async def get_service(
        self,
        name: str,
        *,
        service_name: str,
    ) -> ServiceRecord | None:
        assert name == "yinshi-managed"
        assert service_name == "yinshi-bootstrap"
        self._fail_if_requested("get_service")
        status = self.statuses.pop(0) if self.statuses else "stopped"
        return ServiceRecord(
            name=service_name,
            command="/bin/bash",
            args=(),
            needs=(),
            http_port=None,
            state=ServiceState(
                name=service_name,
                status=status,  # type: ignore[arg-type]
                pid=None,
                started_at=None,
                error=self.bootstrap_error,
            ),
        )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("bootstrap_script", b""),
        ("bootstrap_script", "#!/bin/bash"),
        ("bootstrap_script", b"x" * (10 * 1024 * 1024 + 1)),
        ("relay_idle_timeout_seconds", True),
        ("relay_idle_timeout_seconds", float("nan")),
        ("relay_idle_timeout_seconds", float("inf")),
        ("relay_idle_timeout_seconds", float("-inf")),
        ("relay_idle_timeout_seconds", 0.0),
        ("relay_idle_timeout_seconds", -1.0),
        ("relay_idle_timeout_seconds", "300"),
        ("relay_idle_timeout_seconds", 10**1000),
        ("bootstrap_timeout_seconds", True),
        ("bootstrap_timeout_seconds", float("nan")),
        ("bootstrap_timeout_seconds", float("inf")),
        ("bootstrap_timeout_seconds", float("-inf")),
        ("bootstrap_timeout_seconds", 599.9),
        ("bootstrap_timeout_seconds", 86400.1),
        ("bootstrap_timeout_seconds", "600"),
        ("bootstrap_timeout_seconds", 10**1000),
        ("storage_encryption_confirmed", False),
        ("clock", None),
        ("clock", 1),
        ("sleep", None),
        ("sleep", 1),
    ),
)
def test_constructor_rejects_invalid_local_configuration(
    field: str,
    invalid_value: object,
) -> None:
    """Invalid local configuration must fail before installation can start."""
    from yinshi.services.managed_guest_installer import ManagedGuestInstaller

    async def no_sleep(seconds: float) -> None:
        del seconds

    options: dict[str, Any] = {
        "client": FakeSpritesClient(),
        "bootstrap_script": b"#!/bin/bash\n",
        "relay_idle_timeout_seconds": 300.0,
        "bootstrap_timeout_seconds": 600.0,
        "storage_encryption_confirmed": True,
        "clock": lambda: 0.0,
        "sleep": no_sleep,
    }
    options[field] = invalid_value

    with pytest.raises(ValueError):
        ManagedGuestInstaller(**options)


def _installer(
    client: FakeSpritesClient,
    *,
    clock: Callable[[], float] = lambda: 0.0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> Any:
    from yinshi.services.managed_guest_installer import ManagedGuestInstaller

    async def no_sleep(seconds: float) -> None:
        assert seconds == 1.0

    return ManagedGuestInstaller(
        client=client,
        bootstrap_script=b"#!/bin/bash\n",
        relay_idle_timeout_seconds=300.0,
        bootstrap_timeout_seconds=600.0,
        storage_encryption_confirmed=True,
        clock=clock,
        sleep=sleep or no_sleep,
    )


@pytest.mark.asyncio
async def test_install_writes_private_inputs_then_configures_private_services() -> None:
    """Install verified inputs before starting dependent private services."""
    client = FakeSpritesClient()

    await _installer(client).install(
        sprite_name="yinshi-managed",
        artifact=ARTIFACT,
        environment=dict(CLAIM_ENVIRONMENT),
        artifact_version="release-1",
        artifact_sha256=ARTIFACT_SHA256,
    )

    assert [(item.path, item.mode) for item in client.files] == [
        ("/var/lib/yinshi/.yinshi-encrypted-storage", "0600"),
        ("/home/sprite/.config/yinshi/artifact.tar.gz", "0600"),
        ("/home/sprite/.config/yinshi/bootstrap.sh", "0700"),
        ("/home/sprite/.config/yinshi/runner.env", "0600"),
    ]
    assert client.files[0].content == b"fly-sprites-encrypted-storage\n"
    env_text = client.files[3].content.decode("utf-8")
    assert "YINSHI_REGISTRATION_TOKEN=" in env_text
    assert "YINSHI_RUNNER_STORAGE_PROFILE=fly_sprites_posix\n" in env_text
    assert "YINSHI_RUNNER_DATA_DIR=/var/lib/yinshi\n" in env_text
    assert "YINSHI_RUNNER_USER_DATA_ENCRYPTION=required\n" in env_text
    assert "YINSHI_RUNNER_SPRITE_TASK_LEASE=enabled\n" in env_text
    assert "YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS=300\n" in env_text
    assert "YINSHI_RUNNER_ENV_FILE=/home/sprite/.config/yinshi/runner.env\n" in env_text
    assert f"YINSHI_RUNNER_ARTIFACT_SHA256={ARTIFACT_SHA256}\n" in env_text
    assert (
        "YINSHI_RUNNER_ARTIFACT_ATTESTATION_FILE=" "/opt/yinshi/current/.artifact-sha256\n"
    ) in env_text
    assert "SIDECAR_SOCKET_PATH=/var/lib/yinshi/sidecar.sock\n" in env_text

    assert [service["service_name"] for service in client.services] == [
        "yinshi-bootstrap",
        "yinshi-sidecar",
        "yinshi-runner",
    ]
    assert all(service["http_port"] is None for service in client.services)
    assert client.operation_calls.get("get_service", 0) == 0
    bootstrap, sidecar, runner = client.services
    assert bootstrap["args"] == (
        "/home/sprite/.config/yinshi/bootstrap.sh",
        "/home/sprite/.config/yinshi/artifact.tar.gz",
        ARTIFACT_SHA256,
        "release-1",
    )
    assert sidecar["environment"] == {"SIDECAR_SOCKET_PATH": "/var/lib/yinshi/sidecar.sock"}
    assert runner["needs"] == ("yinshi-sidecar",)
    assert runner["environment"] == {}
    runner_args = runner["args"]
    assert isinstance(runner_args, tuple)
    assert "/home/sprite/.config/yinshi/runner.env" in runner_args[1]
    assert "exec /opt/yinshi/current/venv/bin/python -m yinshi.runner_agent" in runner_args[1]
    assert "sprite-env services stop yinshi-runner" not in runner_args[1]
    assert CLAIM_ENVIRONMENT["YINSHI_REGISTRATION_TOKEN"] not in repr(runner_args)


@pytest.mark.asyncio
async def test_install_rejects_invalid_sprite_name_before_client_calls() -> None:
    """Invalid Sprite names must not reach provider methods."""
    client = FakeSpritesClient()

    with pytest.raises(ValueError):
        await _installer(client).install(
            sprite_name="Invalid",
            artifact=ARTIFACT,
            environment=dict(CLAIM_ENVIRONMENT),
            artifact_version="release-1",
            artifact_sha256=ARTIFACT_SHA256,
        )

    assert client.files == []
    assert client.services == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_artifact", [b"", "not-bytes"])
async def test_install_rejects_invalid_artifact_bytes_before_client_calls(
    invalid_artifact: object,
) -> None:
    """Artifact must be non-empty bounded bytes."""
    client = FakeSpritesClient()

    with pytest.raises(ValueError):
        await _installer(client).install(
            sprite_name="yinshi-managed",
            artifact=invalid_artifact,
            environment=dict(CLAIM_ENVIRONMENT),
            artifact_version="release-1",
            artifact_sha256=ARTIFACT_SHA256,
        )

    assert client.files == []
    assert client.services == []


@pytest.mark.asyncio
async def test_install_preserves_provider_cancellation() -> None:
    """Cancellation from a provider await must propagate unchanged."""
    cancellation = asyncio.CancelledError()
    client = FakeSpritesClient(fail_operation="write_file", failure=cancellation)

    with pytest.raises(asyncio.CancelledError) as raised:
        await _installer(client).install(
            sprite_name="yinshi-managed",
            artifact=ARTIFACT,
            environment=dict(CLAIM_ENVIRONMENT),
            artifact_version="release-1",
            artifact_sha256=ARTIFACT_SHA256,
        )

    assert raised.value is cancellation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "call_number", "stage"),
    [
        ("write_file", 1, "write_storage_marker"),
        ("write_file", 2, "write_artifact"),
        ("write_file", 3, "write_bootstrap"),
        ("write_file", 4, "write_runner_environment"),
        ("configure_service", 1, "configure_bootstrap"),
        ("configure_service", 2, "configure_sidecar"),
        ("configure_service", 3, "configure_runner"),
    ],
)
async def test_install_maps_provider_failures_to_fixed_local_error(
    operation: str,
    call_number: int,
    stage: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Provider failures must not expose provider response text."""
    client = FakeSpritesClient(fail_operation=operation, fail_call=call_number)

    with pytest.raises(RuntimeError) as raised:
        await _installer(client).install(
            sprite_name="yinshi-managed",
            artifact=ARTIFACT,
            environment=dict(CLAIM_ENVIRONMENT),
            artifact_version="release-1",
            artifact_sha256=ARTIFACT_SHA256,
        )

    assert str(raised.value) == "Managed Sprite installation failed"
    assert raised.value.__cause__ is None
    assert [record.getMessage() for record in caplog.records] == [
        f"managed_sprite_installation_failed stage={stage}"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_artifact_version",
    ["", "-release", "rélèase", "a" * 129, "release/path"],
)
async def test_install_rejects_invalid_artifact_version_before_client_calls(
    invalid_artifact_version: str,
) -> None:
    """Artifact versions must match the bootstrap release identifier contract."""
    client = FakeSpritesClient()

    with pytest.raises(ValueError):
        await _installer(client).install(
            sprite_name="yinshi-managed",
            artifact=ARTIFACT,
            environment=dict(CLAIM_ENVIRONMENT),
            artifact_version=invalid_artifact_version,
            artifact_sha256=ARTIFACT_SHA256,
        )

    assert client.files == []
    assert client.services == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_environment", _invalid_claim_environments())
async def test_install_rejects_invalid_claim_environment_before_client_calls(
    invalid_environment: dict[str, Any],
) -> None:
    """Only the exact bounded managed-runner claim environment is accepted."""
    client = FakeSpritesClient()

    with pytest.raises(ValueError):
        await _installer(client).install(
            sprite_name="yinshi-managed",
            artifact=ARTIFACT,
            environment=invalid_environment,
            artifact_version="release-1",
            artifact_sha256=ARTIFACT_SHA256,
        )

    assert client.files == []
    assert client.services == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_sha256",
    ["A" * 64, "0" * 64, "abc"],
)
async def test_install_rejects_invalid_or_mismatched_sha_before_client_calls(
    invalid_sha256: str,
) -> None:
    """SHA must be exact lowercase text matching artifact bytes."""
    client = FakeSpritesClient()

    with pytest.raises(ValueError):
        await _installer(client).install(
            sprite_name="yinshi-managed",
            artifact=ARTIFACT,
            environment=dict(CLAIM_ENVIRONMENT),
            artifact_version="release-1",
            artifact_sha256=invalid_sha256,
        )

    assert client.files == []
    assert client.services == []
