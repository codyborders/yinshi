import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import { YinshiSidecar } from "../src/sidecar.js";

function recordingSocket() {
  const socket = new EventEmitter();
  socket.destroyed = false;
  socket.messages = [];
  socket.write = (value) => {
    socket.messages.push(JSON.parse(String(value).trim()));
    return true;
  };
  return socket;
}

test(
  "bridge tool round trip keeps ordinary tool events visible",
  { timeout: 4000 },
  async () => {
    const sidecar = new YinshiSidecar();
    let capturedTools = [];
    let emitPiEvent;
    sidecar._createPiSession = async (...args) => {
      capturedTools = args[args.length - 1];
      return {
        session: {
          subscribe(listener) {
            emitPiEvent = listener;
            return () => {};
          },
          async prompt() {
            emitPiEvent({ type: "tool_execution_start", toolCallId: "ordinary-1", toolName: "read", args: { path: "README.md" } });
            emitPiEvent({ type: "tool_execution_end", toolCallId: "ordinary-1", result: { content: [{ type: "text", text: "ordinary content" }] } });
            const tool = capturedTools.find((t) => t.name === "thread_bridge_ping");
            await tool.execute(
              "call-1",
              { message: "round trip" },
              undefined,
              undefined,
              {},
            );
          },
          abortCompaction() {},
          abortRetry() {},
          async abort() {},
          dispose() {},
        },
        model: { provider: "test", id: "model" },
        piSessionFile: null,
      };
    };

    const socket = recordingSocket();
    const queryDone = sidecar.processQuery("session-1", socket, "hello", {
      orchestration: { capability: "cap-token-123" },
    });

    // Simulate the backend answering as soon as the request hits the wire.
    while (
      !socket.messages.some((m) => m.type === "orchestration_request")
    ) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    const request = socket.messages.find(
      (m) => m.type === "orchestration_request",
    );
    sidecar.handleRequest(
      {
        type: "orchestration_response",
        id: "session-1",
        request_id: request.request_id,
        ok: true,
        result: {
          status: "ok",
          echo: "round trip",
          session_bound: true,
          session_id: "session-1",
        },
      },
      socket,
    );
    await queryDone;

    // The sidecar emitted exactly one orchestration request over the wire.
    const requests = socket.messages.filter(
      (m) => m.type === "orchestration_request",
    );
    assert.equal(requests.length, 1);
    assert.equal(requests[0].id, "session-1");
    assert.equal(requests[0].capability, "cap-token-123");
    assert.equal(requests[0].operation, "ping_thread_bridge");
    assert.deepEqual(requests[0].arguments, { message: "round trip" });

    assert.ok(socket.messages.some(frame => frame.data?.type === "tool_use" && frame.data.toolName === "read"));
    assert.ok(socket.messages.some(frame => frame.type === "tool_result" && frame.tool_use_id === "ordinary-1" && frame.content.includes("ordinary content")));

    // Internal frames stay on the wire and never enter the event stream.
    assert.ok(
      !socket.messages.some(
        (m) => m.type === "message" && m.data?.type === "orchestration_request",
      ),
    );

    // After the query ends the channel holder is cleared: no stale capability.
    assert.equal(sidecar.activeSessions.get("session-1").orchestration.rpc, null);

    sidecar.cleanup();
  },
);
