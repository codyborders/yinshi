import assert from "node:assert/strict";
import test from "node:test";

import {
  ORCHESTRATION_ERROR_CODES,
  createOrchestrationRpc,
} from "../src/orchestration_rpc.js";

function makeRpc(overrides = {}) {
  const sent = [];
  const rpc = createOrchestrationRpc({
    sessionId: "sess-1",
    capability: "cap-token-123",
    send: (frame) => sent.push(frame),
    ...overrides,
  });
  return { rpc, sent };
}

test("request sends the strict wire frame and routes the response", async () => {
  const sent = [];
  const rpc = createOrchestrationRpc({
    sessionId: "sess-1",
    capability: "cap-token-123",
    send: (frame) => sent.push(frame),
  });

  const pending = rpc.request("ping_thread_bridge", { message: "ping" });

  assert.equal(sent.length, 1);
  const frame = sent[0];
  assert.equal(frame.type, "orchestration_request");
  assert.equal(frame.id, "sess-1");
  assert.equal(frame.capability, "cap-token-123");
  assert.equal(frame.operation, "ping_thread_bridge");
  assert.deepEqual(frame.arguments, { message: "ping" });
  assert.match(frame.request_id, /^[0-9a-f-]{36}$/);

  rpc.handleFrame({
    type: "orchestration_response",
    id: "sess-1",
    request_id: frame.request_id,
    ok: true,
    result: { status: "ok", echo: "ping" },
  });

  const result = await pending;
  assert.deepEqual(result, { status: "ok", echo: "ping" });
  assert.equal(rpc.pendingCount, 0);
});

test("error responses settle with a fixed code and message; remote text is never surfaced", async () => {
  const { rpc, sent } = makeRpc();

  const pending = rpc.request("ping_thread_bridge", {});
  const requestId = sent[0].request_id;

  rpc.handleFrame({
    type: "orchestration_response",
    id: "sess-1",
    request_id: requestId,
    ok: false,
    error: {
      code: "remote_stack_trace_1234",
      message: "secret internals: /srv/backend/handler.py line 42",
    },
  });
  // Duplicate delivery after settle must be swallowed silently.
  rpc.handleFrame({
    type: "orchestration_response",
    id: "sess-1",
    request_id: requestId,
    ok: true,
    result: {},
  });
  // Late response for an unknown request id must be swallowed too.
  rpc.handleFrame({
    type: "orchestration_response",
    id: "sess-1",
    request_id: "00000000-0000-0000-0000-000000000000",
    ok: true,
    result: {},
  });

  await assert.rejects(pending, (err) => {
    assert.equal(err.code, ORCHESTRATION_ERROR_CODES.handlerFailed);
    assert.equal(err.message, "The orchestration operation failed.");
    assert.doesNotMatch(String(err.stack ?? ""), /secret internals/);
    return true;
  });
  assert.equal(rpc.pendingCount, 0);
});

test("handleFrame rejects frames that do not match the exact session id", async () => {
  const { rpc, sent } = makeRpc();
  const pending = rpc.request("ping_thread_bridge", {});

  assert.equal(
    rpc.handleFrame({
      type: "orchestration_response",
      id: "sess-other",
      request_id: sent[0].request_id,
      ok: true,
      result: { hijacked: true },
    }),
    false,
  );

  const stillPending = await Promise.race([
    pending.then(() => "settled"),
    Promise.resolve("pending"),
  ]);
  assert.equal(stillPending, "pending");
  assert.equal(rpc.pendingCount, 1);

  rpc.dispose();
  await assert.rejects(pending, { code: "orchestration_disconnected" });
});

test("handleFrame ignores non-object frames and wrong types without consuming", () => {
  const { rpc } = makeRpc();
  assert.equal(rpc.handleFrame(null), false);
  assert.equal(rpc.handleFrame(undefined), false);
  assert.equal(rpc.handleFrame("orchestration_response"), false);
  assert.equal(rpc.handleFrame([]), false);
  assert.equal(rpc.handleFrame({ type: "other", id: "sess-1" }), false);
});

test("handleFrame enforces strict response shape for malformed ok and result", async () => {
  const { rpc, sent } = makeRpc();
  const first = rpc.request("ping_thread_bridge", {});
  rpc.handleFrame({
    type: "orchestration_response",
    id: "sess-1",
    request_id: sent[0].request_id,
    ok: "yes",
  });
  await assert.rejects(first, {
    code: "orchestration_bad_response",
    message: "The orchestration backend returned an invalid response.",
  });

  const second = rpc.request("ping_thread_bridge", {});
  rpc.handleFrame({
    type: "orchestration_response",
    id: "sess-1",
    request_id: sent[1].request_id,
    ok: true,
    result: "plain string",
  });
  await assert.rejects(second, { code: "orchestration_bad_response" });

  const third = rpc.request("ping_thread_bridge", {});
  rpc.handleFrame({
    type: "orchestration_response",
    id: "sess-1",
    request_id: sent[2].request_id,
    ok: true,
    result: null,
  });
  await assert.rejects(third, { code: "orchestration_bad_response" });
  assert.equal(rpc.pendingCount, 0);
});

test("handleFrame rejects responses above 256 KiB", async () => {
  const { rpc, sent } = makeRpc();
  const pending = rpc.request("ping_thread_bridge", {});
  rpc.handleFrame({
    type: "orchestration_response",
    id: "sess-1",
    request_id: sent[0].request_id,
    ok: true,
    result: { blob: "x".repeat(256 * 1024) },
  });
  await assert.rejects(pending, {
    code: "orchestration_result_too_large",
    message: "The orchestration response exceeded the allowed size.",
  });
  assert.equal(rpc.pendingCount, 0);
});

test("request refuses operations outside the allowlist", async () => {
  const { rpc, sent } = makeRpc();
  const rejected = assert.rejects(rpc.request("spawn_thread", {}), {
    code: "orchestration_operation_not_allowed",
    message: "The orchestration operation is not allowed.",
  });
  rpc.dispose();
  await rejected;
  assert.equal(sent.length, 0);
});

test("request rejects oversized frames before sending", async () => {
  const { rpc, sent } = makeRpc();
  const rejected = assert.rejects(
    rpc.request("ping_thread_bridge", { message: "x".repeat(64 * 1024) }),
    { code: "orchestration_frame_too_large" },
  );
  rpc.dispose();
  await rejected;
  assert.equal(sent.length, 0);
});

test("requests after dispose reject closed without sending", async () => {
  const { rpc, sent } = makeRpc();
  rpc.dispose();
  const rejected = assert.rejects(rpc.request("ping_thread_bridge", {}), {
    code: "orchestration_channel_closed",
  });
  rpc.dispose();
  await rejected;
  assert.equal(sent.length, 0);
});

test("request caps concurrent pending calls at sixteen", async () => {
  const { rpc, sent } = makeRpc();
  const accepted = Array.from({ length: 16 }, () =>
    rpc.request("ping_thread_bridge", {}).catch(() => undefined),
  );
  const rejected = assert.rejects(rpc.request("ping_thread_bridge", {}), {
    code: "orchestration_too_many_requests",
  });
  rpc.dispose();
  await rejected;
  await Promise.all(accepted);
  assert.equal(sent.length, 16);
});

test("abort settles a pending request immediately", async () => {
  const { rpc } = makeRpc();
  const controller = new AbortController();
  const rejected = assert.rejects(
    rpc.request("ping_thread_bridge", {}, { signal: controller.signal }),
    { code: "orchestration_cancelled" },
  );
  controller.abort();
  rpc.dispose();
  await rejected;
  assert.equal(rpc.pendingCount, 0);
});

test("send failure exposes only a fixed safe error", async () => {
  const { rpc } = makeRpc({ send() { throw new Error("private-token=/private/path"); } });
  await assert.rejects(rpc.request("ping_thread_bridge", {}), {
    code: "orchestration_send_failed",
    message: "The orchestration request could not be sent.",
  });
  assert.equal(rpc.pendingCount, 0);
  rpc.dispose();
});

test("response shape requires exactly one tagged payload", async () => {
  for (const payload of [
    { ok: true },
    { ok: true, result: {}, error: { code: "handler_failed", message: "failed" } },
    { ok: false, result: {}, error: { code: "handler_failed", message: "failed" } },
  ]) {
    const { rpc, sent } = makeRpc();
    const rejected = assert.rejects(rpc.request("ping_thread_bridge", {}), { code: "orchestration_bad_response" });
    rpc.handleFrame({ type: "orchestration_response", id: "sess-1", request_id: sent[0].request_id, ...payload });
    rpc.dispose();
    await rejected;
  }
});

test("response limit includes the envelope and newline", async () => {
  const { rpc, sent } = makeRpc();
  const rejected = assert.rejects(rpc.request("ping_thread_bridge", {}), { code: "orchestration_result_too_large" });
  const frame = { type: "orchestration_response", id: "sess-1", request_id: sent[0].request_id, ok: true, result: { blob: "" } };
  frame.result.blob = "x".repeat(256 * 1024 - Buffer.byteLength(JSON.stringify(frame)));
  rpc.handleFrame(frame);
  rpc.dispose();
  await rejected;
});

test("request limit includes its newline", async () => {
  const { rpc } = makeRpc();
  const frame = { type: "orchestration_request", id: "sess-1", request_id: "x".repeat(36), capability: "cap-token-123", operation: "ping_thread_bridge", arguments: { message: "" } };
  const message = "x".repeat(64 * 1024 - Buffer.byteLength(JSON.stringify(frame)));
  const rejected = assert.rejects(rpc.request("ping_thread_bridge", { message }), { code: "orchestration_frame_too_large" });
  rpc.dispose();
  await rejected;
});

test("dispose rejects every pending request as disconnected", async () => {
  const sent = [];
  const rpc = createOrchestrationRpc({
    sessionId: "sess-1",
    capability: "cap-token-123",
    send: (frame) => sent.push(frame),
  });

  const first = rpc.request("ping_thread_bridge", {});
  const second = rpc.request("ping_thread_bridge", {});
  assert.equal(rpc.pendingCount, 2);

  rpc.dispose();
  assert.equal(rpc.pendingCount, 0);

  await assert.rejects(first, (err) => {
    assert.equal(err.code, "orchestration_disconnected");
    return true;
  });
  await assert.rejects(second, (err) => {
    assert.equal(err.code, "orchestration_disconnected");
    return true;
  });
});

test("request rejects non-object arguments without sending", async () => {
  const { rpc, sent } = makeRpc();
  for (const badArgs of [null, "hello", 42, [1, 2]]) {
    await assert.rejects(rpc.request("ping_thread_bridge", badArgs), {
      code: "orchestration_invalid_arguments",
    });
  }
  assert.equal(sent.length, 0);
  assert.equal(rpc.pendingCount, 0);
});

test("handleFrame refuses response frames with unexpected top-level keys", async () => {
  const { rpc, sent } = makeRpc();
  const pending = rpc.request("ping_thread_bridge", {});
  rpc.handleFrame({
    type: "orchestration_response",
    id: "sess-1",
    request_id: sent[0].request_id,
    ok: true,
    result: {},
    injected: true,
  });
  await assert.rejects(pending, { code: "orchestration_bad_response" });
  assert.equal(rpc.pendingCount, 0);

  // Unknown request id with extra keys is still consumed and dropped.
  assert.equal(
    rpc.handleFrame({
      type: "orchestration_response",
      id: "sess-1",
      request_id: "missing",
      ok: true,
      result: {},
      injected: true,
    }),
    true,
  );
});

test("overlapping requests settle independently out of order", async () => {
  const { rpc, sent } = makeRpc();
  const first = rpc.request("ping_thread_bridge", { message: "one" });
  const second = rpc.request("ping_thread_bridge", { message: "two" });
  assert.equal(rpc.pendingCount, 2);

  rpc.handleFrame({
    type: "orchestration_response",
    id: "sess-1",
    request_id: sent[1].request_id,
    ok: true,
    result: { echo: "two" },
  });
  assert.deepEqual(await second, { echo: "two" });
  assert.equal(rpc.pendingCount, 1);

  rpc.handleFrame({
    type: "orchestration_response",
    id: "sess-1",
    request_id: sent[0].request_id,
    ok: true,
    result: { echo: "one" },
  });
  assert.deepEqual(await first, { echo: "one" });
  assert.equal(rpc.pendingCount, 0);
});

test("late response after timeout stays inert", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { rpc, sent } = makeRpc({ timeoutMs: 1_000 });
  const pending = rpc.request("ping_thread_bridge", {});
  const rejection = assert.rejects(pending, {
    code: "orchestration_timeout",
  });

  t.mock.timers.tick(2_000);
  await rejection;
  assert.equal(rpc.pendingCount, 0);

  rpc.handleFrame({
    type: "orchestration_response",
    id: "sess-1",
    request_id: sent[0].request_id,
    ok: true,
    result: { late: true },
  });
});
