"""Install managed runner services inside a private Fly Sprite."""

from __future__ import annotations

import hashlib
import hmac
import re
import shlex
from collections.abc import Awaitable, Callable
from math import isfinite
from typing import Any, TypeVar

_CONFIG_ROOT = "/home/sprite/.config/yinshi"
_ARTIFACT_PATH = f"{_CONFIG_ROOT}/artifact.tar.gz"
_BOOTSTRAP_PATH = f"{_CONFIG_ROOT}/bootstrap.sh"
_RUNNER_ENV_PATH = f"{_CONFIG_ROOT}/runner.env"
_STORAGE_ENCRYPTION_MARKER_PATH = "/var/lib/yinshi/.yinshi-encrypted-storage"
_ARTIFACT_ATTESTATION_PATH = "/opt/yinshi/current/.artifact-sha256"
_MAX_FILE_BYTES = 10 * 1024 * 1024
_MAX_ENVIRONMENT_VALUE_CHARS = 4096
_MIN_BOOTSTRAP_TIMEOUT_SECONDS = 600.0
_MAX_BOOTSTRAP_TIMEOUT_SECONDS = 86400.0
_SPRITE_NAME_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_ARTIFACT_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_VARIABLE_CLAIM_KEYS = {"YINSHI_CONTROL_URL", "YINSHI_REGISTRATION_TOKEN"}
_FIXED_CLAIM_ENVIRONMENT = {
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
_CLAIM_KEYS = _VARIABLE_CLAIM_KEYS | _FIXED_CLAIM_ENVIRONMENT.keys()
_PROVIDER_ERROR = "Managed Sprite installation failed"
_Result = TypeVar("_Result")


def _validate_claim_environment(environment: dict[str, str]) -> None:
    """Reject claims outside the exact managed Fly Sprite runner contract."""
    if not isinstance(environment, dict) or environment.keys() != _CLAIM_KEYS:
        raise ValueError("environment must contain exactly the managed runner claim keys")
    if any(
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ENVIRONMENT_VALUE_CHARS
        or any(character in value for character in "\0\r\n")
        for value in environment.values()
    ):
        raise ValueError("environment values must be valid bounded strings")
    if any(environment[key] != value for key, value in _FIXED_CLAIM_ENVIRONMENT.items()):
        raise ValueError("environment fixed values do not match the managed runner profile")


async def _provider_call(call: Callable[[], Awaitable[_Result]]) -> _Result:
    """Run one provider call without exposing provider failure details."""
    try:
        return await call()
    except Exception:
        raise RuntimeError(_PROVIDER_ERROR) from None


class ManagedGuestInstaller:
    """Write verified inputs and configure private managed Sprite services."""

    def __init__(
        self,
        *,
        client: Any,
        bootstrap_script: bytes,
        relay_idle_timeout_seconds: float,
        bootstrap_timeout_seconds: float,
        storage_encryption_confirmed: bool,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        if (
            not isinstance(bootstrap_script, bytes)
            or not bootstrap_script
            or len(bootstrap_script) > _MAX_FILE_BYTES
        ):
            raise ValueError("bootstrap_script must be non-empty bounded bytes")
        if isinstance(relay_idle_timeout_seconds, bool) or not isinstance(
            relay_idle_timeout_seconds, (int, float)
        ):
            raise ValueError("relay_idle_timeout_seconds must be finite and positive")
        try:
            relay_idle_timeout = float(relay_idle_timeout_seconds)
        except OverflowError:
            raise ValueError("relay_idle_timeout_seconds must be finite and positive") from None
        if not isfinite(relay_idle_timeout) or relay_idle_timeout <= 0:
            raise ValueError("relay_idle_timeout_seconds must be finite and positive")
        if isinstance(bootstrap_timeout_seconds, bool) or not isinstance(
            bootstrap_timeout_seconds, (int, float)
        ):
            raise ValueError("bootstrap_timeout_seconds must be between 600 and 86400")
        try:
            bootstrap_timeout = float(bootstrap_timeout_seconds)
        except OverflowError:
            raise ValueError("bootstrap_timeout_seconds must be between 600 and 86400") from None
        if (
            not isfinite(bootstrap_timeout)
            or not _MIN_BOOTSTRAP_TIMEOUT_SECONDS
            <= bootstrap_timeout
            <= _MAX_BOOTSTRAP_TIMEOUT_SECONDS
        ):
            raise ValueError("bootstrap_timeout_seconds must be between 600 and 86400")
        if storage_encryption_confirmed is not True:
            raise ValueError("storage_encryption_confirmed must be explicitly true")
        if not callable(clock):
            raise ValueError("clock must be callable")
        if not callable(sleep):
            raise ValueError("sleep must be callable")
        self._client = client
        self._bootstrap_script = bootstrap_script
        self._relay_idle_timeout_seconds = relay_idle_timeout
        self._bootstrap_timeout_seconds = bootstrap_timeout
        self._clock = clock
        self._sleep = sleep

    async def install(
        self,
        *,
        sprite_name: str,
        artifact: bytes,
        environment: dict[str, str],
        artifact_version: str,
        artifact_sha256: str,
    ) -> None:
        """Install one verified release and start its private services."""
        if _SPRITE_NAME_PATTERN.fullmatch(sprite_name) is None:
            raise ValueError("sprite_name must be a lowercase DNS label of 1 to 63 characters")
        if not isinstance(artifact, bytes) or not artifact or len(artifact) > _MAX_FILE_BYTES:
            raise ValueError("artifact must be non-empty bytes within the 10 MiB limit")
        if (
            not isinstance(artifact_version, str)
            or _ARTIFACT_VERSION_PATTERN.fullmatch(artifact_version) is None
        ):
            raise ValueError("artifact_version must be a valid release identifier")
        if _SHA256_PATTERN.fullmatch(artifact_sha256) is None:
            raise ValueError("artifact_sha256 must be exactly 64 lowercase hexadecimal characters")
        actual_sha256 = hashlib.sha256(artifact).hexdigest()
        if not hmac.compare_digest(actual_sha256, artifact_sha256):
            raise ValueError("artifact does not match artifact_sha256")
        _validate_claim_environment(environment)
        await _provider_call(
            lambda: self._client.write_file(
                sprite_name,
                path=_STORAGE_ENCRYPTION_MARKER_PATH,
                content=b"fly-sprites-encrypted-storage\n",
                mode="0600",
                mkdir=True,
            )
        )
        await _provider_call(
            lambda: self._client.write_file(
                sprite_name,
                path=_ARTIFACT_PATH,
                content=artifact,
                mode="0600",
                mkdir=True,
            )
        )
        await _provider_call(
            lambda: self._client.write_file(
                sprite_name,
                path=_BOOTSTRAP_PATH,
                content=self._bootstrap_script,
                mode="0700",
                mkdir=True,
            )
        )
        runner_environment = dict(environment)
        runner_environment.update(
            {
                "YINSHI_RUNNER_STORAGE_PROFILE": "fly_sprites_posix",
                "YINSHI_RUNNER_DATA_DIR": "/var/lib/yinshi",
                "YINSHI_RUNNER_ARTIFACT_SHA256": artifact_sha256,
                "YINSHI_RUNNER_ARTIFACT_ATTESTATION_FILE": _ARTIFACT_ATTESTATION_PATH,
                "YINSHI_RUNNER_USER_DATA_ENCRYPTION": "required",
                "YINSHI_RUNNER_SPRITE_TASK_LEASE": "enabled",
                "YINSHI_RUNNER_RELAY_IDLE_TIMEOUT_SECONDS": (
                    f"{self._relay_idle_timeout_seconds:g}"
                ),
                "YINSHI_RUNNER_ENV_FILE": _RUNNER_ENV_PATH,
                "SIDECAR_SOCKET_PATH": "/var/lib/yinshi/sidecar.sock",
            }
        )
        runner_env = "".join(
            f"{key}={shlex.quote(runner_environment[key])}\n" for key in sorted(runner_environment)
        ).encode("utf-8")
        await _provider_call(
            lambda: self._client.write_file(
                sprite_name,
                path=_RUNNER_ENV_PATH,
                content=runner_env,
                mode="0600",
                mkdir=True,
            )
        )
        await _provider_call(
            lambda: self._client.configure_service(
                sprite_name,
                service_name="yinshi-bootstrap",
                command="/bin/bash",
                args=(
                    _BOOTSTRAP_PATH,
                    _ARTIFACT_PATH,
                    artifact_sha256,
                    artifact_version,
                ),
                environment={},
                directory=_CONFIG_ROOT,
                needs=(),
                http_port=None,
                monitor_duration=self._bootstrap_timeout_seconds,
            )
        )
        await _provider_call(
            lambda: self._client.configure_service(
                sprite_name,
                service_name="yinshi-sidecar",
                command="/usr/bin/env",
                args=("node", "/opt/yinshi/current/sidecar/src/index.js"),
                environment={"SIDECAR_SOCKET_PATH": "/var/lib/yinshi/sidecar.sock"},
                directory="/opt/yinshi/current/sidecar",
                needs=(),
                http_port=None,
                monitor_duration=None,
            )
        )
        runner_command = (
            "set -a; . /home/sprite/.config/yinshi/runner.env; set +a; "
            "/opt/yinshi/current/venv/bin/python -m yinshi.runner_agent; "
            "status=$?; "
            'if [ "$status" -eq 0 ]; then sprite-env services stop yinshi-runner; fi; '
            'exit "$status"'
        )
        await _provider_call(
            lambda: self._client.configure_service(
                sprite_name,
                service_name="yinshi-runner",
                command="/bin/bash",
                args=("-lc", runner_command),
                environment={},
                directory="/opt/yinshi/current/backend",
                needs=("yinshi-sidecar",),
                http_port=None,
                monitor_duration=None,
            )
        )
