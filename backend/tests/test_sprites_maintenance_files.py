"""Tests for bounded managed maintenance metadata and cleanup."""

from __future__ import annotations

import httpx
import pytest

import yinshi.services.sprites as sprites_module
from yinshi.services.sprites import SpritesClient, SpritesProtocolError


@pytest.mark.asyncio
async def test_read_small_file_and_delete_exact_file() -> None:
    """Maintenance metadata should use bounded reads and nonrecursive cleanup."""
    payload = b'{"job_id":"job","status":"ready"}\n'
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=payload)
        return httpx.Response(200, json={"count": 1, "deleted": request.url.params["path"]})

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        content = await client.read_file(
            "yinshi-test-user",
            path="/var/lib/yinshi/maintenance/job.result",
            max_bytes=4096,
        )
        await client.delete_file(
            "yinshi-test-user",
            path="/var/lib/yinshi/maintenance/job.result",
        )

    assert content == payload
    assert requests[1].method == "DELETE"
    assert requests[1].url.params["recursive"] == "false"


@pytest.mark.asyncio
async def test_read_file_accepts_full_small_file_limit_for_small_body() -> None:
    """File reads may request the full 10 MiB contract without widening the body."""
    payload = b"restored SQLite bytes"

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        content = await client.read_file(
            "yinshi-test-user",
            path="/var/lib/yinshi/sqlite/drill.db",
            max_bytes=10 * 1024 * 1024,
        )

    assert content == payload


@pytest.mark.asyncio
async def test_read_file_rejects_limit_above_small_file_contract() -> None:
    """File reads must reject a requested bound above 10 MiB."""
    async with httpx.AsyncClient(base_url="https://api.sprites.dev") as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(ValueError, match="small-file limit"):
            await client.read_file(
                "yinshi-test-user",
                path="/var/lib/yinshi/sqlite/drill.db",
                max_bytes=(10 * 1024 * 1024) + 1,
            )


@pytest.mark.asyncio
async def test_generic_response_bound_remains_one_mebibyte() -> None:
    """Generic provider responses must retain the default 1 MiB maximum."""
    response = httpx.Response(200, content=b"small")

    with pytest.raises(ValueError, match="provider response limit"):
        await sprites_module._read_bounded_response(
            response,
            "generic response",
            max_bytes=(1024 * 1024) + 1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("use_content_length", [True, False])
async def test_file_read_enforces_requested_bound(use_content_length: bool) -> None:
    """File reads must enforce requested bounds for declared and streamed sizes."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        if use_content_length:
            return httpx.Response(200, content=b"12345")
        return httpx.Response(
            200,
            headers={"Transfer-Encoding": "chunked"},
            stream=httpx.ByteStream(b"12345"),
        )

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        with pytest.raises(SpritesProtocolError, match="exceeds size limit"):
            await client.read_file(
                "yinshi-test-user",
                path="/var/lib/yinshi/sqlite/drill.db",
                max_bytes=4,
            )
