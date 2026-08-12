"""Tests for bounded managed backup transfer through Fly filesystem APIs."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from yinshi.services.sprites import SpritesClient


@pytest.mark.asyncio
async def test_download_file_streams_validated_ranges_to_disk(tmp_path: Path) -> None:
    """Large guest ciphertext should download in exact bounded byte ranges."""
    payload = bytes(range(256)) * 20_000
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        range_header = request.headers.get("Range")
        assert range_header is not None
        start_text, end_text = range_header.removeprefix("bytes=").split("-", maxsplit=1)
        start = int(start_text)
        end = min(int(end_text), len(payload) - 1)
        return httpx.Response(
            206,
            content=payload[start : end + 1],
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{len(payload)}",
            },
        )

    transport = httpx.MockTransport(handle_request)
    target = tmp_path / "archive.enc"
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        result = await client.download_file(
            "yinshi-test-user",
            path="/var/lib/yinshi/maintenance/archive.enc",
            target_path=target,
            expected_size=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )

    assert target.read_bytes() == payload
    assert result.size_bytes == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert len(requests) > 1
    assert all(request.url.params["path"].startswith("/var/lib/yinshi/") for request in requests)


@pytest.mark.asyncio
async def test_upload_file_streams_large_ciphertext_without_buffering(tmp_path: Path) -> None:
    """Restore ciphertext should upload from disk through one streaming request."""
    payload = bytes(range(251)) * 50_000
    source = tmp_path / "archive.enc"
    source.write_bytes(payload)
    received = bytearray()

    async def handle_request(request: httpx.Request) -> httpx.Response:
        async for chunk in request.stream:
            received.extend(chunk)
        return httpx.Response(200, json={"path": request.url.params["path"], "size": len(received)})

    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(
        base_url="https://api.sprites.dev",
        transport=transport,
    ) as http_client:
        client = SpritesClient(api_token="provider-token", http_client=http_client)
        result = await client.upload_file(
            "yinshi-test-user",
            source_path=source,
            path="/var/lib/yinshi/maintenance/archive.enc",
            expected_size=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            mode="0600",
        )

    assert bytes(received) == payload
    assert result.size_bytes == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
