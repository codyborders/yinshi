"""Verify version-two query permissions and trusted handler identity."""

import asyncio
import json

from tests.test_sidecar_orchestration_lifecycle import BridgePeer
from yinshi.services.orchestration_bridge import generate_orchestration_capability


async def test_backend_handler_deadline_leaves_grace_after_the_domain_wait(monkeypatch):
    timeouts = []
    wait_for = asyncio.wait_for

    async def record_timeout(awaitable, timeout):
        timeouts.append(timeout)
        return await wait_for(awaitable, timeout)

    async def handler(arguments, *, caller):
        return {"threads": [], "complete": True}

    monkeypatch.setattr(asyncio, "wait_for", record_timeout)
    peer = BridgePeer(orchestration_handlers={"wait_for_threads": handler})
    peer.capability = generate_orchestration_capability(
        "sess-1",
        run_id="run-1",
        allowed_operations=frozenset({"wait_for_threads"}),
        database_path="/backend/tenant/yinshi.db",
    )
    query = asyncio.create_task(peer.collect())
    peer.feed(
        peer.request(operation="wait_for_threads", protocol_version=2, tool_call_id="sdk-wait")
    )
    try:
        assert (await peer.wait_responses())[0]["ok"] is True
        assert timeouts == [65]
    finally:
        peer.finish()
        await query
        await peer.client.disconnect()


async def test_v2_cancellation_requires_the_query_capability_and_keeps_query_alive():
    started, drained = asyncio.Event(), asyncio.Event()

    async def handler(arguments, *, caller):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    peer = BridgePeer(orchestration_handlers={"wait_for_threads": handler})
    peer.capability = generate_orchestration_capability(
        "sess-1",
        run_id="run-1",
        allowed_operations=frozenset({"wait_for_threads"}),
        database_path="/backend/tenant/yinshi.db",
    )
    query = asyncio.create_task(peer.collect())
    peer.feed(
        peer.request(operation="wait_for_threads", protocol_version=2, tool_call_id="sdk-wait")
    )
    cancel = {
        "type": "orchestration_cancel",
        "protocol_version": 2,
        "id": "sess-1",
        "request_id": "req-1",
        "capability": peer.capability.token,
    }
    try:
        await asyncio.wait_for(started.wait(), 1)
        peer.feed({**cancel, "capability": "foreign"})
        await asyncio.sleep(0.02)
        assert not drained.is_set()
        peer.feed(cancel)
        await asyncio.wait_for(drained.wait(), 1)
        assert not query.done()
    finally:
        peer.finish()
        await asyncio.gather(query, return_exceptions=True)
        await peer.client.disconnect()


async def test_v2_query_dispatches_verified_caller_and_selected_permissions():
    observed = []

    async def handler(arguments, *, caller):
        observed.append(caller)
        return {"thread_id": "child"}

    peer = BridgePeer(orchestration_handlers={"spawn_thread": handler})
    peer.capability = generate_orchestration_capability(
        "sess-1",
        run_id="run-1",
        tenant_id="owner",
        runtime_id="workspace-runtime",
        allowed_operations=frozenset({"spawn_thread"}),
        database_path="/backend/tenant/yinshi.db",
    )
    query = asyncio.create_task(peer.collect())
    peer.feed(peer.request(operation="spawn_thread", protocol_version=2, tool_call_id="sdk-call"))
    try:
        response = (await peer.wait_responses())[0]
        assert response["ok"] is True
        assert (observed[0].run_id, observed[0].tool_call_id, observed[0].tenant_id) == (
            "run-1",
            "sdk-call",
            "owner",
        )
        sent_query = json.loads(peer.writer.write.call_args_list[0].args[0])
        options = sent_query["options"]["orchestration"]
        assert options["protocol_version"] == 2
        assert options["allowed_operations"] == ["spawn_thread"]
    finally:
        peer.finish()
        await query
        await peer.client.disconnect()
