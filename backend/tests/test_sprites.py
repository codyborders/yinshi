"""Tests for the Fly Sprites HTTP client.

These tests exercise provider requests through an HTTP mock transport and verify
that provider responses become validated Yinshi records.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import cast

import httpx
import pytest

import yinshi.services.sprites as sprites_module
from yinshi.services.sprites import (
    SpritesClient,
    SpritesProtocolError,
    SpritesProviderError,
)


class OversizedResponseStream(httpx.AsyncByteStream):
    """Fail if the client reads beyond its configured response limit."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b" " * (600 * 1024)
        yield b" " * (600 * 1024)
        raise AssertionError("client read beyond response limit")


class SlowResponseStream(httpx.AsyncByteStream):
    """Send regular chunks for longer than the operation deadline."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(3):
            await asyncio.sleep(0.03)
            yield b'{"type":"info"}\n'


@pytest.mark.asyncio
async def test_create_sprite_stops_reading_at_response_size_limit() -> None:
    """Sprite creation should stop before buffering an oversized response."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, stream=OversizedResponseStream())

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="exceeds size limit"):
            await client.create_sprite("yinshi-test-user")


@pytest.mark.asyncio
async def test_client_rejects_blank_api_token() -> None:
    """Provider authentication must fail before any HTTP request can be sent."""
    async with httpx.AsyncClient(base_url="https://api.sprites.dev") as http_client:
        with pytest.raises(ValueError, match="api_token"):
            SpritesClient(api_token="   ", http_client=http_client)


@pytest.mark.asyncio
async def test_create_sprite_uses_private_url_and_returns_record() -> None:
    """Creating a Sprite should request private URL access and validate its identity."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "id": "01234567-89ab-cdef-0123-456789abcdef",
                "name": "yinshi-test-user",
                "organization": "yinshi-test",
                "url": "https://yinshi-test-user.example.sprites.app",
                "url_settings": {"auth": "sprite"},
                "status": "cold",
                "created_at": "2026-08-11T10:00:00Z",
                "updated_at": "2026-08-11T10:00:00Z",
            },
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        sprite = await client.create_sprite("yinshi-test-user")

    assert sprite.id == "01234567-89ab-cdef-0123-456789abcdef"
    assert sprite.name == "yinshi-test-user"
    assert sprite.status == "cold"
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url == "https://api.sprites.dev/v1/sprites"
    assert requests[0].headers["Authorization"] == "Bearer provider-token"
    assert requests[0].extensions["timeout"]["read"] == 120.0
    assert json.loads(requests[0].content) == {
        "name": "yinshi-test-user",
        "url_settings": {"auth": "sprite"},
        "wait_for_capacity": True,
    }


@pytest.mark.asyncio
async def test_create_sprite_rejects_mismatched_provider_identity() -> None:
    """A provider response for another Sprite must not be accepted."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"id": "provider-id", "name": "other-sprite", "status": "cold"},
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="name"):
            await client.create_sprite("yinshi-test-user")


@pytest.mark.asyncio
async def test_create_sprite_rejects_malformed_provider_record() -> None:
    """Malformed provider records should become a stable protocol error."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"id": 7, "name": "yinshi-test-user", "status": "cold"},
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="record"):
            await client.create_sprite("yinshi-test-user")


@pytest.mark.asyncio
async def test_create_sprite_rejects_invalid_json() -> None:
    """Invalid provider JSON should become a stable protocol error."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=b"not-json")

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="JSON"):
            await client.create_sprite("yinshi-test-user")


@pytest.mark.asyncio
async def test_list_sprites_follows_pagination_without_detail_requests() -> None:
    """Inventory should paginate without hydrating every listed Sprite."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/sprites":
            continuation = request.url.params.get("continuation_token")
            if continuation is None:
                return httpx.Response(
                    200,
                    json={
                        "sprites": [{"name": "yinshi-first", "org_slug": "org"}],
                        "has_more": True,
                        "next_continuation_token": "next-page",
                    },
                )
            assert continuation == "next-page"
            return httpx.Response(
                200,
                json={
                    "sprites": [{"name": "yinshi-second", "org_slug": "org"}],
                    "has_more": False,
                },
            )
        raise AssertionError("detail request was not expected")

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        records = await client.list_sprites(prefix="yinshi-")

    assert [record.name for record in records] == ["yinshi-first", "yinshi-second"]
    assert len(requests) == 2
    list_requests = [request for request in requests if request.url.path == "/v1/sprites"]
    assert dict(list_requests[0].url.params) == {"prefix": "yinshi-", "max_results": "50"}
    assert dict(list_requests[1].url.params) == {
        "prefix": "yinshi-",
        "max_results": "50",
        "continuation_token": "next-page",
    }


@pytest.mark.asyncio
async def test_list_sprites_accepts_empty_filtered_terminal_page() -> None:
    """Provider empty prefix results may omit a continuation token despite has_more."""
    from yinshi.services.sprites import SpritesClient

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "has_more": True,
                "next_continuation_token": None,
                "sprites": [],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.sprites.dev"
    ) as http_client:
        client = SpritesClient(api_token="secret", http_client=http_client)

        records = await client.list_sprites(prefix="yinshi-staging")

    assert records == ()


@pytest.mark.asyncio
async def test_list_sprites_rejects_repeated_continuation_token() -> None:
    """Incomplete cyclic pagination must never become an inventory snapshot."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sprites":
            return httpx.Response(
                200,
                json={
                    "sprites": [],
                    "has_more": True,
                    "next_continuation_token": "same-token",
                },
            )
        raise AssertionError("record fetch was not expected")

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="continuation"):
            await client.list_sprites(prefix="yinshi-")


@pytest.mark.asyncio
async def test_get_sprite_returns_validated_provider_record() -> None:
    """Successful lookup should return the requested Sprite record."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "provider-id",
                "name": "yinshi-test-user",
                "status": "running",
            },
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        sprite = await client.get_sprite("yinshi-test-user")

    assert sprite is not None
    assert sprite.id == "provider-id"
    assert sprite.name == "yinshi-test-user"
    assert sprite.status == "running"
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/sprites/yinshi-test-user"
    assert requests[0].headers["Authorization"] == "Bearer provider-token"
    assert requests[0].extensions["timeout"]["read"] == 30.0


@pytest.mark.asyncio
async def test_get_sprite_returns_none_when_provider_has_no_resource() -> None:
    """Missing provider resources should support idempotent provisioning."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        sprite = await client.get_sprite("yinshi-test-user")

    assert sprite is None


@pytest.mark.asyncio
async def test_get_sprite_rejects_malformed_provider_record() -> None:
    """Lookup should reject malformed records using the shared protocol error."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "provider-id", "name": "yinshi-test-user"},
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="record"):
            await client.get_sprite("yinshi-test-user")


@pytest.mark.asyncio
async def test_set_network_policy_accepts_supported_leading_wildcard() -> None:
    """A leading subdomain wildcard should pass strict domain validation."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "rules": [
                    {"action": "allow", "domain": "*.example.com"},
                    {"action": "deny", "domain": "*"},
                ]
            },
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.set_network_policy(
            "yinshi-test-user",
            allowed_domains=("*.example.com",),
        )

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_set_network_policy_rejects_global_wildcard_without_request() -> None:
    """Caller allow rules must not contain the provider global wildcard."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "rules": [
                    {"action": "allow", "domain": "*"},
                    {"action": "deny", "domain": "*"},
                ]
            },
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="Allowed domain"):
            await client.set_network_policy(
                "yinshi-test-user",
                allowed_domains=("*",),
            )

    assert requests == []


@pytest.mark.asyncio
async def test_set_network_policy_rejects_malformed_domain_without_request() -> None:
    """Allow rules must contain valid DNS labels."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "rules": [
                    {"action": "allow", "domain": "EXAMPLE.com"},
                    {"action": "deny", "domain": "*"},
                ]
            },
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="Allowed domain"):
            await client.set_network_policy(
                "yinshi-test-user",
                allowed_domains=("EXAMPLE.com",),
            )

    assert requests == []


@pytest.mark.asyncio
async def test_set_network_policy_rejects_empty_domain_without_request() -> None:
    """Allow rules must not contain an empty domain."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="Allowed domain"):
            await client.set_network_policy(
                "yinshi-test-user",
                allowed_domains=("",),
            )

    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_domains",
    (
        [],
        "example.com",
        tuple(f"host-{index}.example.com" for index in range(257)),
        ("example.com", "example.com"),
        (7,),
        ([],),
        ("example",),
        ("EXAMPLE.com",),
        ("example .com",),
        ("example\n.com",),
        ("127.0.0.1",),
        ("*",),
        ("*.*.example.com",),
        ("api.*.example.com",),
        (".example.com",),
        ("-api.example.com",),
        ("api-.example.com",),
        (f"{'a' * 64}.example.com",),
        (f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 62}.com",),
        ("example.123",),
        ("api_example.com",),
    ),
)
async def test_set_network_policy_rejects_invalid_domain_sets_without_request(
    invalid_domains: object,
) -> None:
    """Only bounded unique tuples of public DNS names may reach the provider."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"rules": []})

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="Allowed domains"):
            await client.set_network_policy(
                "yinshi-test-user",
                allowed_domains=cast(tuple[str, ...], invalid_domains),
            )

    assert requests == []


@pytest.mark.asyncio
async def test_set_network_policy_denies_unlisted_destinations() -> None:
    """Configured domains should be followed by an explicit global deny rule."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "rules": [
                    {"action": "allow", "domain": "control.example.com"},
                    {"action": "allow", "domain": "github.com"},
                    {"action": "deny", "domain": "*"},
                ]
            },
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.set_network_policy(
            "yinshi-test-user",
            allowed_domains=("control.example.com", "github.com"),
        )

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/sprites/yinshi-test-user/policy/network"
    assert requests[0].extensions["timeout"]["read"] == 30.0
    assert json.loads(requests[0].content) == {
        "rules": [
            {"action": "allow", "domain": "control.example.com"},
            {"action": "allow", "domain": "github.com"},
            {"action": "deny", "domain": "*"},
        ]
    }


@pytest.mark.asyncio
async def test_write_file_sends_raw_bytes_and_filesystem_query_values() -> None:
    """File writes should use raw bytes and the documented filesystem query."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"path": "/opt/yinshi/runner.py", "size": 4, "mode": "0750"},
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.write_file(
            "yinshi-test-user",
            path="/opt/yinshi/runner.py",
            content=b"\x00\xffpi",
            mode="0750",
            mkdir=True,
        )

    assert len(requests) == 1
    assert requests[0].method == "PUT"
    assert requests[0].url.path == "/v1/sprites/yinshi-test-user/fs/write"
    assert dict(requests[0].url.params) == {
        "path": "/opt/yinshi/runner.py",
        "workingDir": "/",
        "mode": "0750",
        "mkdir": "true",
    }
    assert requests[0].headers["Content-Type"] == "application/octet-stream"
    assert requests[0].content == b"\x00\xffpi"
    assert requests[0].extensions["timeout"]["read"] == 30.0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_mkdir", (0, 1, None, "true"))
async def test_write_file_rejects_non_boolean_mkdir_without_request(
    invalid_mkdir: object,
) -> None:
    """Filesystem creation flag must be an exact Boolean before provider I/O."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="mkdir"):
            await client.write_file(
                "yinshi-test-user",
                path="/opt/yinshi/runner.py",
                content=b"runner",
                mode="0750",
                mkdir=cast(bool, invalid_mkdir),
            )

    assert requests == []


@pytest.mark.asyncio
async def test_write_file_rejects_unsafe_path_without_request() -> None:
    """File paths must be bounded absolute paths without traversal."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="File path"):
            await client.write_file(
                "yinshi-test-user",
                path="/opt/../secret",
                content=b"runner",
                mode="0750",
                mkdir=True,
            )

    assert requests == []


@pytest.mark.asyncio
async def test_write_file_rejects_oversized_content_without_request() -> None:
    """File content must remain within the upload bound."""
    async with httpx.AsyncClient(base_url="https://api.sprites.dev") as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="File content"):
            await client.write_file(
                "yinshi-test-user",
                path="/opt/runner.py",
                content=b"x" * (10 * 1024 * 1024 + 1),
                mode="0750",
                mkdir=True,
            )


@pytest.mark.asyncio
async def test_write_file_rejects_invalid_mode_without_request() -> None:
    """File mode must be a four-digit octal permission string."""
    async with httpx.AsyncClient(base_url="https://api.sprites.dev") as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="File mode"):
            await client.write_file(
                "yinshi-test-user",
                path="/opt/runner.py",
                content=b"runner",
                mode="0999",
                mkdir=True,
            )


@pytest.mark.asyncio
async def test_configure_service_sends_full_definition_and_monitor_duration() -> None:
    """Generic service configuration should preserve each provider field."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b'{"type":"complete","timestamp":1}\n')

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.configure_service(
            "yinshi-test-user",
            service_name="web",
            command="python",
            args=("-m", "http.server", "8080"),
            environment={"MODE": "managed"},
            directory="/srv/app",
            needs=("database",),
            http_port=8080,
            monitor_duration=12.5,
        )

    assert len(requests) == 1
    assert requests[0].method == "PUT"
    assert requests[0].url.path == "/v1/sprites/yinshi-test-user/services/web"
    assert dict(requests[0].url.params) == {"duration": "12.5s"}
    assert requests[0].extensions["timeout"]["read"] == 120.0
    assert json.loads(requests[0].content) == {
        "cmd": "python",
        "args": ["-m", "http.server", "8080"],
        "env": {"MODE": "managed"},
        "dir": "/srv/app",
        "needs": ["database"],
        "http_port": 8080,
    }


@pytest.mark.asyncio
async def test_service_monitor_duration_extends_provider_stream_deadline() -> None:
    """Maximum valid monitoring should extend service operation deadlines."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b'{"type":"complete"}\n')

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.configure_service(
            "yinshi-test-user",
            service_name="web",
            command="python",
            args=(),
            environment={},
            directory="/srv/app",
            needs=(),
            monitor_duration=86400.0,
        )
        await client.restart_service(
            "yinshi-test-user",
            service_name="web",
            monitor_duration=86400.0,
        )

    assert len(requests) == 2
    for request in requests:
        assert dict(request.url.params) == {"duration": "86400s"}
        assert request.extensions["timeout"]["read"] == 86430.0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("configure", "restart"))
@pytest.mark.parametrize(
    "monitor_duration",
    (
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
        0.0,
        -1.0,
        86400.1,
        10**1000,
        "10",
    ),
)
async def test_service_monitor_duration_rejects_invalid_values_without_request(
    operation: str,
    monitor_duration: object,
) -> None:
    """Invalid monitoring durations must fail before provider I/O."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b'{"type":"complete"}\n')

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="Monitor duration"):
            if operation == "configure":
                await client.configure_service(
                    "yinshi-test-user",
                    service_name="web",
                    command="python",
                    args=(),
                    environment={},
                    directory="/srv/app",
                    needs=(),
                    monitor_duration=cast(float, monitor_duration),
                )
            else:
                await client.restart_service(
                    "yinshi-test-user",
                    service_name="web",
                    monitor_duration=cast(float, monitor_duration),
                )

    assert requests == []


@pytest.mark.asyncio
async def test_configure_service_allows_private_service_without_optional_port() -> None:
    """Generic service configuration should allow omitted optional values."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        assert "http_port" not in json.loads(request.content)
        assert "duration" not in request.url.params
        return httpx.Response(200, content=b'{"type":"complete"}\n')

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.configure_service(
            "yinshi-test-user",
            service_name="worker",
            command="python",
            args=("-m", "worker"),
            environment={},
            directory="/srv/app",
            needs=(),
        )


@pytest.mark.asyncio
async def test_configure_service_rejects_nonzero_exit() -> None:
    """A service process failure must fail configuration."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"type":"exit","exit_code":1}\n{"type":"complete"}\n',
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="service failed"):
            await client.configure_service(
                "yinshi-test-user",
                service_name="bootstrap",
                command="/bin/false",
                args=(),
                environment={},
                directory="/",
                needs=(),
            )


@pytest.mark.asyncio
async def test_configure_service_rejects_blank_command_without_request() -> None:
    """Service command must contain bounded executable text."""
    async with httpx.AsyncClient(base_url="https://api.sprites.dev") as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="Command"):
            await client.configure_service(
                "yinshi-test-user",
                service_name="web",
                command="",
                args=(),
                environment={},
                directory="/srv/app",
                needs=(),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("command", ""),
        ("command", "   "),
        ("command", "python\x00-m"),
        ("command", "x" * 4097),
        ("command", 7),
        ("args", []),
        ("args", ("x",) * 257),
        ("args", ("x" * 4097,)),
        ("args", ("bad\x00arg",)),
        ("args", (7,)),
        ("environment", []),
        ("environment", {f"KEY_{index}": "x" for index in range(257)}),
        ("environment", {"1MODE": "managed"}),
        ("environment", {"BAD-NAME": "managed"}),
        ("environment", {"MODE\x00": "managed"}),
        ("environment", {"MODE": "managed\x00"}),
        ("environment", {"MODE": 7}),
        ("environment", {7: "managed"}),
        ("environment", {"A" * 4097: "managed"}),
        ("environment", {"MODE": "x" * 4097}),
        ("directory", "srv/app"),
        ("directory", "/srv/../app"),
        ("directory", "/srv/app\x00"),
        ("directory", "/" + "x" * 4096),
        ("directory", 7),
        ("needs", []),
        ("needs", ("database",) * 257),
        ("needs", ("database", "database")),
        ("needs", ("invalid/name",)),
        ("needs", (7,)),
        ("http_port", True),
        ("http_port", 1.0),
        ("http_port", 0),
        ("http_port", 65536),
    ),
)
async def test_configure_service_rejects_invalid_fields_without_request(
    field: str,
    invalid_value: object,
) -> None:
    """Every service definition field must be validated before provider I/O."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b'{"type":"complete"}\n')

    command = cast(str, invalid_value) if field == "command" else "python"
    args = cast(tuple[str, ...], invalid_value) if field == "args" else ("-m", "worker")
    environment = (
        cast(Mapping[str, str], invalid_value) if field == "environment" else {"MODE": "managed"}
    )
    directory = cast(str, invalid_value) if field == "directory" else "/srv/app"
    needs = cast(tuple[str, ...], invalid_value) if field == "needs" else ("database",)
    http_port = cast(int | None, invalid_value) if field == "http_port" else 8080

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError):
            await client.configure_service(
                "yinshi-test-user",
                service_name="web",
                command=command,
                args=args,
                environment=environment,
                directory=directory,
                needs=needs,
                http_port=http_port,
            )

    assert requests == []


@pytest.mark.asyncio
async def test_get_service_returns_typed_definition_and_state() -> None:
    """Service lookup should decode provider details into typed records."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "web",
                "cmd": "python",
                "args": ["-m", "http.server", "8080"],
                "needs": ["database"],
                "http_port": 8080,
                "state": {
                    "name": "web",
                    "status": "running",
                    "pid": 31,
                    "started_at": "2026-08-11T10:00:00Z",
                },
            },
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        service = await client.get_service("yinshi-test-user", service_name="web")

    assert service is not None
    assert type(service).__name__ == "ServiceRecord"
    assert service.name == "web"
    assert service.command == "python"
    assert service.args == ("-m", "http.server", "8080")
    assert service.needs == ("database",)
    assert service.http_port == 8080
    assert service.state is not None
    assert type(service.state).__name__ == "ServiceState"
    assert service.state.name == "web"
    assert service.state.status == "running"
    assert service.state.pid == 31
    assert service.state.started_at == "2026-08-11T10:00:00Z"
    assert service.state.error is None


@pytest.mark.asyncio
async def test_get_service_rejects_unsafe_service_name_before_request() -> None:
    """Service names should be validated before endpoint construction."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="Service name"):
            await client.get_service(
                "yinshi-test-user",
                service_name="../yinshi-runner/restart",
            )

    assert requests == []


@pytest.mark.asyncio
async def test_restart_service_uses_duration_and_waits_for_completion() -> None:
    """Service restart should consume provider progress through completion."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=(
                b'{"type":"stopping","timestamp":1}\n'
                b'{"type":"started","timestamp":2}\n'
                b'{"type":"complete","timestamp":3}\n'
            ),
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.restart_service(
            "yinshi-test-user",
            service_name="web",
            monitor_duration=8.0,
        )

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/sprites/yinshi-test-user/services/web/restart"
    assert dict(requests[0].url.params) == {"duration": "8s"}
    assert requests[0].extensions["timeout"]["read"] == 120.0


@pytest.mark.asyncio
async def test_restart_service_stops_reading_at_stream_size_limit() -> None:
    """Service restart should stop before buffering an oversized stream."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=OversizedResponseStream())

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="exceeds size limit"):
            await client.restart_service(
                "yinshi-test-user",
                service_name="web",
                monitor_duration=None,
            )


@pytest.mark.asyncio
async def test_configure_private_runner_uses_service_without_http_port() -> None:
    """The managed runner should stay private while running as a Sprite service."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=b'{"type":"complete","timestamp":1767609000000}\n',
            headers={"Content-Type": "application/x-ndjson"},
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.configure_private_runner(
            "yinshi-test-user",
            command="/bin/bash",
            args=("-lc", "exec python -m yinshi"),
            environment={"YINSHI_MODE": "worker", "RUNNER_TOKEN": "runner-token"},
            working_directory="/opt/yinshi",
        )

    assert len(requests) == 1
    assert requests[0].method == "PUT"
    assert requests[0].url.path == ("/v1/sprites/yinshi-test-user/services/yinshi-runner")
    assert requests[0].extensions["timeout"]["read"] == 120.0
    assert json.loads(requests[0].content) == {
        "cmd": "/bin/bash",
        "args": ["-lc", "exec python -m yinshi"],
        "env": {"YINSHI_MODE": "worker", "RUNNER_TOKEN": "runner-token"},
        "dir": "/opt/yinshi",
        "needs": [],
    }


@pytest.mark.asyncio
async def test_wake_sprite_uses_one_shot_http_exec() -> None:
    """A one-shot HTTP Exec request should wake a cold Sprite without WebSockets."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.wake_sprite("yinshi-test-user")

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/sprites/yinshi-test-user/exec"
    assert requests[0].url.params.get_list("cmd") == ["true"]
    assert requests[0].headers["Authorization"] == "Bearer provider-token"
    assert requests[0].extensions["timeout"]["read"] == 30.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    (
        "",
        "../connectors/value",
        "Uppercase",
        "-leading",
        "trailing-",
        "two.parts",
        "white space",
        "a" * 64,
    ),
)
async def test_delete_sprite_rejects_unsafe_name_without_request(name: str) -> None:
    """Sprite names must be provider-safe labels before endpoint construction."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="Sprite name"):
            await client.delete_sprite(name)

    assert requests == []


@pytest.mark.asyncio
async def test_delete_sprite_treats_missing_resource_as_deleted() -> None:
    """Repeated deletion should succeed after the provider removes the Sprite."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.delete_sprite("yinshi-test-user")


@pytest.mark.asyncio
async def test_delete_sprite_uses_provider_delete_endpoint() -> None:
    """Deleting managed capacity should call the permanent Sprite endpoint."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.delete_sprite("yinshi-test-user")

    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    assert requests[0].url.path == "/v1/sprites/yinshi-test-user"
    assert requests[0].extensions["timeout"]["read"] == 30.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "message"),
    (
        ("configure", "configure service"),
        ("restart", "restart service"),
        ("restore", "restore checkpoint"),
        ("checkpoint", "create checkpoint"),
    ),
)
async def test_stream_operations_enforce_total_elapsed_deadline(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    message: str,
) -> None:
    """Regular response chunks must not extend a streamed operation forever."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=SlowResponseStream())

    monkeypatch.setattr(sprites_module, "_LONG_OPERATION_TIMEOUT_SECONDS", 0.05)
    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProviderError, match=message) as error:
            if operation == "configure":
                await client.configure_service(
                    "yinshi-test-user",
                    service_name="web",
                    command="python",
                    args=(),
                    environment={},
                    directory="/srv/app",
                    needs=(),
                )
            elif operation == "restart":
                await client.restart_service(
                    "yinshi-test-user",
                    service_name="web",
                    monitor_duration=None,
                )
            elif operation == "restore":
                await client.restore_checkpoint("yinshi-test-user", checkpoint_id="v7")
            else:
                await client.create_checkpoint("yinshi-test-user", comment="configured")

    assert "provider-token" not in str(error.value)


@pytest.mark.asyncio
async def test_restore_checkpoint_waits_for_provider_completion() -> None:
    """Checkpoint restore should consume provider progress through completion."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/sprites/yinshi-test-user/checkpoints/v7/restore"
        return httpx.Response(200, content=b'{"type":"complete"}\n')

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.restore_checkpoint("yinshi-test-user", checkpoint_id="v7")


@pytest.mark.asyncio
async def test_restore_checkpoint_stops_reading_at_stream_size_limit() -> None:
    """Checkpoint restore should stop before buffering an oversized stream."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=OversizedResponseStream())

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="exceeds size limit"):
            await client.restore_checkpoint("yinshi-test-user", checkpoint_id="v7")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_comment",
    ("", "   ", "configured\x00secret", "line one\nline two", "x" * 4097, 7),
)
async def test_create_checkpoint_rejects_invalid_comment_without_request(
    invalid_comment: object,
) -> None:
    """Checkpoint comments must be bounded safe nonblank text before provider I/O."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b'{"type":"complete"}\n')

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="Checkpoint comment"):
            await client.create_checkpoint(
                "yinshi-test-user",
                comment=cast(str, invalid_comment),
            )

    assert requests == []


@pytest.mark.asyncio
async def test_create_checkpoint_waits_for_provider_completion() -> None:
    """Checkpoint creation should use the streaming singular checkpoint endpoint."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=(
                b'{"type":"info","data":"Creating checkpoint...",'
                b'"time":"2026-08-11T10:00:00Z"}\n'
                b'{"type":"complete","data":"Checkpoint v1 created",'
                b'"time":"2026-08-11T10:00:01Z"}\n'
            ),
            headers={"Content-Type": "application/x-ndjson"},
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.create_checkpoint(
            "yinshi-test-user",
            comment="Runner configured",
        )

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/sprites/yinshi-test-user/checkpoint"
    assert requests[0].extensions["timeout"]["read"] == 120.0
    assert json.loads(requests[0].content) == {"comment": "Runner configured"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "message"),
    (
        ("get", "get Sprite"),
        ("policy", "set network policy"),
        ("runner", "configure runner service"),
        ("wake", "wake Sprite"),
        ("delete", "delete Sprite"),
        ("checkpoint", "create checkpoint"),
        ("write", "write Sprite file"),
        ("service-configure", "configure service"),
        ("service-get", "get service"),
        ("restart", "restart service"),
        ("restore", "restore checkpoint"),
    ),
)
async def test_transport_failures_never_expose_authenticated_request(
    operation: str,
    message: str,
) -> None:
    """Every operation should replace request-bearing transport errors."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider-token transport detail", request=request)

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProviderError, match=message) as error:
            if operation == "get":
                await client.get_sprite("yinshi-test-user")
            elif operation == "policy":
                await client.set_network_policy(
                    "yinshi-test-user",
                    allowed_domains=("control.example.com",),
                )
            elif operation == "runner":
                await client.configure_private_runner(
                    "yinshi-test-user",
                    command="python",
                    args=("-m", "yinshi"),
                    environment={"RUNNER_TOKEN": "runner-token"},
                    working_directory="/opt/yinshi",
                )
            elif operation == "write":
                await client.write_file(
                    "yinshi-test-user",
                    path="/opt/yinshi/runner.py",
                    content=b"runner",
                    mode="0750",
                    mkdir=True,
                )
            elif operation == "service-configure":
                await client.configure_service(
                    "yinshi-test-user",
                    service_name="web",
                    command="python",
                    args=(),
                    environment={},
                    directory="/srv/app",
                    needs=(),
                    http_port=None,
                    monitor_duration=None,
                )
            elif operation == "service-get":
                await client.get_service("yinshi-test-user", service_name="web")
            elif operation == "restart":
                await client.restart_service(
                    "yinshi-test-user",
                    service_name="web",
                    monitor_duration=None,
                )
            elif operation == "restore":
                await client.restore_checkpoint("yinshi-test-user", checkpoint_id="v7")
            elif operation == "wake":
                await client.wake_sprite("yinshi-test-user")
            elif operation == "delete":
                await client.delete_sprite("yinshi-test-user")
            else:
                await client.create_checkpoint(
                    "yinshi-test-user",
                    comment="configured",
                )

    assert "provider-token" not in str(error.value)
    assert "transport detail" not in str(error.value)
    assert not hasattr(error.value, "request")


@pytest.mark.asyncio
async def test_transport_failure_is_translated_without_token() -> None:
    """Network failures should not expose provider credentials."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("provider-token transport detail", request=request)

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProviderError, match="create Sprite") as error:
            await client.create_sprite("yinshi-test-user")

    assert "provider-token" not in str(error.value)
    assert "transport detail" not in str(error.value)


@pytest.mark.asyncio
async def test_provider_failure_is_translated_without_token_or_response_body() -> None:
    """Provider failures should expose stable metadata without credentials or internals."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": "rejected provider-token and internal-provider-detail"},
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProviderError) as error:
            await client.get_sprite("yinshi-test-user")

    message = str(error.value)
    assert "get Sprite" in message
    assert "401" in message
    assert "provider-token" not in message
    assert "internal-provider-detail" not in message
    assert not hasattr(error.value, "request")


@pytest.mark.asyncio
async def test_network_policy_failure_is_translated_without_token() -> None:
    """Network policy updates should translate provider status failures."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "provider-token"})

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(
            SpritesProviderError,
            match="set network policy.*503",
        ) as error:
            await client.set_network_policy(
                "yinshi-test-user",
                allowed_domains=("control.example.com",),
            )

    assert "provider-token" not in str(error.value)


@pytest.mark.asyncio
async def test_runner_service_failure_is_translated_without_secrets() -> None:
    """Runner service failures should not expose provider or runner tokens."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "provider-token runner-token"})

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(
            SpritesProviderError,
            match="configure runner service.*500",
        ) as error:
            await client.configure_private_runner(
                "yinshi-test-user",
                command="python",
                args=("-m", "yinshi"),
                environment={"RUNNER_TOKEN": "runner-token"},
                working_directory="/opt/yinshi",
            )

    assert "provider-token" not in str(error.value)
    assert "runner-token" not in str(error.value)


@pytest.mark.asyncio
async def test_runner_service_rejects_stream_error_without_secrets() -> None:
    """Service stream errors should become safe protocol failures."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b'{"type":"error","data":"runner-token provider-token",'
                b'"timestamp":1767609000000}\n'
            ),
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="runner service") as error:
            await client.configure_private_runner(
                "yinshi-test-user",
                command="python",
                args=("-m", "yinshi"),
                environment={"RUNNER_TOKEN": "runner-token"},
                working_directory="/opt/yinshi",
            )

    assert "provider-token" not in str(error.value)
    assert "runner-token" not in str(error.value)


@pytest.mark.asyncio
async def test_wake_failure_is_translated_without_token() -> None:
    """HTTP Exec wake failures should translate provider status errors."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "provider-token"})

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProviderError, match="wake Sprite.*502") as error:
            await client.wake_sprite("yinshi-test-user")

    assert "provider-token" not in str(error.value)


@pytest.mark.asyncio
async def test_delete_failure_is_translated_without_token() -> None:
    """Sprite deletion should translate provider status errors."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "provider-token"})

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProviderError, match="delete Sprite.*500") as error:
            await client.delete_sprite("yinshi-test-user")

    assert "provider-token" not in str(error.value)


@pytest.mark.asyncio
async def test_checkpoint_failure_is_translated_without_token() -> None:
    """Checkpoint creation should translate provider status errors."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "provider-token"})

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(
            SpritesProviderError,
            match="create checkpoint.*500",
        ) as error:
            await client.create_checkpoint("yinshi-test-user", comment="configured")

    assert "provider-token" not in str(error.value)


@pytest.mark.asyncio
async def test_checkpoint_rejects_stream_error_without_provider_detail() -> None:
    """Checkpoint stream errors should become safe protocol failures."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b'{"type":"error","error":"disk full provider-token",'
                b'"time":"2026-08-11T10:00:00Z"}\n'
            ),
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="checkpoint") as error:
            await client.create_checkpoint("yinshi-test-user", comment="configured")

    assert "provider-token" not in str(error.value)
    assert "disk full" not in str(error.value)


@pytest.mark.asyncio
async def test_checkpoint_rejects_oversized_stream() -> None:
    """Checkpoint parsing should reject a response beyond the byte limit."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b" " * (1024 * 1024 + 1))

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="exceeds size limit"):
            await client.create_checkpoint("yinshi-test-user", comment="configured")


@pytest.mark.asyncio
async def test_runner_service_stops_reading_stream_at_size_limit() -> None:
    """Runner service reading should stop when accumulated bytes exceed the limit."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=OversizedResponseStream())

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="exceeds size limit"):
            await client.configure_private_runner(
                "yinshi-test-user",
                command="python",
                args=(),
                environment={},
                working_directory="/opt/yinshi",
            )


@pytest.mark.asyncio
async def test_checkpoint_stops_reading_stream_at_size_limit() -> None:
    """Checkpoint reading should stop once accumulated bytes exceed the limit."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=OversizedResponseStream())

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="exceeds size limit"):
            await client.create_checkpoint("yinshi-test-user", comment="configured")


@pytest.mark.asyncio
async def test_checkpoint_rejects_too_many_events() -> None:
    """Checkpoint parsing should reject too many NDJSON events."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"type":"complete"}\n' * 1001)

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="exceeds event limit"):
            await client.create_checkpoint("yinshi-test-user", comment="configured")


@pytest.mark.asyncio
async def test_checkpoint_rejects_incomplete_stream() -> None:
    """Checkpoint creation should require a terminal complete event."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b'{"type":"info","data":"Creating checkpoint...",'
                b'"time":"2026-08-11T10:00:00Z"}\n'
            ),
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="did not complete"):
            await client.create_checkpoint("yinshi-test-user", comment="configured")


@pytest.mark.asyncio
async def test_checkpoint_rejects_invalid_ndjson() -> None:
    """Invalid checkpoint progress data should become a stable protocol error."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json\n")

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="NDJSON"):
            await client.create_checkpoint("yinshi-test-user", comment="configured")


@pytest.mark.asyncio
async def test_set_network_policy_accepts_successful_empty_response() -> None:
    """Fly may acknowledge a policy update with a successful empty response."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.set_network_policy(
            "yinshi-test-user",
            allowed_domains=("control.example.com",),
        )


@pytest.mark.asyncio
async def test_set_network_policy_rejects_mismatched_response() -> None:
    """The client should reject a policy response missing requested restrictions."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rules": []})

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="network policy"):
            await client.set_network_policy(
                "yinshi-test-user",
                allowed_domains=("control.example.com",),
            )
