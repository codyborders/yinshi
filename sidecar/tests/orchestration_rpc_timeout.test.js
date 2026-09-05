import assert from "node:assert/strict";
import test from "node:test";

import { createOrchestrationRpc } from "../src/orchestration_rpc.js";

test(
  "timed-out requests reject and late responses are ignored",
  { timeout: 2000 },
  async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const sent = [];
    const rpc = createOrchestrationRpc({
      sessionId: "sess-1",
      capability: "cap-token-123",
      send: (frame) => sent.push(frame),
      timeoutMs: 10,
    });

    const pending = rpc.request("ping_thread_bridge", {});
    const requestId = sent[0].request_id;
    const rejection = assert.rejects(pending, (err) => {
      assert.equal(err.code, "orchestration_timeout");
      return true;
    });

    t.mock.timers.tick(100);
    await rejection;
    assert.equal(rpc.pendingCount, 0);

    // A late response after the timeout must not crash or resurrect state.
    rpc.handleFrame({
      type: "orchestration_response",
      id: "sess-1",
      request_id: requestId,
      ok: true,
      result: {},
    });
  },
);
