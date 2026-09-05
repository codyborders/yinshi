"""End-to-end bridge integration test over a real Unix socket.

A scripted peer stands in for the Node sidecar: it accepts the query frame,
reads the capability from its in-memory query options, drives one harmless
``ping_thread_bridge`` round trip, and then finishes the query. The backend
``SidecarClient`` runs unmodified against the real transport.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from yinshi.services.orchestration_bridge import (
    generate_orchestration_capability,
)
from yinshi.services.sidecar import SidecarClient


async def test_harmless_operation_round_trips_over_real_socket() -> None:
    # AF_UNIX paths are short on some platforms; keep the socket at the root
    # of the temp directory with a unique, short name.
    socket_path = f"/tmp/yinshi-bridge-{id(object())}-{uuid.uuid4().hex[:8]}.sock"
    observed: dict[str, object] = {}

    async def peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b'{"type": "init_status", "success": true}\n')
        await writer.drain()

        query_line = await reader.readline()
        query = json.loads(query_line.decode())
        capability = query["options"]["orchestration"]["capability"]
        observed["capability"] = capability
        observed["session_id"] = query["id"]

        writer.write(
            json.dumps(
                {
                    "id": query["id"],
                    "type": "message",
                    "data": {"type": "assistant"},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()

        request_id = uuid.uuid4().hex
        writer.write(
            json.dumps(
                {
                    "type": "orchestration_request",
                    "id": query["id"],
                    "request_id": request_id,
                    "capability": capability,
                    "operation": "ping_thread_bridge",
                    "arguments": {"message": "round trip"},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()

        response_line = await reader.readline()
        response = json.loads(response_line.decode())
        observed["response"] = response
        observed["request_id"] = request_id

        writer.write(
            json.dumps(
                {
                    "id": query["id"],
                    "type": "message",
                    "data": {"type": "result"},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(peer, path=socket_path)
    try:
        client = SidecarClient()
        await client.connect(socket_path)
        capability = generate_orchestration_capability("sess-1", run_id="run-1")

        events = [
            event
            async for event in client.query(
                "sess-1",
                "hello",
                orchestration_capability=capability,
            )
        ]
        await client.disconnect()
    finally:
        server.close()
        await server.wait_closed()

    # The sidecar peer received the exact capability in memory.
    assert observed["session_id"] == "sess-1"
    assert observed["capability"] == capability.token
    # The backend answered the harmless operation over the wire.
    response = observed["response"]
    assert response["type"] == "orchestration_response"
    assert response["request_id"] == observed["request_id"]
    assert response["ok"] is True
    assert response["result"]["echo"] == "round trip"
    assert response["result"]["session_bound"] is True
    # The model-facing event stream only saw ordinary Pi events.
    assert [event.get("type") for event in events] == ["message", "message"]
    assert capability.token not in json.dumps(events)
