import assert from "node:assert/strict";
import test from "node:test";

import { createThreadBridgePingTool } from "../src/orchestration_tools.js";

test("ping tool executes a round trip and returns bounded JSON content", async () => {
  const sent = [];
  const rpc = {
    request: async (operation, args) => {
      sent.push({ operation, args });
      return { status: "ok", echo: args.message, session_bound: true };
    },
  };

  const tool = createThreadBridgePingTool({ rpcForCall: () => rpc });
  assert.equal(tool.name, "thread_bridge_ping");
  assert.equal(typeof tool.label, "string");
  assert.equal(typeof tool.description, "string");
  assert.ok(tool.parameters, "tool must define a TypeBox parameter schema");

  const result = await tool.execute(
    "call-1",
    { message: "hello bridge" },
    undefined,
    undefined,
    {},
  );

  assert.deepEqual(sent, [
    { operation: "ping_thread_bridge", args: { message: "hello bridge" } },
  ]);
  assert.equal(result.content[0].type, "text");
  const parsed = JSON.parse(result.content[0].text);
  assert.equal(parsed.status, "ok");
  assert.equal(parsed.echo, "hello bridge");
  assert.equal(parsed.session_bound, true);
});

test("ping tool rejects invalid arguments without calling the backend", async () => {
  let calls = 0;
  const tool = createThreadBridgePingTool({ rpcForCall: () => ({
    request: async () => { calls += 1; return {}; },
  }) });
  for (const params of [{ message: 42 }, { extra: true }, { message: "x".repeat(257) }, []]) {
    await assert.rejects(tool.execute("call-1", params), /Invalid bridge arguments/);
  }
  assert.equal(calls, 0);
});

test("ping tool fails closed when the channel is not active", async () => {
  const tool = createThreadBridgePingTool({ rpcForCall: () => null });

  await assert.rejects(
    tool.execute("call-1", {}, undefined, undefined, {}),
    /orchestration channel is not active/,
  );
});

test("ping tool forwards the abort signal to the channel", async () => {
  const captured = [];
  const rpc = {
    request: async (operation, args, options) => {
      captured.push(options);
      return { status: "ok" };
    },
  };
  const tool = createThreadBridgePingTool({ rpcForCall: () => rpc });
  const controller = new AbortController();

  await tool.execute("call-1", {}, controller.signal, undefined, {});

  assert.equal(captured.length, 1);
  assert.equal(captured[0].signal, controller.signal);
});

test("ping tool surfaces cancellation from the channel", async () => {
  const rpc = {
    request: async (operation, args, options) => {
      return new Promise((resolve, reject) => {
        options.signal.addEventListener("abort", () => {
          const error = new Error("The orchestration request was cancelled.");
          error.code = "orchestration_cancelled";
          reject(error);
        });
      });
    },
  };
  const tool = createThreadBridgePingTool({ rpcForCall: () => rpc });
  const controller = new AbortController();

  const pending = tool.execute("call-1", {}, controller.signal, undefined, {});
  controller.abort();
  await assert.rejects(pending, { code: "orchestration_cancelled" });
});
