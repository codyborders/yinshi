"""Exercise duplex bounds and privacy through query with real encoded frames."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from yinshi.exceptions import SidecarError
from yinshi.services.orchestration_bridge import generate_orchestration_capability
from yinshi.services.sidecar import SidecarClient


class BridgePeer:
    """In-memory byte transport with deterministic response synchronization."""

    def __init__(self, **options):
        self.client = SidecarClient(**options)
        self.reader = asyncio.StreamReader(limit=8 * 1024 * 1024)
        self.writer = MagicMock()
        self.writer.drain = AsyncMock()
        self.writer.wait_closed = AsyncMock()
        self.client._connected = True
        self.client._reader = self.reader
        self.client._writer = self.writer
        self.capability = generate_orchestration_capability("sess-1", run_id="run-1")

    def feed(self, frame, *, padding=0):
        self.reader.feed_data(json.dumps(frame).encode() + b" " * padding + b"\n")

    def request(self, request_id="req-1", **changes):
        return {
            "type": "orchestration_request",
            "id": "sess-1",
            "request_id": request_id,
            "capability": self.capability.token,
            "operation": "ping_thread_bridge",
            "arguments": {},
            **changes,
        }

    def responses(self):
        return [
            message
            for call in self.writer.write.call_args_list
            for message in [json.loads(call.args[0])]
            if message["type"] == "orchestration_response"
        ]

    async def wait_responses(self, count=1):
        async with asyncio.timeout(1):
            while len(self.responses()) < count:
                await asyncio.sleep(0)
        return self.responses()

    async def collect(self, capability=True):
        return [
            event
            async for event in self.client.query(
                "sess-1",
                "hello",
                orchestration_capability=self.capability if capability else None,
            )
        ]

    def finish(self):
        self.feed({"id": "sess-1", "type": "message", "data": {"type": "result"}})


@pytest.mark.parametrize("duplicate", [False, True])
async def test_overflow_or_pending_duplicate_fails_closed_without_task_fanout(duplicate):
    peer = BridgePeer(orchestration_max_pending=1)
    query = asyncio.create_task(peer.collect())
    for index in range(200):
        peer.feed(peer.request("duplicate" if duplicate else str(index)))
    peer.finish()
    try:
        with pytest.raises(SidecarError):
            await asyncio.wait_for(query, 1)
        assert len(peer.responses()) <= 1
        assert not peer.client.connected
    finally:
        await peer.client.disconnect()


async def test_deferred_handler_keeps_own_frame_size():
    peer = BridgePeer()
    query = asyncio.create_task(peer.collect())
    peer.feed(peer.request("oversized"), padding=64 * 1024)
    peer.feed(peer.request("small"))
    try:
        responses = {item["request_id"]: item for item in await peer.wait_responses(2)}
        assert responses["oversized"]["error"]["code"] == "request_too_large"
        assert responses["small"]["ok"] is True
    finally:
        peer.finish()
        await query
        await peer.client.disconnect()


async def test_malformed_correlation_cannot_expand_error_response():
    peer = BridgePeer()
    query = asyncio.create_task(peer.collect())
    peer.feed(peer.request(request_id="x" * (300 * 1024)))
    try:
        response = (await peer.wait_responses())[0]
        assert response["ok"] is False
        assert len(json.dumps(response).encode()) + 1 <= 256 * 1024
    finally:
        peer.finish()
        await query
        await peer.client.disconnect()


async def test_connection_rejects_a_second_query_owner():
    peer = BridgePeer()
    query = asyncio.create_task(peer.collect())
    peer.feed(peer.request())
    try:
        await peer.wait_responses()
        other = peer.client.query(
            "other", "hello", orchestration_capability=generate_orchestration_capability("other")
        )
        with pytest.raises(SidecarError):
            await asyncio.wait_for(anext(other), 0.05)
    finally:
        query.cancel()
        await asyncio.gather(query, return_exceptions=True)
        await peer.client.disconnect()


async def test_capability_cannot_be_reused_by_later_query():
    peer = BridgePeer()
    peer.finish()
    await peer.collect()
    peer.finish()
    try:
        with pytest.raises((SidecarError, ValueError)):
            await peer.collect()
    finally:
        await peer.client.disconnect()


async def test_disconnect_drains_pending_handler():
    started, drained = asyncio.Event(), asyncio.Event()

    async def handler(arguments, *, session_id):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    peer = BridgePeer(orchestration_handlers={"ping_thread_bridge": handler})
    query = asyncio.create_task(peer.collect())
    peer.feed(peer.request())
    try:
        await asyncio.wait_for(started.wait(), 1)
        await peer.client.disconnect()
        assert drained.is_set()
        assert not peer.responses()
    finally:
        query.cancel()
        await asyncio.gather(query, return_exceptions=True)


async def test_internal_response_is_never_yielded():
    peer = BridgePeer()
    peer.feed({"type": "orchestration_response", "id": "sess-1", "capability": "secret"})
    peer.finish()
    try:
        events = await peer.collect(capability=False)
        assert all(event.get("type") != "orchestration_response" for event in events)
    finally:
        await peer.client.disconnect()


async def test_completed_request_id_can_repeat_harmless_ping():
    peer = BridgePeer()
    query = asyncio.create_task(peer.collect())
    try:
        for index in range(40):
            peer.feed(peer.request())
            responses = await peer.wait_responses(index + 1)
            assert responses[-1]["ok"] is True
    finally:
        peer.finish()
        await query
        await peer.client.disconnect()


@pytest.mark.parametrize("extra", [0, 1])
async def test_response_bound_includes_entire_encoded_frame_and_newline(extra):
    base = {
        "type": "orchestration_response",
        "id": "sess-1",
        "request_id": "req-1",
        "ok": True,
        "result": {"blob": ""},
    }
    size = 256 * 1024 - len(json.dumps(base).encode()) - 1 + extra

    async def handler(arguments, *, session_id):
        return {"blob": "x" * size}

    peer = BridgePeer(orchestration_handlers={"ping_thread_bridge": handler})
    query = asyncio.create_task(peer.collect())
    peer.feed(peer.request())
    try:
        response = (await peer.wait_responses())[0]
        assert response["ok"] is (extra == 0)
        assert len(peer.writer.write.call_args_list[-1].args[0]) <= 256 * 1024
    finally:
        peer.finish()
        await query
        await peer.client.disconnect()


@pytest.mark.parametrize("result", [{"bad": object()}, {"bad": float("nan")}, []])
async def test_invalid_handler_result_returns_safe_error(result):
    async def handler(arguments, *, session_id):
        return result

    peer = BridgePeer(orchestration_handlers={"ping_thread_bridge": handler})
    query = asyncio.create_task(peer.collect())
    peer.feed(peer.request())
    try:
        responses = await peer.wait_responses()
        assert responses[0]["ok"] is False
        assert responses[0]["error"]["code"] == "handler_failed"
    finally:
        peer.finish()
        await query
        await peer.client.disconnect()
