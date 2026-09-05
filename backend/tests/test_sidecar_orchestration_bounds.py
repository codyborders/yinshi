"""Pending duplication and capacity exhaustion close the offending query."""

import asyncio

import pytest

from tests.test_sidecar_orchestration_lifecycle import BridgePeer
from yinshi.exceptions import SidecarError


@pytest.mark.parametrize("request_ids", [["duplicate", "duplicate"], ["first", "second", "third"]])
async def test_pending_conflicts_fail_closed_and_drain(request_ids):
    started = asyncio.Event()
    drained = asyncio.Event()

    async def handler(arguments, *, session_id):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    peer = BridgePeer(
        orchestration_handlers={"ping_thread_bridge": handler},
        orchestration_max_pending=2,
    )
    query = asyncio.create_task(peer.collect())
    peer.feed(peer.request(request_ids[0]))
    try:
        await asyncio.wait_for(started.wait(), 1)
        for request_id in request_ids[1:]:
            peer.feed(peer.request(request_id))
        with pytest.raises(SidecarError):
            await asyncio.wait_for(query, 1)
        assert drained.is_set()
        assert not peer.client.connected
        assert peer.responses() == []
    finally:
        query.cancel()
        await asyncio.gather(query, return_exceptions=True)
        await peer.client.disconnect()
