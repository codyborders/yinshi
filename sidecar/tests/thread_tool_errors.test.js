import assert from "node:assert/strict";
import test from "node:test";
import { createOrchestrationRpc } from "../src/orchestration_rpc.js";
import { createThreadTools } from "../src/orchestration_tools.js";

const messages = {
  depth_exceeded: "The thread depth limit has been reached.",
  child_limit_exceeded: "The direct-child limit has been reached.",
  active_thread_limit_exceeded: "The active-thread limit has been reached.",
  tree_limit_exceeded: "The thread tree limit has been reached.",
  spawn_turn_limit_exceeded: "The current turn has reached its child spawn limit.",
  thread_not_found: "Thread not found.",
  runtime_unavailable: "The thread runtime is unavailable.",
  workspace_provisioning_failed: "The child workspace could not be provisioned.",
};

function channel(error, extra = {}) {
  const rpc = createOrchestrationRpc({
    sessionId: "private-session", capability: "private-capability", protocolVersion: 2,
    allowedOperations: ["spawn_thread"],
    send: frame => rpc.handleFrame({
      type: "orchestration_response", id: frame.id, request_id: frame.request_id,
      ok: false, error, ...extra,
    }),
  });
  const [tool] = createThreadTools({ allowedOperations: ["spawn_thread"], rpcForCall: () => rpc });
  return { rpc, tool };
}

test("known v2 tool failures expose fixed JSON through the SDK error message", async t => {
  for (const [code, message] of Object.entries(messages)) {
    await t.test(code, async () => {
      const { rpc, tool } = channel({ code, message: "private-server-path-and-token" });
      try {
        await assert.rejects(tool.execute("private-call", { title: "private-title", task: "private-task" }), error => {
          assert.equal(error.code, code);
          assert.deepEqual(JSON.parse(error.message), { error: { code, message } });
          assert.equal(error.message.includes("private-"), false);
          return true;
        });
        assert.equal(rpc.pendingCount, 0);
      } finally {
        rpc.dispose();
      }
    });
  }
});

test("malformed and unknown remote errors cannot surface known codes or private content", async t => {
  for (const error of [
    null,
    ["depth_exceeded", "private-message"],
    { code: "depth_exceeded" },
    { code: "depth_exceeded", message: 4 },
    { code: "depth_exceeded", message: "private-message", private: true },
    { code: "__proto__", message: "private-message" },
    { code: "private-code", message: "private-message" },
  ]) {
    await t.test(JSON.stringify(error), async () => {
      const { rpc, tool } = channel(error);
      try {
        await assert.rejects(tool.execute("call", { title: "Child", task: "Inspect" }), {
          code: "handler_failed", message: "The orchestration operation failed.",
        });
      } finally {
        rpc.dispose();
      }
    });
  }
});

test("known errors in malformed outer envelopes retain strict rejection", async () => {
  const { rpc, tool } = channel({ code: "depth_exceeded", message: "private-message" }, { result: { private: true } });
  try {
    await assert.rejects(tool.execute("call", { title: "Child", task: "Inspect" }), { code: "orchestration_bad_response" });
  } finally {
    rpc.dispose();
  }
});

test("legacy ping retains its generic error even for a known domain code", async () => {
  const rpc = createOrchestrationRpc({
    sessionId: "session", capability: "private-capability",
    send: frame => rpc.handleFrame({
      type: "orchestration_response", id: frame.id, request_id: frame.request_id,
      ok: false, error: { code: "depth_exceeded", message: "private-message" },
    }),
  });
  try {
    await assert.rejects(rpc.request("ping_thread_bridge", {}), {
      code: "handler_failed", message: "The orchestration operation failed.",
    });
  } finally {
    rpc.dispose();
  }
});
