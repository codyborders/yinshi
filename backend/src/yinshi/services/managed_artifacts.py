"""Secure retrieval for pinned managed guest artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from urllib.parse import urlsplit

import httpx

MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 10.0
TOTAL_TIMEOUT_SECONDS = 30.0


class ManagedArtifactError(Exception):
    """Base error for managed artifact retrieval."""


class ManagedArtifactValidationError(ManagedArtifactError):
    """Raised when artifact metadata is invalid."""


class ManagedArtifactHTTPError(ManagedArtifactError):
    """Raised when the artifact server returns a non-success response."""


class ManagedArtifactSizeError(ManagedArtifactError):
    """Raised when artifact size constraints fail."""


class ManagedArtifactTransportError(ManagedArtifactError):
    """Raised when artifact transport fails."""


class ManagedArtifactTimeoutError(ManagedArtifactError):
    """Raised when artifact retrieval times out."""


class ManagedArtifactDigestError(ManagedArtifactError):
    """Raised when artifact digest verification fails."""


def _validate_artifact_url(artifact_url: str) -> None:
    """Validate an artifact URL without including it in failure details."""
    try:
        parsed_url = urlsplit(artifact_url)
        has_user_information = parsed_url.username is not None or parsed_url.password is not None
        hostname = parsed_url.hostname
    except (TypeError, ValueError):
        raise ManagedArtifactValidationError("Invalid artifact URL") from None

    if (
        parsed_url.scheme != "https"
        or hostname is None
        or has_user_information
        or "#" in artifact_url
    ):
        raise ManagedArtifactValidationError("Invalid artifact URL")


def _validate_expected_sha256(expected_sha256: str) -> None:
    """Require a canonical lowercase SHA-256 digest."""
    if (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise ManagedArtifactValidationError("Invalid artifact digest")


def _decode_content_payload(response: httpx.Response, encoded: bytes) -> bytes:
    """Decode one bounded payload using its declared encoded representation."""
    content_encoding = response.headers.get("content-encoding", "").strip().lower()
    if content_encoding in ("", "identity"):
        return encoded
    decoded_response = httpx.Response(
        200,
        headers={"Content-Encoding": content_encoding},
        content=encoded,
    )
    decoded: bytearray = bytearray()
    for chunk in decoded_response.iter_bytes():
        decoded.extend(chunk)
        if len(decoded) > MAX_ARTIFACT_BYTES:
            raise ManagedArtifactSizeError("Invalid artifact size")
    return bytes(decoded)


async def fetch_pinned_artifact(
    http_client: httpx.AsyncClient,
    artifact_url: str,
    expected_sha256: str,
) -> bytes:
    """Fetch one bounded HTTPS artifact and verify its pinned SHA-256 digest."""
    _validate_artifact_url(artifact_url)
    _validate_expected_sha256(expected_sha256)
    request_timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT_SECONDS,
        read=READ_TIMEOUT_SECONDS,
        write=CONNECT_TIMEOUT_SECONDS,
        pool=CONNECT_TIMEOUT_SECONDS,
    )
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            async with http_client.stream(
                "GET",
                artifact_url,
                follow_redirects=False,
                timeout=request_timeout,
            ) as response:
                response.raise_for_status()
                content_length_values = response.headers.get_list("content-length")
                declared_length: int | None = None
                if content_length_values:
                    declared_value = content_length_values[0]
                    if (
                        len(content_length_values) != 1
                        or re.fullmatch(r"[0-9]+", declared_value) is None
                    ):
                        raise ManagedArtifactSizeError("Invalid artifact size")
                    declared_length = int(declared_value)
                    if declared_length > MAX_ARTIFACT_BYTES:
                        raise ManagedArtifactSizeError("Invalid artifact size")
                if response.is_stream_consumed:
                    encoded_content = response.content
                    if len(encoded_content) > MAX_ARTIFACT_BYTES:
                        raise ManagedArtifactSizeError("Invalid artifact size")
                else:
                    content_buffer = bytearray()
                    async for chunk in response.aiter_raw():
                        content_buffer.extend(chunk)
                        if len(content_buffer) > MAX_ARTIFACT_BYTES:
                            raise ManagedArtifactSizeError("Invalid artifact size")
                    encoded_content = bytes(content_buffer)
                if declared_length is not None and len(encoded_content) != declared_length:
                    raise ManagedArtifactSizeError("Invalid artifact size")
            content = _decode_content_payload(response, encoded_content)
            if not content or len(content) > MAX_ARTIFACT_BYTES:
                raise ManagedArtifactSizeError("Invalid artifact size")
    except httpx.HTTPStatusError:
        raise ManagedArtifactHTTPError("Artifact HTTP failure") from None
    except httpx.TimeoutException:
        raise ManagedArtifactTimeoutError("Artifact retrieval timed out") from None
    except httpx.RequestError:
        raise ManagedArtifactTransportError("Artifact transport failure") from None
    except TimeoutError:
        raise ManagedArtifactTimeoutError("Artifact retrieval timed out") from None

    actual_sha256 = hashlib.sha256(content).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise ManagedArtifactDigestError("Artifact digest mismatch")
    return content
