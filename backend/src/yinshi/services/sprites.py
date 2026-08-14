"""Validated asynchronous client for the Fly Sprites HTTP API."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Literal, cast

import httpx


class SpritesProtocolError(RuntimeError):
    """Raised when Fly returns an invalid Sprite response."""


class SpritesProviderError(RuntimeError):
    """Raised when Fly rejects or cannot complete a request."""


_SPRITE_NAME_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SERVICE_NAME_PATTERN = _SPRITE_NAME_PATTERN
_CHECKPOINT_ID_PATTERN = re.compile(r"v[0-9]+\Z")
_FILE_MODE_PATTERN = re.compile(r"0[0-7]{3}\Z")
_DNS_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_POSIX_ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_STANDARD_OPERATION_TIMEOUT_SECONDS = 30.0
_LONG_OPERATION_TIMEOUT_SECONDS = 120.0
_SERVICE_STREAM_TRANSPORT_MARGIN_SECONDS = 30.0
_MAX_STREAM_RESPONSE_BYTES = 1024 * 1024
_MAX_STREAM_EVENTS = 1000
_MAX_PATH_LENGTH = 4096
_MAX_FILE_CONTENT_BYTES = 10 * 1024 * 1024
_MAX_SPRITE_ID_LENGTH = 256
_MAX_SPRITE_STATUS_LENGTH = 64
_MAX_SPRITE_INVENTORY_PAGES = 100
_MAX_SPRITE_INVENTORY_ITEMS = 5000
_MAX_CONTINUATION_TOKEN_LENGTH = 4096
_MAX_SERVICE_COMMAND_LENGTH = 4096
_MAX_SERVICE_LIST_ITEMS = 256
_MAX_SERVICE_VALUE_LENGTH = 4096
_MAX_ALLOWED_DOMAINS = 256
_MAX_DNS_NAME_LENGTH = 253
_MAX_CHECKPOINT_COMMENT_LENGTH = 4096
_MAX_MONITOR_DURATION_SECONDS = 86400.0
_MAX_STATE_STARTED_AT_LENGTH = 128
_MAX_STATE_ERROR_LENGTH = 4096
_FILE_TRANSFER_CHUNK_BYTES = 4 * 1024 * 1024
_FILE_TRANSFER_BYTES_MAX = 200 * 1024 * 1024 * 1024
_CONTENT_RANGE_PATTERN = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)\Z")


async def _read_bounded_response(
    response: httpx.Response,
    description: str,
    *,
    max_bytes: int = _MAX_STREAM_RESPONSE_BYTES,
    maximum: int = _MAX_STREAM_RESPONSE_BYTES,
) -> bytes:
    """Read one provider response without exceeding its byte limit."""
    if type(maximum) is not int or not 1 <= maximum <= _MAX_FILE_CONTENT_BYTES:
        raise ValueError("maximum is outside the provider response limit")
    if type(max_bytes) is not int or not 1 <= max_bytes <= maximum:
        raise ValueError("max_bytes is outside the provider response limit")
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = 0
        if declared_length > max_bytes:
            raise SpritesProtocolError(f"{description} exceeds size limit")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > max_bytes:
            raise SpritesProtocolError(f"{description} exceeds size limit")
        body.extend(chunk)
    return bytes(body)


def _validate_sprite_name(name: str) -> str:
    """Require one lowercase provider-safe DNS label."""
    if not isinstance(name, str) or _SPRITE_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("Sprite name must be a lowercase DNS label of 1 to 63 characters")
    return name


def _validate_service_name(name: str) -> str:
    """Require one lowercase provider-safe service label."""
    if not isinstance(name, str) or _SERVICE_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("Service name must be a lowercase DNS label of 1 to 63 characters")
    return name


def _validate_monitor_duration(monitor_duration: float | None) -> float | None:
    """Require a finite positive provider monitoring duration within one day."""
    if monitor_duration is None:
        return None
    if isinstance(monitor_duration, bool) or not isinstance(monitor_duration, (int, float)):
        raise ValueError("Monitor duration must be between 0 and 86400 seconds")
    try:
        validated_duration = float(monitor_duration)
    except OverflowError:
        raise ValueError("Monitor duration must be between 0 and 86400 seconds") from None
    if (
        not isfinite(validated_duration)
        or not 0 < validated_duration <= _MAX_MONITOR_DURATION_SECONDS
    ):
        raise ValueError("Monitor duration must be between 0 and 86400 seconds")
    return validated_duration


def _service_stream_timeout(monitor_duration: float | None) -> float:
    """Allow provider monitoring plus a bounded transport completion margin."""
    if monitor_duration is None:
        return _LONG_OPERATION_TIMEOUT_SECONDS
    return max(
        _LONG_OPERATION_TIMEOUT_SECONDS,
        monitor_duration + _SERVICE_STREAM_TRANSPORT_MARGIN_SECONDS,
    )


def _validate_service_command(command: str) -> str:
    """Require one bounded nonblank command without a null byte."""
    if (
        not isinstance(command, str)
        or not command.strip()
        or len(command) > _MAX_SERVICE_COMMAND_LENGTH
        or "\x00" in command
    ):
        raise ValueError("Command must be bounded nonblank text without null bytes")
    return command


def _validate_service_args(args: tuple[str, ...]) -> tuple[str, ...]:
    """Require a bounded tuple of bounded string arguments."""
    if (
        not isinstance(args, tuple)
        or len(args) > _MAX_SERVICE_LIST_ITEMS
        or not all(
            isinstance(value, str)
            and len(value) <= _MAX_SERVICE_VALUE_LENGTH
            and "\x00" not in value
            for value in args
        )
    ):
        raise ValueError("Service args must be a bounded tuple of strings")
    return args


def _validate_service_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Require bounded POSIX environment names and bounded string values."""
    if not isinstance(environment, Mapping) or len(environment) > _MAX_SERVICE_LIST_ITEMS:
        raise ValueError("Service environment must be a bounded mapping")
    for key, value in environment.items():
        if (
            not isinstance(key, str)
            or len(key) > _MAX_SERVICE_VALUE_LENGTH
            or _POSIX_ENVIRONMENT_NAME_PATTERN.fullmatch(key) is None
            or not isinstance(value, str)
            or len(value) > _MAX_SERVICE_VALUE_LENGTH
            or "\x00" in value
        ):
            raise ValueError("Service environment keys and values are invalid")
    return dict(environment)


def _validate_service_dependencies(needs: tuple[str, ...]) -> tuple[str, ...]:
    """Require unique bounded service labels in one tuple."""
    if not isinstance(needs, tuple) or len(needs) > _MAX_SERVICE_LIST_ITEMS:
        raise ValueError("Service dependencies must be a bounded tuple")
    validated = tuple(_validate_service_name(value) for value in needs)
    if len(set(validated)) != len(validated):
        raise ValueError("Service dependencies must not contain duplicates")
    return validated


def _validate_http_port(http_port: int | None) -> int | None:
    """Require an exact TCP port integer when present."""
    if http_port is not None and (type(http_port) is not int or not 1 <= http_port <= 65535):
        raise ValueError("HTTP port must be an integer between 1 and 65535")
    return http_port


def _validate_checkpoint_comment(comment: str) -> str:
    """Require bounded printable nonblank checkpoint text."""
    if (
        not isinstance(comment, str)
        or not comment.strip()
        or len(comment) > _MAX_CHECKPOINT_COMMENT_LENGTH
        or not all(character.isprintable() for character in comment)
    ):
        raise ValueError("Checkpoint comment must be bounded safe nonblank text")
    return comment


def _validate_checkpoint_id(checkpoint_id: str) -> str:
    """Require the provider checkpoint identifier format."""
    if (
        not isinstance(checkpoint_id, str)
        or _CHECKPOINT_ID_PATTERN.fullmatch(checkpoint_id) is None
    ):
        raise ValueError("Checkpoint ID must use the provider v<number> format")
    return checkpoint_id


def _validate_file_path(path: str, *, field: str, allow_root: bool) -> str:
    """Require a bounded absolute POSIX path without traversal."""
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > _MAX_PATH_LENGTH
        or "\x00" in path
        or any(part in {".", ".."} for part in path.split("/"))
        or (not allow_root and path == "/")
    ):
        raise ValueError(f"{field} must be a safe absolute path")
    return path


def _validate_allowed_domains(domains: tuple[str, ...]) -> tuple[str, ...]:
    """Require a bounded tuple of unique public DNS allow rules."""
    if not isinstance(domains, tuple) or len(domains) > _MAX_ALLOWED_DOMAINS:
        raise ValueError("Allowed domains must be a bounded tuple")
    if not all(isinstance(entry, str) for entry in domains):
        raise ValueError("Allowed domains must contain public DNS names")
    if len(set(domains)) != len(domains):
        raise ValueError("Allowed domains must not contain duplicates")
    for entry in domains:
        if entry == "*":
            raise ValueError("Allowed domains must contain public DNS names")
        domain = entry[2:] if entry.startswith("*.") else entry
        labels = domain.split(".")
        if (
            "*" in domain
            or len(domain) > _MAX_DNS_NAME_LENGTH
            or len(labels) < 2
            or any(_DNS_LABEL_PATTERN.fullmatch(label) is None for label in labels)
            or labels[-1].isdigit()
        ):
            raise ValueError("Allowed domains must contain public DNS names")
        try:
            ipaddress.ip_address(domain)
        except ValueError:
            continue
        raise ValueError("Allowed domains must not contain IP addresses")
    return domains


@contextmanager
def _translate_transport_errors(operation: str) -> Iterator[None]:
    """Replace request-bearing transport errors with a stable provider error."""
    try:
        yield
    except (httpx.RequestError, TimeoutError):
        raise SpritesProviderError(f"Fly could not {operation}") from None


@dataclass(frozen=True, slots=True)
class SpriteFileTransfer:
    """Validated metadata for one completed Sprite file transfer."""

    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SpriteInventoryRecord:
    """Minimal provider inventory identity from the list endpoint."""

    name: str


@dataclass(frozen=True, slots=True)
class SpriteRecord:
    """Provider identity and lifecycle state for one Sprite."""

    id: str
    name: str
    status: str
    created_at: datetime | None = None


ServiceStatus = Literal["stopped", "starting", "running", "stopping", "failed"]
_SERVICE_STATUSES = {"stopped", "starting", "running", "stopping", "failed"}


@dataclass(frozen=True, slots=True)
class ServiceState:
    """Current provider runtime state for one Sprite service."""

    name: str
    status: ServiceStatus
    pid: int | None
    started_at: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ServiceRecord:
    """Provider definition and runtime state for one Sprite service."""

    name: str
    command: str
    args: tuple[str, ...]
    needs: tuple[str, ...]
    http_port: int | None
    state: ServiceState | None


def _raise_for_stream_error(response: httpx.Response, operation: str) -> None:
    """Translate an NDJSON error event without copying provider details."""
    if len(response.content) > _MAX_STREAM_RESPONSE_BYTES:
        raise SpritesProtocolError(f"Sprite {operation} response exceeds size limit")
    lines = [line for line in response.text.splitlines() if line.strip()]
    if len(lines) > _MAX_STREAM_EVENTS:
        raise SpritesProtocolError(f"Sprite {operation} response exceeds event limit")
    try:
        events = [json.loads(line) for line in lines]
    except json.JSONDecodeError:
        raise SpritesProtocolError(f"Sprite {operation} response is not valid NDJSON") from None
    if any(isinstance(event, dict) and event.get("type") == "error" for event in events):
        raise SpritesProtocolError(f"Sprite {operation} failed")
    if any(
        isinstance(event, dict)
        and event.get("type") == "exit"
        and (type(event.get("exit_code")) is not int or event.get("exit_code") != 0)
        for event in events
    ):
        raise SpritesProtocolError(f"Sprite {operation} failed")
    if not events or not isinstance(events[-1], dict) or events[-1].get("type") != "complete":
        raise SpritesProtocolError(f"Sprite {operation} response did not complete")


def _parse_sprite_response(
    response: httpx.Response,
    expected_name: str,
) -> SpriteRecord:
    """Decode and validate one provider Sprite response."""
    try:
        payload = response.json()
    except ValueError:
        raise SpritesProtocolError("Sprite response is not valid JSON") from None
    return _parse_sprite_record(payload, expected_name)


def _parse_provider_timestamp(value: object) -> datetime | None:
    """Parse one optional provider timestamp into an aware UTC value."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 128:
        raise SpritesProtocolError("Sprite response creation timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SpritesProtocolError("Sprite response creation timestamp is invalid") from None
    if parsed.tzinfo is None:
        raise SpritesProtocolError("Sprite response creation timestamp is invalid")
    return parsed.astimezone(timezone.utc)


def _parse_sprite_record(payload: object, expected_name: str) -> SpriteRecord:
    """Validate one provider Sprite record."""
    if not isinstance(payload, dict):
        raise SpritesProtocolError("Sprite response record is invalid")
    values = (payload.get("id"), payload.get("name"), payload.get("status"))
    if any(not isinstance(value, str) or not value for value in values):
        raise SpritesProtocolError("Sprite response record is invalid")
    if (
        len(payload["id"]) > _MAX_SPRITE_ID_LENGTH
        or len(payload["status"]) > _MAX_SPRITE_STATUS_LENGTH
    ):
        raise SpritesProtocolError("Sprite response record is invalid")
    if payload["name"] != expected_name:
        raise SpritesProtocolError("Sprite response name does not match the request")
    return SpriteRecord(
        id=payload["id"],
        name=payload["name"],
        status=payload["status"],
        created_at=_parse_provider_timestamp(payload.get("created_at")),
    )


def _parse_service_response(response: httpx.Response, expected_name: str) -> ServiceRecord:
    """Decode and validate one provider service response."""
    try:
        payload = response.json()
    except ValueError:
        raise SpritesProtocolError("Sprite service response is not valid JSON") from None
    if not isinstance(payload, dict):
        raise SpritesProtocolError("Sprite service response is invalid")
    name = payload.get("name")
    command = payload.get("cmd")
    args = payload.get("args")
    needs = payload.get("needs")
    http_port = payload.get("http_port")
    if (
        name != expected_name
        or not isinstance(command, str)
        or not command
        or len(command) > _MAX_SERVICE_COMMAND_LENGTH
    ):
        raise SpritesProtocolError("Sprite service response is invalid")
    if (
        not isinstance(args, list)
        or len(args) > _MAX_SERVICE_LIST_ITEMS
        or not all(
            isinstance(value, str) and len(value) <= _MAX_SERVICE_VALUE_LENGTH for value in args
        )
    ):
        raise SpritesProtocolError("Sprite service response is invalid")
    if needs is None:
        needs = []
    if (
        not isinstance(needs, list)
        or len(needs) > _MAX_SERVICE_LIST_ITEMS
        or not all(
            isinstance(value, str) and len(value) <= _MAX_SERVICE_VALUE_LENGTH for value in needs
        )
    ):
        raise SpritesProtocolError("Sprite service response is invalid")
    if http_port is not None and (
        not isinstance(http_port, int) or isinstance(http_port, bool) or not 1 <= http_port <= 65535
    ):
        raise SpritesProtocolError("Sprite service response is invalid")
    return ServiceRecord(
        name=name,
        command=command,
        args=tuple(args),
        needs=tuple(needs),
        http_port=http_port,
        state=_parse_service_state(payload.get("state"), expected_name),
    )


def _parse_service_state(payload: object, expected_name: str) -> ServiceState | None:
    """Validate optional runtime state from a service response."""
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise SpritesProtocolError("Sprite service state is invalid")
    name = payload.get("name")
    status = payload.get("status")
    pid = payload.get("pid")
    started_at = payload.get("started_at")
    error = payload.get("error")
    if name != expected_name or status not in _SERVICE_STATUSES:
        raise SpritesProtocolError("Sprite service state is invalid")
    if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool)):
        raise SpritesProtocolError("Sprite service state is invalid")
    if started_at is not None and (
        not isinstance(started_at, str) or len(started_at) > _MAX_STATE_STARTED_AT_LENGTH
    ):
        raise SpritesProtocolError("Sprite service state is invalid")
    if error is not None and (not isinstance(error, str) or len(error) > _MAX_STATE_ERROR_LENGTH):
        raise SpritesProtocolError("Sprite service state is invalid")
    return ServiceState(
        name=name,
        status=cast(ServiceStatus, status),
        pid=pid,
        started_at=started_at,
        error=error,
    )


class SpritesClient:
    """Call the Fly Sprites API without exposing provider details to callers."""

    def __init__(self, *, api_token: str, http_client: httpx.AsyncClient) -> None:
        if not isinstance(api_token, str) or not api_token.strip():
            raise ValueError("api_token must be a non-empty string")
        self._api_token = api_token
        self._http_client = http_client

    async def list_sprites(self, *, prefix: str) -> tuple[SpriteInventoryRecord, ...]:
        """Return complete provider inventory for one exact managed prefix."""
        if not isinstance(prefix, str) or not prefix or len(prefix) > 63:
            raise ValueError("prefix must be bounded non-empty text")
        records: list[SpriteInventoryRecord] = []
        names: set[str] = set()
        tokens: set[str] = set()
        continuation_token: str | None = None
        for _ in range(_MAX_SPRITE_INVENTORY_PAGES):
            params = {"prefix": prefix, "max_results": "50"}
            if continuation_token is not None:
                params["continuation_token"] = continuation_token
            with _translate_transport_errors("list Sprites"):
                async with asyncio.timeout(_STANDARD_OPERATION_TIMEOUT_SECONDS):
                    async with self._http_client.stream(
                        "GET",
                        "/v1/sprites",
                        headers={"Authorization": f"Bearer {self._api_token}"},
                        params=params,
                        timeout=_STANDARD_OPERATION_TIMEOUT_SECONDS,
                    ) as response:
                        body = await _read_bounded_response(response, "Sprite inventory response")
                        status_code = response.status_code
            if not 200 <= status_code < 300:
                raise SpritesProviderError(
                    f"Fly could not list Sprites (status {status_code})"
                ) from None
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise SpritesProtocolError("Sprite inventory response is not valid JSON") from None
            if not isinstance(payload, dict):
                raise SpritesProtocolError("Sprite inventory response is invalid")
            entries = payload.get("sprites")
            has_more = payload.get("has_more")
            has_next_token_field = "next_continuation_token" in payload
            next_token = payload.get("next_continuation_token")
            if not isinstance(entries, list) or len(entries) > 50 or type(has_more) is not bool:
                raise SpritesProtocolError("Sprite inventory response is invalid")
            page_names: list[str] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    raise SpritesProtocolError("Sprite inventory record is invalid")
                name = entry.get("name")
                if not isinstance(name, str) or _SPRITE_NAME_PATTERN.fullmatch(name) is None:
                    raise SpritesProtocolError("Sprite inventory record is invalid")
                if not name.startswith(prefix) or name in names:
                    raise SpritesProtocolError("Sprite inventory record is invalid")
                names.add(name)
                page_names.append(name)
            if len(names) > _MAX_SPRITE_INVENTORY_ITEMS:
                raise SpritesProtocolError("Sprite inventory exceeds item limit")
            records.extend(SpriteInventoryRecord(name=name) for name in page_names)
            if not has_more or (has_next_token_field and next_token is None):
                if not has_more and next_token is not None:
                    raise SpritesProtocolError("Sprite inventory continuation is invalid")
                return tuple(records)
            if (
                not isinstance(next_token, str)
                or not next_token
                or len(next_token) > _MAX_CONTINUATION_TOKEN_LENGTH
                or next_token in tokens
            ):
                raise SpritesProtocolError("Sprite inventory continuation is invalid")
            tokens.add(next_token)
            continuation_token = next_token
        raise SpritesProtocolError("Sprite inventory exceeds page limit")

    async def create_sprite(self, name: str) -> SpriteRecord:
        """Create one private Sprite and return its validated provider record."""
        name = _validate_sprite_name(name)
        with _translate_transport_errors("create Sprite"):
            async with asyncio.timeout(_LONG_OPERATION_TIMEOUT_SECONDS):
                async with self._http_client.stream(
                    "POST",
                    "/v1/sprites",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    json={
                        "name": name,
                        "url_settings": {"auth": "sprite"},
                        "wait_for_capacity": True,
                    },
                    timeout=_LONG_OPERATION_TIMEOUT_SECONDS,
                ) as response:
                    body = await _read_bounded_response(response, "Sprite response")
                    status_code = response.status_code
        if not 200 <= status_code < 300:
            raise SpritesProviderError(
                f"Fly could not create Sprite (status {status_code})"
            ) from None
        return _parse_sprite_response(httpx.Response(status_code, content=body), name)

    async def get_sprite(self, name: str) -> SpriteRecord | None:
        """Return one Sprite or None when Fly reports it missing."""
        name = _validate_sprite_name(name)
        with _translate_transport_errors("get Sprite"):
            async with asyncio.timeout(_STANDARD_OPERATION_TIMEOUT_SECONDS):
                async with self._http_client.stream(
                    "GET",
                    f"/v1/sprites/{name}",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    timeout=_STANDARD_OPERATION_TIMEOUT_SECONDS,
                ) as response:
                    body = await _read_bounded_response(response, "Sprite response")
                    status_code = response.status_code
        if status_code == 404:
            return None
        if not 200 <= status_code < 300:
            raise SpritesProviderError(f"Fly could not get Sprite (status {status_code})") from None
        return _parse_sprite_response(httpx.Response(status_code, content=body), name)

    async def set_network_policy(
        self,
        name: str,
        *,
        allowed_domains: tuple[str, ...],
    ) -> None:
        """Restrict Sprite egress to explicit domains."""
        name = _validate_sprite_name(name)
        allowed_domains = _validate_allowed_domains(allowed_domains)
        rules = [{"action": "allow", "domain": domain} for domain in allowed_domains]
        rules.append({"action": "deny", "domain": "*"})
        with _translate_transport_errors("set network policy"):
            async with asyncio.timeout(_STANDARD_OPERATION_TIMEOUT_SECONDS):
                async with self._http_client.stream(
                    "POST",
                    f"/v1/sprites/{name}/policy/network",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    json={"rules": rules},
                    timeout=_STANDARD_OPERATION_TIMEOUT_SECONDS,
                ) as response:
                    body = await _read_bounded_response(
                        response,
                        "Sprite network policy response",
                    )
                    status_code = response.status_code
        if not 200 <= status_code < 300:
            raise SpritesProviderError(
                f"Fly could not set network policy (status {status_code})"
            ) from None
        if not body:
            return
        try:
            payload = httpx.Response(status_code, content=body).json()
        except ValueError:
            raise SpritesProtocolError("Sprite network policy response is not valid JSON") from None
        if not isinstance(payload, dict) or payload.get("rules") != rules:
            raise SpritesProtocolError("Sprite network policy response is invalid")

    async def write_file(
        self,
        name: str,
        *,
        path: str,
        content: bytes,
        mode: str,
        mkdir: bool,
    ) -> None:
        """Write raw bytes to one Sprite filesystem path."""
        name = _validate_sprite_name(name)
        path = _validate_file_path(path, field="File path", allow_root=False)
        if not isinstance(content, bytes) or len(content) > _MAX_FILE_CONTENT_BYTES:
            raise ValueError("File content must be bytes within the 10 MiB limit")
        if not isinstance(mode, str) or _FILE_MODE_PATTERN.fullmatch(mode) is None:
            raise ValueError("File mode must be a four-digit octal permission string")
        if type(mkdir) is not bool:
            raise ValueError("mkdir must be a Boolean")
        with _translate_transport_errors("write Sprite file"):
            async with asyncio.timeout(_STANDARD_OPERATION_TIMEOUT_SECONDS):
                async with self._http_client.stream(
                    "PUT",
                    f"/v1/sprites/{name}/fs/write",
                    headers={
                        "Authorization": f"Bearer {self._api_token}",
                        "Content-Type": "application/octet-stream",
                    },
                    params={
                        "path": path,
                        "workingDir": "/",
                        "mode": mode,
                        "mkdir": str(mkdir).lower(),
                    },
                    content=content,
                    timeout=_STANDARD_OPERATION_TIMEOUT_SECONDS,
                ) as response:
                    await _read_bounded_response(response, "Sprite file write response")
                    status_code = response.status_code
        if not 200 <= status_code < 300:
            raise SpritesProviderError(
                f"Fly could not write Sprite file (status {status_code})"
            ) from None

    async def read_file(
        self,
        name: str,
        *,
        path: str,
        max_bytes: int,
    ) -> bytes:
        """Read one small guest file under an explicit byte limit."""
        name = _validate_sprite_name(name)
        path = _validate_file_path(path, field="File path", allow_root=False)
        if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_FILE_CONTENT_BYTES:
            raise ValueError("max_bytes is outside the small-file limit")
        with _translate_transport_errors("read Sprite file"):
            async with asyncio.timeout(_STANDARD_OPERATION_TIMEOUT_SECONDS):
                async with self._http_client.stream(
                    "GET",
                    f"/v1/sprites/{name}/fs/read",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    params={"path": path, "workingDir": "/"},
                    timeout=_STANDARD_OPERATION_TIMEOUT_SECONDS,
                ) as response:
                    body = await _read_bounded_response(
                        response,
                        "Sprite file read response",
                        max_bytes=max_bytes,
                        maximum=_MAX_FILE_CONTENT_BYTES,
                    )
                    status_code = response.status_code
        if not 200 <= status_code < 300:
            raise SpritesProviderError(
                f"Fly could not read Sprite file (status {status_code})"
            ) from None
        return body

    async def delete_file(
        self,
        name: str,
        *,
        path: str,
    ) -> None:
        """Delete one exact guest file without recursive behavior."""
        name = _validate_sprite_name(name)
        path = _validate_file_path(path, field="File path", allow_root=False)
        with _translate_transport_errors("delete Sprite file"):
            async with asyncio.timeout(_STANDARD_OPERATION_TIMEOUT_SECONDS):
                async with self._http_client.stream(
                    "DELETE",
                    f"/v1/sprites/{name}/fs/delete",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    params={"path": path, "workingDir": "/", "recursive": "false"},
                    timeout=_STANDARD_OPERATION_TIMEOUT_SECONDS,
                ) as response:
                    await _read_bounded_response(response, "Sprite file delete response")
                    status_code = response.status_code
        if status_code == 404:
            return
        if not 200 <= status_code < 300:
            raise SpritesProviderError(
                f"Fly could not delete Sprite file (status {status_code})"
            ) from None

    async def upload_file(
        self,
        name: str,
        *,
        source_path: Path,
        path: str,
        expected_size: int,
        expected_sha256: str,
        mode: str,
    ) -> SpriteFileTransfer:
        """Upload one verified ciphertext file without loading it into memory."""
        name = _validate_sprite_name(name)
        path = _validate_file_path(path, field="File path", allow_root=False)
        if (
            not isinstance(source_path, Path)
            or not source_path.is_file()
            or source_path.is_symlink()
        ):
            raise ValueError("source_path must be a regular pathlib.Path")
        if type(expected_size) is not int or not 1 <= expected_size <= _FILE_TRANSFER_BYTES_MAX:
            raise ValueError("expected_size is outside the transfer limit")
        if source_path.stat().st_size != expected_size:
            raise ValueError("source file size does not match expected_size")
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise ValueError("expected_sha256 must be 64 lowercase hexadecimal characters")
        if not isinstance(mode, str) or _FILE_MODE_PATTERN.fullmatch(mode) is None:
            raise ValueError("File mode must be a four-digit octal permission string")
        digest = hashlib.sha256()

        async def content() -> AsyncIterator[bytes]:
            source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(source_path, source_flags)
            try:
                with os.fdopen(descriptor, "rb", closefd=False) as source:
                    while chunk := await asyncio.to_thread(source.read, _FILE_TRANSFER_CHUNK_BYTES):
                        digest.update(chunk)
                        yield chunk
            finally:
                os.close(descriptor)

        with _translate_transport_errors("write Sprite file"):
            async with asyncio.timeout(_LONG_OPERATION_TIMEOUT_SECONDS):
                async with self._http_client.stream(
                    "PUT",
                    f"/v1/sprites/{name}/fs/write",
                    headers={
                        "Authorization": f"Bearer {self._api_token}",
                        "Content-Type": "application/octet-stream",
                    },
                    params={
                        "path": path,
                        "workingDir": "/",
                        "mode": mode,
                        "mkdir": "true",
                    },
                    content=content(),
                    timeout=_LONG_OPERATION_TIMEOUT_SECONDS,
                ) as response:
                    await _read_bounded_response(response, "Sprite file write response")
                    status_code = response.status_code
        if not 200 <= status_code < 300:
            raise SpritesProviderError(
                f"Fly could not write Sprite file (status {status_code})"
            ) from None
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256:
            raise SpritesProtocolError("Sprite upload source checksum did not match")
        return SpriteFileTransfer(size_bytes=expected_size, sha256=actual_sha256)

    async def download_file(
        self,
        name: str,
        *,
        path: str,
        target_path: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> SpriteFileTransfer:
        """Download one guest ciphertext file through validated byte ranges."""
        name = _validate_sprite_name(name)
        path = _validate_file_path(path, field="File path", allow_root=False)
        if not isinstance(target_path, Path) or not target_path.is_absolute():
            raise ValueError("target_path must be an absolute pathlib.Path")
        if target_path.exists() or target_path.is_symlink():
            raise FileExistsError(target_path)
        if type(expected_size) is not int or not 1 <= expected_size <= _FILE_TRANSFER_BYTES_MAX:
            raise ValueError("expected_size is outside the transfer limit")
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise ValueError("expected_sha256 must be 64 lowercase hexadecimal characters")
        target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        written = 0
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                while written < expected_size:
                    end = min(written + _FILE_TRANSFER_CHUNK_BYTES, expected_size) - 1
                    with _translate_transport_errors("read Sprite file"):
                        async with asyncio.timeout(_LONG_OPERATION_TIMEOUT_SECONDS):
                            async with self._http_client.stream(
                                "GET",
                                f"/v1/sprites/{name}/fs/read",
                                headers={
                                    "Authorization": f"Bearer {self._api_token}",
                                    "Range": f"bytes={written}-{end}",
                                },
                                params={"path": path, "workingDir": "/"},
                                timeout=_LONG_OPERATION_TIMEOUT_SECONDS,
                            ) as response:
                                if response.status_code != 206:
                                    raise SpritesProtocolError(
                                        "Sprite file read did not honor the byte range"
                                    )
                                content_range = response.headers.get("Content-Range", "")
                                match = _CONTENT_RANGE_PATTERN.fullmatch(content_range)
                                if (
                                    match is None
                                    or int(match.group(1)) != written
                                    or int(match.group(2)) != end
                                    or int(match.group(3)) != expected_size
                                ):
                                    raise SpritesProtocolError(
                                        "Sprite file read returned an invalid content range"
                                    )
                                range_bytes = 0
                                async for chunk in response.aiter_bytes():
                                    range_bytes += len(chunk)
                                    if range_bytes > end - written + 1:
                                        raise SpritesProtocolError(
                                            "Sprite file read exceeded the requested range"
                                        )
                                    output.write(chunk)
                                    digest.update(chunk)
                                if range_bytes != end - written + 1:
                                    raise SpritesProtocolError(
                                        "Sprite file read returned an incomplete range"
                                    )
                    written = end + 1
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            target_path.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256:
            target_path.unlink(missing_ok=True)
            raise SpritesProtocolError("Sprite file checksum did not match")
        os.chmod(target_path, 0o600)
        return SpriteFileTransfer(size_bytes=written, sha256=actual_sha256)

    async def configure_service(
        self,
        name: str,
        *,
        service_name: str,
        command: str,
        args: tuple[str, ...],
        environment: Mapping[str, str],
        directory: str,
        needs: tuple[str, ...],
        http_port: int | None = None,
        monitor_duration: float | None = None,
    ) -> None:
        """Create or update one named Sprite service."""
        name = _validate_sprite_name(name)
        service_name = _validate_service_name(service_name)
        monitor_duration = _validate_monitor_duration(monitor_duration)
        command = _validate_service_command(command)
        args = _validate_service_args(args)
        validated_environment = _validate_service_environment(environment)
        directory = _validate_file_path(directory, field="Service directory", allow_root=True)
        needs = _validate_service_dependencies(needs)
        http_port = _validate_http_port(http_port)
        payload: dict[str, object] = {
            "cmd": command,
            "args": list(args),
            "env": validated_environment,
            "dir": directory,
            "needs": list(needs),
        }
        if http_port is not None:
            payload["http_port"] = http_port
        params = None
        if monitor_duration is not None:
            params = {"duration": f"{monitor_duration:g}s"}
        operation = (
            "configure runner service" if service_name == "yinshi-runner" else "configure service"
        )
        stream_operation = "runner service" if service_name == "yinshi-runner" else "service"
        stream_timeout = _service_stream_timeout(monitor_duration)
        with _translate_transport_errors(operation):
            async with asyncio.timeout(stream_timeout):
                async with self._http_client.stream(
                    "PUT",
                    f"/v1/sprites/{name}/services/{service_name}",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    params=params,
                    json=payload,
                    timeout=stream_timeout,
                ) as response:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError:
                        raise SpritesProviderError(
                            f"Fly could not {operation} (status {response.status_code})"
                        ) from None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_STREAM_RESPONSE_BYTES:
                            raise SpritesProtocolError(
                                f"Sprite {stream_operation} response exceeds size limit"
                            )
                        body.extend(chunk)
        _raise_for_stream_error(
            httpx.Response(200, content=bytes(body)),
            stream_operation,
        )

    async def start_service(
        self,
        name: str,
        *,
        service_name: str,
        monitor_duration: float | None,
    ) -> None:
        """Start one service and require started plus complete progress events."""
        name = _validate_sprite_name(name)
        service_name = _validate_service_name(service_name)
        monitor_duration = _validate_monitor_duration(monitor_duration)
        params = None
        if monitor_duration is not None:
            params = {"duration": f"{monitor_duration:g}s"}
        stream_timeout = _service_stream_timeout(monitor_duration)
        with _translate_transport_errors("start service"):
            async with asyncio.timeout(stream_timeout):
                async with self._http_client.stream(
                    "POST",
                    f"/v1/sprites/{name}/services/{service_name}/start",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    params=params,
                    timeout=stream_timeout,
                ) as response:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError:
                        raise SpritesProviderError(
                            f"Fly could not start service (status {response.status_code})"
                        ) from None
                    body = await _read_bounded_response(response, "Sprite service start response")
        parsed = httpx.Response(200, content=body)
        _raise_for_stream_error(parsed, "service start")
        events = [json.loads(line) for line in parsed.text.splitlines() if line.strip()]
        if not any(isinstance(event, dict) and event.get("type") == "started" for event in events):
            raise SpritesProtocolError("Sprite service start response did not start")

    async def stop_service(
        self,
        name: str,
        *,
        service_name: str,
        timeout_seconds: int,
    ) -> None:
        """Stop one service and require stopped plus complete progress events."""
        name = _validate_sprite_name(name)
        service_name = _validate_service_name(service_name)
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 1 and 300")
        stream_timeout = float(timeout_seconds + 15)
        with _translate_transport_errors("stop service"):
            async with asyncio.timeout(stream_timeout):
                async with self._http_client.stream(
                    "POST",
                    f"/v1/sprites/{name}/services/{service_name}/stop",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    params={"timeout": f"{timeout_seconds}s"},
                    timeout=stream_timeout,
                ) as response:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError:
                        raise SpritesProviderError(
                            f"Fly could not stop service (status {response.status_code})"
                        ) from None
                    body = await _read_bounded_response(response, "Sprite service stop response")
        parsed = httpx.Response(200, content=body)
        _raise_for_stream_error(parsed, "service stop")
        events = [json.loads(line) for line in parsed.text.splitlines() if line.strip()]
        if not any(isinstance(event, dict) and event.get("type") == "stopped" for event in events):
            raise SpritesProtocolError("Sprite service stop response did not stop")

    async def restart_service(
        self,
        name: str,
        *,
        service_name: str,
        monitor_duration: float | None,
    ) -> None:
        """Restart one service and consume bounded provider progress."""
        name = _validate_sprite_name(name)
        service_name = _validate_service_name(service_name)
        monitor_duration = _validate_monitor_duration(monitor_duration)
        params = None
        if monitor_duration is not None:
            params = {"duration": f"{monitor_duration:g}s"}
        stream_timeout = _service_stream_timeout(monitor_duration)
        with _translate_transport_errors("restart service"):
            async with asyncio.timeout(stream_timeout):
                async with self._http_client.stream(
                    "POST",
                    f"/v1/sprites/{name}/services/{service_name}/restart",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    params=params,
                    timeout=stream_timeout,
                ) as response:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError:
                        raise SpritesProviderError(
                            f"Fly could not restart service (status {response.status_code})"
                        ) from None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_STREAM_RESPONSE_BYTES:
                            raise SpritesProtocolError(
                                "Sprite service restart response exceeds size limit"
                            )
                        body.extend(chunk)
        _raise_for_stream_error(
            httpx.Response(200, content=bytes(body)),
            "service restart",
        )

    async def get_service(
        self,
        name: str,
        *,
        service_name: str,
    ) -> ServiceRecord | None:
        """Return one typed Sprite service record when it exists."""
        name = _validate_sprite_name(name)
        service_name = _validate_service_name(service_name)
        with _translate_transport_errors("get service"):
            async with asyncio.timeout(_STANDARD_OPERATION_TIMEOUT_SECONDS):
                async with self._http_client.stream(
                    "GET",
                    f"/v1/sprites/{name}/services/{service_name}",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    timeout=_STANDARD_OPERATION_TIMEOUT_SECONDS,
                ) as response:
                    body = await _read_bounded_response(
                        response,
                        "Sprite service response",
                    )
                    status_code = response.status_code
        if status_code == 404:
            return None
        if not 200 <= status_code < 300:
            raise SpritesProviderError(
                f"Fly could not get service (status {status_code})"
            ) from None
        return _parse_service_response(
            httpx.Response(status_code, content=body),
            service_name,
        )

    async def configure_private_runner(
        self,
        name: str,
        *,
        command: str,
        args: tuple[str, ...],
        environment: Mapping[str, str],
        working_directory: str,
    ) -> None:
        """Create or update the private Yinshi runner service."""
        await self.configure_service(
            name,
            service_name="yinshi-runner",
            command=command,
            args=args,
            environment=environment,
            directory=working_directory,
            needs=(),
            http_port=None,
            monitor_duration=None,
        )

    async def wake_sprite(self, name: str) -> None:
        """Wake one cold Sprite through the simple HTTP Exec endpoint."""
        name = _validate_sprite_name(name)
        with _translate_transport_errors("wake Sprite"):
            async with asyncio.timeout(_STANDARD_OPERATION_TIMEOUT_SECONDS):
                async with self._http_client.stream(
                    "POST",
                    f"/v1/sprites/{name}/exec",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    params=[("cmd", "true")],
                    timeout=_STANDARD_OPERATION_TIMEOUT_SECONDS,
                ) as response:
                    await _read_bounded_response(response, "Sprite wake response")
                    status_code = response.status_code
        if not 200 <= status_code < 300:
            raise SpritesProviderError(
                f"Fly could not wake Sprite (status {status_code})"
            ) from None

    async def delete_sprite(self, name: str) -> None:
        """Permanently delete one Sprite."""
        name = _validate_sprite_name(name)
        with _translate_transport_errors("delete Sprite"):
            async with asyncio.timeout(_STANDARD_OPERATION_TIMEOUT_SECONDS):
                async with self._http_client.stream(
                    "DELETE",
                    f"/v1/sprites/{name}",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    timeout=_STANDARD_OPERATION_TIMEOUT_SECONDS,
                ) as response:
                    await _read_bounded_response(response, "Sprite delete response")
                    status_code = response.status_code
        if status_code == 404:
            return
        if not 200 <= status_code < 300:
            raise SpritesProviderError(
                f"Fly could not delete Sprite (status {status_code})"
            ) from None

    async def restore_checkpoint(self, name: str, *, checkpoint_id: str) -> None:
        """Restore one checkpoint and consume bounded provider progress."""
        name = _validate_sprite_name(name)
        checkpoint_id = _validate_checkpoint_id(checkpoint_id)
        with _translate_transport_errors("restore checkpoint"):
            async with asyncio.timeout(_LONG_OPERATION_TIMEOUT_SECONDS):
                async with self._http_client.stream(
                    "POST",
                    f"/v1/sprites/{name}/checkpoints/{checkpoint_id}/restore",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    timeout=_LONG_OPERATION_TIMEOUT_SECONDS,
                ) as response:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError:
                        raise SpritesProviderError(
                            f"Fly could not restore checkpoint (status {response.status_code})"
                        ) from None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_STREAM_RESPONSE_BYTES:
                            raise SpritesProtocolError(
                                "Sprite checkpoint restore response exceeds size limit"
                            )
                        body.extend(chunk)
        _raise_for_stream_error(
            httpx.Response(200, content=bytes(body)),
            "checkpoint restore",
        )

    async def create_checkpoint(self, name: str, *, comment: str) -> None:
        """Create a filesystem checkpoint after reading its progress response."""
        name = _validate_sprite_name(name)
        comment = _validate_checkpoint_comment(comment)
        with _translate_transport_errors("create checkpoint"):
            async with asyncio.timeout(_LONG_OPERATION_TIMEOUT_SECONDS):
                async with self._http_client.stream(
                    "POST",
                    f"/v1/sprites/{name}/checkpoint",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    json={"comment": comment},
                    timeout=_LONG_OPERATION_TIMEOUT_SECONDS,
                ) as response:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError:
                        raise SpritesProviderError(
                            f"Fly could not create checkpoint (status {response.status_code})"
                        ) from None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_STREAM_RESPONSE_BYTES:
                            raise SpritesProtocolError(
                                "Sprite checkpoint response exceeds size limit"
                            )
                        body.extend(chunk)
        _raise_for_stream_error(httpx.Response(200, content=bytes(body)), "checkpoint")
