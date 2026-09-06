import assert from "node:assert/strict";
import test from "node:test";
import { createOrchestrationRpc } from "../src/orchestration_rpc.js";

test("version two abort authenticates cancellation of only its request", async () => {
  const sent = [];
  const controller = new AbortController();
  const rpc = createOrchestrationRpc({ sessionId: "parent", capability: "secret", protocolVersion: 2, allowedOperations: ["wait_for_threads"], send: frame => sent.push(frame) });
  const pending = rpc.request("wait_for_threads", { thread_ids: ["child"], timeout_seconds: 60 }, { toolCallId: "sdk-wait", signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, { code: "orchestration_cancelled" });
  assert.equal(sent.length, 2);
  assert.deepEqual(sent[1], { type: "orchestration_cancel", protocol_version: 2, id: "parent", request_id: sent[0].request_id, capability: "secret" });
  assert.equal(rpc.pendingCount, 0);
  rpc.dispose();
});

test("version two preserves SDK identity independently of transport delivery", async () => {
  const sent = [];
  const rpc = createOrchestrationRpc({ sessionId: "parent", capability: "secret", protocolVersion: 2, allowedOperations: ["spawn_thread"], send: frame => sent.push(frame) });
  const pending = rpc.request("spawn_thread", { title: "Child", task: "Inspect" }, { toolCallId: "sdk-call" });
  pending.catch(() => {});
  try {
    assert.equal(sent.length, 1);
    assert.equal(sent[0].protocol_version, 2);
    assert.equal(sent[0].tool_call_id, "sdk-call");
    assert.notEqual(sent[0].request_id, "sdk-call");
    rpc.handleFrame({ type: "orchestration_response", id: "parent", request_id: sent[0].request_id, ok: true, result: { thread_id: "child" } });
    assert.deepEqual(await pending, { thread_id: "child" });
  } finally {
    rpc.dispose();
    await Promise.allSettled([pending]);
  }
});
