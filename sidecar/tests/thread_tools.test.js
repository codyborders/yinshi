import assert from "node:assert/strict";
import test from "node:test";

import * as toolsModule from "../src/orchestration_tools.js";
import { THREAD_OPERATIONS } from "../src/orchestration_rpc.js";
import { Check } from "typebox/value";

test("published thread schemas enforce model input bounds without authority fields", async t => {
  const tools = toolsModule.createThreadTools({ allowedOperations: THREAD_OPERATIONS, rpcForCall: () => null });
  const cases = {
    spawn_thread: [{ title: "Child", task: "Inspect" }, [{ title: "x".repeat(201), task: "Inspect" }, { title: "Child", task: "Inspect", parent_session_id: "forged" }]],
    list_children: [{ include_terminal: false }, [{ parent_id: "forged" }, { include_terminal: "false" }]],
    get_thread: [{ thread_id: "child", include_result: false }, [{ thread_id: "child", include_result: "true" }, { thread_id: "child", include_messages: true }]],
    wait_for_threads: [{ thread_ids: ["child"], timeout_seconds: 60 }, [{ thread_ids: ["child"], timeout_seconds: 61 }, { thread_ids: ["child", "child"] }]],
    cancel_thread: [{ thread_id: "child", cascade: true }, [{ thread_id: "child", cascade: "true" }]],
    report_thread_result: [{ summary: "Done", tests: [{ command: "pytest", status: "passed", summary: "x".repeat(4000) }], warnings: ["x".repeat(4000)] }, [{ summary: "x".repeat(20001) }, { summary: "Done", expected_version: 0 }, { summary: "Done", warnings: ["x".repeat(4001)] }, { summary: "Done", tests: [{ command: "pytest", status: "unknown" }] }]],
  };
  for (const tool of tools) {
    await t.test(tool.name, () => {
      assert.ok(tool.parameters);
      assert.equal(typeof tool.label, "string");
      assert.equal(typeof tool.description, "string");
      const [valid, invalid] = cases[tool.name];
      assert.equal(Check(tool.parameters, valid), true);
      for (const input of invalid) assert.equal(Check(tool.parameters, input), false);
    });
  }
});

test("thread execution rejects invalid arguments before channel access and handles inactive channels safely", async () => {
  let channelReads = 0;
  const [tool] = toolsModule.createThreadTools({
    allowedOperations: ["spawn_thread"],
    rpcForCall: () => { channelReads += 1; return null; },
  });
  await assert.rejects(tool.execute("call", { title: "Child", task: "Inspect", parent_id: "forged" }), /^Error: Invalid thread tool arguments\.$/);
  assert.equal(channelReads, 0);
  await assert.rejects(tool.execute("call", { title: "Child", task: "Inspect" }), /^Error: Thread orchestration channel is not active\.$/);
});

test("thread tool factory rejects invalid permission manifests", async t => {
  const cases = [
    ["unknown operation", { allowedOperations: ["unknown"], rpcForCall: () => null }],
    ["duplicate operation", { allowedOperations: ["spawn_thread", "spawn_thread"], rpcForCall: () => null }],
    ["noncallable channel", { allowedOperations: ["spawn_thread"], rpcForCall: null }],
  ];
  for (const [name, options] of cases) {
    await t.test(name, () => assert.throws(() => toolsModule.createThreadTools(options), TypeError));
  }
});

test("thread tools follow backend permissions and preserve SDK call identity", async () => {
  assert.equal(typeof toolsModule.createThreadTools, "function");
  const calls = [];
  const rpc = { request: async (...args) => { calls.push(args); return { status: "accepted" }; } };
  const children = toolsModule.createThreadTools({ allowedOperations: THREAD_OPERATIONS, rpcForCall: () => rpc });
  assert.deepEqual(children.map(tool => tool.name).sort(), [...THREAD_OPERATIONS].sort());
  const roots = toolsModule.createThreadTools({ allowedOperations: THREAD_OPERATIONS.filter(name => name !== "report_thread_result"), rpcForCall: () => rpc });
  assert.equal(roots.length, 5);
  assert.equal(roots.some(tool => tool.name === "report_thread_result"), false);
  const tool = children.find(tool => tool.name === "spawn_thread");
  const signal = new AbortController().signal;
  const result = await tool.execute("immutable-call", { title: "Child", task: "Inspect" }, signal);
  assert.equal(calls[0][0], "spawn_thread");
  assert.deepEqual(calls[0][2], { signal, toolCallId: "immutable-call" });
  assert.equal(result.isError, undefined);
  assert.deepEqual(toolsModule.createThreadTools({ allowedOperations: [], rpcForCall: () => null }), []);
});
