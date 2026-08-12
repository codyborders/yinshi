"""Tests for pinned managed guest artifact retrieval."""

import hashlib
from collections.abc import AsyncIterator

import httpx
import pytest


async def test_fetch_pinned_artifact_returns_verified_content() -> None:
    """A bounded HTTPS response should be returned after digest verification."""
    from yinshi.services.managed_artifacts import fetch_pinned_artifact

    content = b"managed guest artifact"
    expected_sha256 = hashlib.sha256(content).hexdigest()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://artifacts.example/guest.tar.gz")
        assert request.extensions["timeout"]["connect"] == 5.0
        assert request.extensions["timeout"]["read"] == 10.0
        return httpx.Response(200, content=content)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        result = await fetch_pinned_artifact(
            client,
            "https://artifacts.example/guest.tar.gz",
            expected_sha256,
        )

    assert result == content


@pytest.mark.parametrize(
    "artifact_url",
    [
        "http://artifacts.example/guest.tar.gz",
        "https://user:secret@artifacts.example/guest.tar.gz",
        "https://artifacts.example/guest.tar.gz#fragment",
        "https:///guest.tar.gz",
    ],
)
async def test_fetch_pinned_artifact_rejects_invalid_url(artifact_url: str) -> None:
    """Only HTTPS URLs without user information or fragments are accepted."""
    from yinshi.services.managed_artifacts import (
        ManagedArtifactValidationError,
        fetch_pinned_artifact,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid URL must not be requested")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ManagedArtifactValidationError) as raised:
            await fetch_pinned_artifact(client, artifact_url, "0" * 64)

    assert str(raised.value) == "Invalid artifact URL"
    assert artifact_url not in str(raised.value)


async def test_fetch_pinned_artifact_validates_hash_before_request() -> None:
    """Expected SHA-256 must be canonical before transport starts."""
    from yinshi.services.managed_artifacts import (
        ManagedArtifactValidationError,
        fetch_pinned_artifact,
    )

    requested = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200, content=b"artifact")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ManagedArtifactValidationError, match="^Invalid artifact digest$"):
            await fetch_pinned_artifact(client, "https://artifacts.example/guest", "A" * 64)

    assert requested is False


async def test_fetch_pinned_artifact_rejects_redirect() -> None:
    """Redirects should become fixed local HTTP failures."""
    from yinshi.services.managed_artifacts import (
        ManagedArtifactHTTPError,
        fetch_pinned_artifact,
    )

    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(302, headers={"Location": "https://secret.example"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(ManagedArtifactHTTPError, match="^Artifact HTTP failure$"):
            await fetch_pinned_artifact(client, "https://artifacts.example/guest", "0" * 64)

    assert requests == 1


async def test_fetch_pinned_artifact_translates_http_error() -> None:
    """HTTP errors should not expose response content."""
    from yinshi.services.managed_artifacts import (
        ManagedArtifactHTTPError,
        fetch_pinned_artifact,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"remote body secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ManagedArtifactHTTPError) as raised:
            await fetch_pinned_artifact(client, "https://artifacts.example/guest", "0" * 64)

    assert str(raised.value) == "Artifact HTTP failure"
    assert raised.value.__cause__ is None


class ChunkStream(httpx.AsyncByteStream):
    """Yield configured chunks and record consumption."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.chunks_read = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.chunks_read += 1
            yield chunk


async def test_fetch_pinned_artifact_rejects_truncation() -> None:
    """Received bytes must match a declared Content-Length."""
    import yinshi.services.managed_artifacts as managed_artifacts

    response = httpx.Response(200, headers={"Content-Length": "6"}, stream=ChunkStream([b"abc"]))

    async def handler(request: httpx.Request) -> httpx.Response:
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(managed_artifacts.ManagedArtifactSizeError):
            await managed_artifacts.fetch_pinned_artifact(
                client,
                "https://artifacts.example/guest",
                hashlib.sha256(b"abc").hexdigest(),
            )


async def test_fetch_pinned_artifact_stops_oversized_stream() -> None:
    """Streaming should stop when content exceeds 10 MiB."""
    import yinshi.services.managed_artifacts as managed_artifacts

    stream = ChunkStream([b"x" * (1024 * 1024)] * 11)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(managed_artifacts.ManagedArtifactSizeError):
            await managed_artifacts.fetch_pinned_artifact(
                client, "https://artifacts.example/guest", "0" * 64
            )

    assert stream.chunks_read == 11


async def test_fetch_pinned_artifact_translates_transport_failure() -> None:
    """Request failures should expose fixed local exceptions."""
    import yinshi.services.managed_artifacts as managed_artifacts

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("remote secret", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(managed_artifacts.ManagedArtifactTransportError) as raised:
            await managed_artifacts.fetch_pinned_artifact(
                client, "https://artifacts.example/guest", "0" * 64
            )

    assert str(raised.value) == "Artifact transport failure"
    assert raised.value.__cause__ is None


async def test_fetch_pinned_artifact_translates_timeout() -> None:
    """Timeout failures should expose a fixed local exception."""
    import yinshi.services.managed_artifacts as managed_artifacts

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("remote secret", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(managed_artifacts.ManagedArtifactTimeoutError) as raised:
            await managed_artifacts.fetch_pinned_artifact(
                client, "https://artifacts.example/guest", "0" * 64
            )

    assert str(raised.value) == "Artifact retrieval timed out"
    assert raised.value.__cause__ is None


async def test_fetch_pinned_artifact_rejects_hash_mismatch() -> None:
    """Digest mismatch should expose a fixed local error."""
    import yinshi.services.managed_artifacts as managed_artifacts

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"artifact")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(managed_artifacts.ManagedArtifactDigestError) as raised:
            await managed_artifacts.fetch_pinned_artifact(
                client, "https://artifacts.example/guest", "0" * 64
            )

    assert str(raised.value) == "Artifact digest mismatch"


async def test_fetch_pinned_artifact_rejects_empty_artifact() -> None:
    """Artifacts must contain at least one byte."""
    import yinshi.services.managed_artifacts as managed_artifacts

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkStream([]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(managed_artifacts.ManagedArtifactSizeError):
            await managed_artifacts.fetch_pinned_artifact(
                client, "https://artifacts.example/guest", hashlib.sha256(b"").hexdigest()
            )


async def test_fetch_pinned_artifact_rejects_declared_oversize() -> None:
    """Excessive Content-Length should become a fixed size failure."""
    import yinshi.services.managed_artifacts as managed_artifacts

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": str(10 * 1024 * 1024 + 1)},
            stream=ChunkStream([b"x"]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(managed_artifacts.ManagedArtifactSizeError):
            await managed_artifacts.fetch_pinned_artifact(
                client, "https://artifacts.example/guest", "0" * 64
            )
