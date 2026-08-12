"""Response-boundary tests for the Fly Sprites HTTP client."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from yinshi.services.sprites import SpritesClient, SpritesProtocolError


class OversizedResponseStream(httpx.AsyncByteStream):
    """Fail if the client reads after accumulated bytes exceed its limit."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b" " * (600 * 1024)
        yield b" " * (600 * 1024)
        raise AssertionError("client read beyond response limit")


async def _invoke_normal_operation(client: SpritesClient, operation: str) -> None:
    """Call one non-NDJSON provider operation."""
    if operation == "get":
        await client.get_sprite("yinshi-test-user")
    elif operation == "policy":
        await client.set_network_policy(
            "yinshi-test-user",
            allowed_domains=("control.example.com",),
        )
    elif operation == "write":
        await client.write_file(
            "yinshi-test-user",
            path="/opt/yinshi/runner.py",
            content=b"runner",
            mode="0750",
            mkdir=True,
        )
    elif operation == "wake":
        await client.wake_sprite("yinshi-test-user")
    elif operation == "delete":
        await client.delete_sprite("yinshi-test-user")
    elif operation == "get-service":
        await client.get_service("yinshi-test-user", service_name="web")
    else:
        raise AssertionError(f"Unsupported test operation: {operation}")


@pytest.mark.asyncio
async def test_get_sprite_stops_reading_at_response_size_limit() -> None:
    """Sprite lookup should stop before buffering an oversized response."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=OversizedResponseStream())

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="exceeds size limit"):
            await client.get_sprite("yinshi-test-user")


@pytest.mark.asyncio
async def test_network_policy_stops_reading_at_response_size_limit() -> None:
    """Policy updates should stop before buffering an oversized response."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=OversizedResponseStream())

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="exceeds size limit"):
            await client.set_network_policy(
                "yinshi-test-user",
                allowed_domains=("control.example.com",),
            )


@pytest.mark.asyncio
async def test_write_file_stops_reading_at_response_size_limit() -> None:
    """File writes should stop before buffering an oversized response."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=OversizedResponseStream())

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="exceeds size limit"):
            await _invoke_normal_operation(client, "write")


@pytest.mark.asyncio
async def test_wake_sprite_stops_reading_at_response_size_limit() -> None:
    """Sprite wake should stop before buffering an oversized response."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=OversizedResponseStream())

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="exceeds size limit"):
            await _invoke_normal_operation(client, "wake")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ("delete", "get-service"),
)
async def test_normal_operations_stop_reading_at_response_size_limit(operation: str) -> None:
    """Each normal operation should stop before buffering an oversized response."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=OversizedResponseStream())

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="exceeds size limit"):
            await _invoke_normal_operation(client, operation)


@pytest.mark.asyncio
async def test_sprite_response_rejects_unreasonably_long_id() -> None:
    """Returned Sprite records should contain practical identifier lengths."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "i" * 257,
                "name": "yinshi-test-user",
                "status": "cold",
            },
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
async def test_sprite_response_rejects_unreasonably_long_status() -> None:
    """Returned Sprite status should have a practical length."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "provider-id", "name": "yinshi-test-user", "status": "s" * 65},
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
async def test_service_response_rejects_unreasonable_returned_values() -> None:
    """Returned services should contain bounded fields and list counts."""
    replacements: tuple[dict[str, object], ...] = (
        {"cmd": "c" * 4097},
        {"args": ["argument"] * 257},
        {"args": ["a" * 4097]},
        {"needs": ["dependency"] * 257},
        {"needs": ["n" * 4097]},
        {"http_port": 65536},
        {"state": {"name": "web", "status": "running", "started_at": "t" * 129}},
        {"state": {"name": "web", "status": "failed", "error": "e" * 4097}},
    )

    for replacement in replacements:
        payload: dict[str, object] = {
            "name": "web",
            "cmd": "python",
            "args": [],
            "needs": [],
            "http_port": 8080,
            "state": None,
        }
        payload.update(replacement)

        def handle_request(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        transport = httpx.MockTransport(handle_request)
        async with httpx.AsyncClient(
            base_url="https://api.sprites.dev",
            transport=transport,
        ) as http_client:
            client = SpritesClient(api_token="provider-token", http_client=http_client)
            with pytest.raises(SpritesProtocolError, match="service"):
                await client.get_service("yinshi-test-user", service_name="web")


@pytest.mark.asyncio
async def test_stop_service_requires_stopped_and_complete_events() -> None:
    """Service stop should consume a bounded completed stop stream."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=(
                b'{"type":"stopping","timestamp":1}\n'
                b'{"type":"stopped","exit_code":143,"timestamp":2}\n'
                b'{"type":"complete","timestamp":3}\n'
            ),
            headers={"content-type": "application/x-ndjson"},
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.stop_service(
            "yinshi-test-user",
            service_name="yinshi-runner",
            timeout_seconds=30,
        )

    assert requests[0].url.path.endswith("/services/yinshi-runner/stop")
    assert requests[0].url.params["timeout"] == "30s"


@pytest.mark.asyncio
async def test_start_service_requires_started_and_complete_events() -> None:
    """Service start should consume a bounded completed start stream."""
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=(b'{"type":"started","timestamp":1}\n' b'{"type":"complete","timestamp":2}\n'),
            headers={"content-type": "application/x-ndjson"},
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        await client.start_service(
            "yinshi-test-user",
            service_name="yinshi-sidecar",
            monitor_duration=5,
        )

    assert requests[0].url.path.endswith("/services/yinshi-sidecar/start")
    assert requests[0].url.params["duration"] == "5s"
