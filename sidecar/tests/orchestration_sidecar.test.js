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

test("overlapping bridge query cannot replace its active owner", async () => {
  const sidecar = new YinshiSidecar();
  let release;
  let started;
  const blocked = new Promise(resolve => { release = resolve; });
  const ready = new Promise(resolve => { started = resolve; });
  sidecar._createPiSession = async () => {
    const created = sessionResult();
    created.session.prompt = async () => { started(); await blocked; };
    return created;
  };
  const owner = recordingSocket();
  const intruder = recordingSocket();
  const first = sidecar.processQuery("same", owner, "first", { orchestration: { capability: "first-token" } });
  await ready;
  const second = sidecar.processQuery("same", intruder, "second", { orchestration: { capability: "second-token" } });
  try {
    await Promise.race([second, new Promise(resolve => setTimeout(resolve, 30))]);
    assert.ok(intruder.messages.some(frame => frame.type === "error"));
    assert.equal(sidecar.activeSessions.get("same").orchestration.socket, owner);
  } finally {
    release();
    await Promise.all([first, second]);
    sidecar.cleanup();
  }
});

test("bridge query rejects malformed capability options before session creation", async () => {
  const sidecar = new YinshiSidecar();
  let created = 0;
  sidecar._createPiSession = async () => { created += 1; return sessionResult(); };
  try {
    for (const orchestration of [{ capability: "x".repeat(257) }, { capability: "token", extra: true }, [], { capability: "\u2603" }]) {
      const socket = recordingSocket();
      await sidecar.processQuery("invalid", socket, "hello", { orchestration });
      assert.ok(socket.messages.some(frame => frame.type === "error"));
    }
    assert.equal(created, 0);
  } finally {
    sidecar.cleanup();
  }
});

function sessionResult() {
  return {
    session: {
      subscribe() {
        return () => {};
      },
      async prompt() {},
      abortCompaction() {},
      abortRetry() {},
      async abort() {},
      dispose() {},
    },
    model: { provider: "test", id: "model" },
    piSessionFile: null,
  };
}

test(
  "processQuery registers the bridge tool only when a capability is present",
  { timeout: 4000 },
  async () => {
    const sidecar = new YinshiSidecar();
    const captured = [];
    sidecar._createPiSession = async (...args) => {
      captured.push(args);
      return sessionResult();
    };

    const socket = recordingSocket();
    await sidecar.processQuery("session-1", socket, "hello", {
      orchestration: { capability: "cap-token-123" },
    });

    const withCapability = captured.at(-1);
    const tools = withCapability[withCapability.length - 1];
    assert.equal(Array.isArray(tools), true);
    assert.equal(tools.some((tool) => tool.name === "thread_bridge_ping"), true);

    const plainSocket = recordingSocket();
    await sidecar.processQuery("session-2", plainSocket, "hello", {});
    const withoutCapability = captured.at(-1);
    const plainTools = withoutCapability[withoutCapability.length - 1];
    assert.equal(plainTools.some((tool) => tool.name === "thread_bridge_ping"), false);

    sidecar.cleanup();
  },
);

test(
  "orchestration responses route only from the originating socket",
  { timeout: 4000 },
  async () => {
    const recordingSocket = () => {
      const socket = new EventEmitter();
      socket.destroyed = false;
      socket.messages = [];
      socket.write = (value) => {
        socket.messages.push(JSON.parse(String(value).trim()));
        return true;
      };
      return socket;
    };
    const settleProbe = async (promise, waitMs) => {
      let settled = false;
      const probe = promise.then(
        () => {
          settled = true;
        },
        () => {
          settled = true;
        },
      );
      const verdict = await Promise.race([
        probe,
        new Promise((resolve) => setTimeout(resolve, waitMs)),
      ]);
      return verdict === undefined ? settled : settled;
    };

    const sidecar = new YinshiSidecar();
    let bridgeTool = null;
    let toolPromise = null;
    sidecar._createPiSession = async (...args) => {
      bridgeTool = args[args.length - 1]
        .find((tool) => tool.name === "thread_bridge_ping");
      return {
        session: {
          subscribe() {
            return () => {};
          },
          async prompt() {
            toolPromise = bridgeTool.execute(
              "call-1",
              {},
              undefined,
              undefined,
              {},
            );
            await toolPromise;
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

    const socketA = recordingSocket();
    const queryDone = sidecar.processQuery("session-1", socketA, "hello", {
      orchestration: { capability: "cap-1" },
    });
    let request = null;
    for (let attempt = 0; attempt < 200 && !request; attempt += 1) {
      request = socketA.messages.find((m) => m.type === "orchestration_request") || null;
      if (!request) {
        await new Promise((resolve) => setTimeout(resolve, 5));
      }
    }
    assert.ok(request, "no orchestration_request was sent");

    // A response arriving on a different socket must not settle the tool.
    sidecar.handleRequest(
      {
        type: "orchestration_response",
        id: "session-1",
        request_id: request.request_id,
        ok: true,
        result: { hijacked: true },
      },
      recordingSocket(),
    );
    assert.equal(await settleProbe(toolPromise, 60), false);

    // A wrong session id on the right socket must also be dropped.
    sidecar.handleRequest(
      {
        type: "orchestration_response",
        id: "other-session",
        request_id: request.request_id,
        ok: true,
        result: {},
      },
      socketA,
    );
    assert.equal(await settleProbe(toolPromise, 60), false);

    // The originating socket settles it.
    sidecar.handleRequest(
      {
        type: "orchestration_response",
        id: "session-1",
        request_id: request.request_id,
        ok: true,
        result: { status: "ok" },
      },
      socketA,
    );
    assert.equal(await settleProbe(toolPromise, 4000), true);

    await queryDone;
    assert.equal(
      socketA.messages.filter((m) => m.type === "orchestration_request").length,
      1,
    );
    sidecar.cleanup();
  },
);

test(
  "socket close disposes the pending RPC before the prompt unwinds",
  { timeout: 4000 },
  async () => {
    const recordingSocket = () => {
      const socket = new EventEmitter();
      socket.destroyed = false;
      socket.messages = [];
      socket.write = (value) => {
        socket.messages.push(JSON.parse(String(value).trim()));
        return true;
      };
      return socket;
    };
    const settleProbe = async (promise, waitMs) => {
      let settled = false;
      const probe = promise.then(
        () => {
          settled = true;
        },
        () => {
          settled = true;
        },
      );
      await Promise.race([
        probe,
        new Promise((resolve) => setTimeout(resolve, waitMs)),
      ]);
      return settled;
    };

    const sidecar = new YinshiSidecar();
    let bridgeTool = null;
    let toolPromise = null;
    let toolError = null;
    let releasePrompt;
    const promptGate = new Promise((resolve) => {
      releasePrompt = resolve;
    });
    sidecar._createPiSession = async (...args) => {
      bridgeTool = args[args.length - 1]
        .find((tool) => tool.name === "thread_bridge_ping");
      return {
        session: {
          subscribe() {
            return () => {};
          },
          async prompt() {
            toolPromise = bridgeTool.execute(
              "call-1",
              {},
              undefined,
              undefined,
              {},
            );
            const outcome = await toolPromise.then(
              () => "ok",
              (err) => {
                toolError = err;
                return "err";
              },
            );
            await promptGate;
            if (outcome === "err") {
              throw toolError;
            }
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
      orchestration: { capability: "cap-1" },
    });
    let request = null;
    for (let attempt = 0; attempt < 200 && !request; attempt += 1) {
      request = socket.messages.find((m) => m.type === "orchestration_request") || null;
      if (!request) {
        await new Promise((resolve) => setTimeout(resolve, 5));
      }
    }
    assert.ok(request, "no orchestration_request was sent");

    // The transport closes while the tool waits on the backend. The pending
    // RPC must settle immediately, not after the prompt unwinds.
    sidecar._cancelPromptsForSocket(socket);
    let rejection = null;
    for (let attempt = 0; attempt < 100 && !rejection; attempt += 1) {
      if (await settleProbe(toolPromise, 10)) {
        rejection = await toolPromise.then(
          () => null,
          (err) => err,
        );
      }
    }
    assert.ok(rejection, "tool promise did not settle after socket close");
    assert.equal(rejection.code, "orchestration_disconnected");

    // Let the cancellation bookkeeping finish before unwinding the prompt.
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    releasePrompt();
    await queryDone;
    assert.equal(
      socket.messages.some((m) => m.type === "cancelled"),
      true,
    );
    assert.equal(sidecar.activeSessions.get("session-1").orchestration.rpc, null);
    sidecar.cleanup();
  },
);

test(
  "cancelSession disposes the pending RPC before abort resolves",
  { timeout: 4000 },
  async () => {
    const recordingSocket = () => {
      const socket = new EventEmitter();
      socket.destroyed = false;
      socket.messages = [];
      socket.write = (value) => {
        socket.messages.push(JSON.parse(String(value).trim()));
        return true;
      };
      return socket;
    };
    const settleProbe = async (promise, waitMs) => {
      let settled = false;
      const probe = promise.then(
        () => {
          settled = true;
        },
        () => {
          settled = true;
        },
      );
      await Promise.race([
        probe,
        new Promise((resolve) => setTimeout(resolve, waitMs)),
      ]);
      return settled;
    };

    const sidecar = new YinshiSidecar();
    let bridgeTool = null;
    let toolPromise = null;
    let toolError = null;
    let releaseAbort;
    const abortGate = new Promise((resolve) => {
      releaseAbort = resolve;
    });
    let releasePrompt;
    const promptGate = new Promise((resolve) => {
      releasePrompt = resolve;
    });
    sidecar._createPiSession = async (...args) => {
      bridgeTool = args[args.length - 1]
        .find((tool) => tool.name === "thread_bridge_ping");
      return {
        session: {
          subscribe() {
            return () => {};
          },
          async prompt() {
            toolPromise = bridgeTool.execute(
              "call-1",
              {},
              undefined,
              undefined,
              {},
            );
            const outcome = await toolPromise.then(
              () => "ok",
              (err) => {
                toolError = err;
                return "err";
              },
            );
            await promptGate;
            if (outcome === "err") {
              throw toolError;
            }
          },
          abortCompaction() {},
          abortRetry() {},
          abort: () => abortGate,
          dispose() {},
        },
        model: { provider: "test", id: "model" },
        piSessionFile: null,
      };
    };

    const socket = recordingSocket();
    const queryDone = sidecar.processQuery("session-1", socket, "hello", {
      orchestration: { capability: "cap-1" },
    });
    let request = null;
    for (let attempt = 0; attempt < 200 && !request; attempt += 1) {
      request = socket.messages.find((m) => m.type === "orchestration_request") || null;
      if (!request) {
        await new Promise((resolve) => setTimeout(resolve, 5));
      }
    }
    assert.ok(request, "no orchestration_request was sent");

    const cancelPromise = sidecar.cancelSession("session-1");
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));

    // The pending tool must already be rejected while abort is still gated.
    assert.equal(await settleProbe(toolPromise, 10), true);
    const rejection = await toolPromise.then(
      () => null,
      (err) => err,
    );
    assert.equal(rejection.code, "orchestration_disconnected");

    releaseAbort();
    await cancelPromise;
    await new Promise((resolve) => setImmediate(resolve));
    releasePrompt();
    await queryDone;
    assert.equal(
      socket.messages.some((m) => m.type === "cancelled"),
      true,
    );
    sidecar.cleanup();
  },
);

test(
  "query teardown disposes the channel and clears the owning socket",
  { timeout: 4000 },
  async () => {
    const recordingSocket = () => {
      const socket = new EventEmitter();
      socket.destroyed = false;
      socket.messages = [];
      socket.write = (value) => {
        socket.messages.push(JSON.parse(String(value).trim()));
        return true;
      };
      return socket;
    };

    const sidecar = new YinshiSidecar();
    let bridgeTool = null;
    sidecar._createPiSession = async (...args) => {
      bridgeTool = args[args.length - 1]
        .find((tool) => tool.name === "thread_bridge_ping");
      return {
        session: {
          subscribe() {
            return () => {};
          },
          async prompt() {
            await bridgeTool.execute(
              "call-1",
              {},
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
      orchestration: { capability: "cap-1" },
    });
    let request = null;
    for (let attempt = 0; attempt < 200 && !request; attempt += 1) {
      request = socket.messages.find((m) => m.type === "orchestration_request") || null;
      if (!request) {
        await new Promise((resolve) => setTimeout(resolve, 5));
      }
    }
    assert.ok(request, "no orchestration_request was sent");

    sidecar.handleRequest(
      {
        type: "orchestration_response",
        id: "session-1",
        request_id: request.request_id,
        ok: true,
        result: { status: "ok" },
      },
      socket,
    );
    await queryDone;

    const entry = sidecar.activeSessions.get("session-1");
    assert.equal(entry.orchestration.rpc, null);
    assert.equal(entry.orchestration.socket, null);

    // A late response after teardown must be dropped, not crash.
    sidecar.handleRequest(
      {
        type: "orchestration_response",
        id: "session-1",
        request_id: request.request_id,
        ok: true,
        result: {},
      },
      socket,
    );
    sidecar.cleanup();
  },
);

test(
  "session reuse never exposes a stale capability or stale tool set",
  { timeout: 4000 },
  async () => {
    const recordingSocket = () => {
      const socket = new EventEmitter();
      socket.destroyed = false;
      socket.messages = [];
      socket.write = (value) => {
        socket.messages.push(JSON.parse(String(value).trim()));
        return true;
      };
      return socket;
    };

    const sidecar = new YinshiSidecar();
    const capturedToolSets = [];
    sidecar._createPiSession = async (...args) => {
      capturedToolSets.push(args[args.length - 1]);
      return {
        session: {
          subscribe() {
            return () => {};
          },
          async prompt() {},
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
    await sidecar.processQuery("session-1", socket, "one", {
      orchestration: { capability: "cap-1" },
    });
    assert.equal(
      capturedToolSets[0].some((tool) => tool.name === "thread_bridge_ping"),
      true,
    );

    // A plain query on the same session must recreate it without tools.
    await sidecar.processQuery("session-1", recordingSocket(), "two", {});
    assert.equal(capturedToolSets.length, 2);
    assert.equal(
      capturedToolSets[1].some((tool) => tool.name === "thread_bridge_ping"),
      false,
    );
    const plainEntry = sidecar.activeSessions.get("session-1");
    assert.equal(plainEntry.orchestration.rpc, null);
    assert.equal(plainEntry.orchestrationRegistered, false);

    // An orchestration query must recreate it with tools again.
    await sidecar.processQuery("session-1", recordingSocket(), "three", {
      orchestration: { capability: "cap-2" },
    });
    assert.equal(capturedToolSets.length, 3);
    assert.equal(
      capturedToolSets[2].some((tool) => tool.name === "thread_bridge_ping"),
      true,
    );
    const bridgeEntry = sidecar.activeSessions.get("session-1");
    assert.equal(bridgeEntry.orchestration.rpc, null);
    assert.equal(bridgeEntry.orchestrationRegistered, true);
    sidecar.cleanup();
  },
);

test(
  "ordinary tool events stream unfiltered for every tool name",
  { timeout: 4000 },
  async () => {
    const recordingSocket = () => {
      const socket = new EventEmitter();
      socket.destroyed = false;
      socket.messages = [];
      socket.write = (value) => {
        socket.messages.push(JSON.parse(String(value).trim()));
        return true;
      };
      return socket;
    };

    const sidecar = new YinshiSidecar();
    sidecar._createPiSession = async () => {
      const session = {
        handler: null,
        subscribe(onEvent) {
          session.handler = onEvent;
          return () => {
            session.handler = null;
          };
        },
        async prompt() {
          session.handler({
            type: "tool_execution_start",
            toolCallId: "t1",
            toolName: "thread_bridge_ping",
            args: {},
          });
          session.handler({
            type: "tool_execution_end",
            toolCallId: "t1",
            isError: false,
            result: "ping ok",
          });
          session.handler({
            type: "tool_execution_start",
            toolCallId: "t2",
            toolName: "read",
            args: { path: "a.txt" },
          });
          session.handler({
            type: "tool_execution_end",
            toolCallId: "t2",
            isError: false,
            result: "file text",
          });
        },
        abortCompaction() {},
        abortRetry() {},
        async abort() {},
        dispose() {},
      };
      return {
        session,
        model: { provider: "test", id: "model" },
        piSessionFile: null,
      };
    };

    const socket = recordingSocket();
    await sidecar.processQuery("session-1", socket, "hello", {
      orchestration: { capability: "cap-1" },
    });

    const toolUseNames = socket.messages
      .filter((m) => m.type === "message" && m.data?.type === "tool_use")
      .map((m) => m.data.toolName);
    assert.deepEqual(toolUseNames, ["thread_bridge_ping", "read"]);
    const toolResultIds = socket.messages
      .filter((m) => m.type === "tool_result")
      .map((m) => m.tool_use_id);
    assert.deepEqual(toolResultIds, ["t1", "t2"]);
    sidecar.cleanup();
  },
);

test(
  "session release during a live prompt disposes the pending RPC",
  { timeout: 4000 },
  async () => {
    const recordingSocket = () => {
      const socket = new EventEmitter();
      socket.destroyed = false;
      socket.messages = [];
      socket.write = (value) => {
        socket.messages.push(JSON.parse(String(value).trim()));
        return true;
      };
      return socket;
    };

    const sidecar = new YinshiSidecar();
    let bridgeTool = null;
    let toolPromise = null;
    sidecar._createPiSession = async (...args) => {
      bridgeTool = args[args.length - 1]
        .find((tool) => tool.name === "thread_bridge_ping");
      return {
        session: {
          subscribe() {
            return () => {};
          },
          async prompt() {
            toolPromise = bridgeTool.execute(
              "call-1",
              {},
              undefined,
              undefined,
              {},
            );
            await toolPromise;
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
      orchestration: { capability: "cap-1" },
    });
    let request = null;
    for (let attempt = 0; attempt < 200 && !request; attempt += 1) {
      request = socket.messages.find((m) => m.type === "orchestration_request") || null;
      if (!request) {
        await new Promise((resolve) => setTimeout(resolve, 5));
      }
    }
    assert.ok(request, "no orchestration_request was sent");

    assert.equal(sidecar.releasePiSession("session-1"), true);
    let rejection = null;
    for (let attempt = 0; attempt < 100 && !rejection; attempt += 1) {
      const settled = await Promise.race([
        toolPromise.then(
          () => true,
          () => true,
        ),
        new Promise((resolve) => setTimeout(resolve, 10)).then(() => false),
      ]);
      if (settled) {
        rejection = await toolPromise.then(
          () => null,
          (err) => err,
        );
      }
    }
    assert.ok(rejection, "pending tool did not settle after session release");
    assert.equal(rejection.code, "orchestration_disconnected");
    await queryDone;
    assert.equal(
      socket.messages.some((m) => m.type === "error" || m.type === "cancelled"),
      true,
    );
    sidecar.cleanup();
  },
);
