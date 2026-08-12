"""Tests for bounded managed maintenance metadata and cleanup."""

from __future__ import annotations

import httpx
import pytest

from yinshi.services.sprites import SpritesClient


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
