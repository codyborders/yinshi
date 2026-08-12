import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import { YinshiSidecar } from "../src/sidecar.js";

function recordingSocket(onWrite = () => {}) {
  const socket = new EventEmitter();
  socket.destroyed = false;
  socket.messages = [];
  socket.write = (value) => {
    const message = JSON.parse(String(value).trim());
    socket.messages.push(message);
    onWrite(message);
    return true;
  };
  return socket;
}

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

test("warmup acknowledges success only after storing the session", async () => {
  const sidecar = new YinshiSidecar();
  const socket = recordingSocket((message) => {
    if (message.type === "warmup_status") {
      assert.equal(sidecar.activeSessions.has("session-1"), true);
    }
  });
  sidecar._createPiSession = async () => sessionResult();

  await sidecar.warmupSession("session-1", socket, {});

  assert.deepEqual(socket.messages, [
    { id: "session-1", type: "warmup_status", success: true },
  ]);
  sidecar.cleanup();
});

test("warmup acknowledges an already active session", async () => {
  const sidecar = new YinshiSidecar();
  const socket = recordingSocket();
  const existing = sessionResult();
  sidecar.activeSessions.set("session-1", {
    piSession: existing.session,
    lastActivityMs: Date.now(),
  });

  await sidecar.warmupSession("session-1", socket, {});

  assert.deepEqual(socket.messages, [
    { id: "session-1", type: "warmup_status", success: true },
  ]);
  assert.equal(sidecar.activeSessions.get("session-1").piSession, existing.session);
  sidecar.cleanup();
});

test("concurrent warmups for one ID create and store one session", async () => {
  const sidecar = new YinshiSidecar();
  const socket = recordingSocket();
  let creationCalls = 0;
  const result = sessionResult();
  sidecar._createPiSession = async () => {
    creationCalls += 1;
    await new Promise((resolve) => setImmediate(resolve));
    return result;
  };

  await Promise.all([
    sidecar.warmupSession("session-1", socket, {}),
    sidecar.warmupSession("session-1", socket, {}),
  ]);

  assert.equal(creationCalls, 1);
  assert.equal(sidecar.activeSessions.size, 1);
  assert.equal(sidecar.activeSessions.get("session-1").piSession, result.session);
  assert.equal(
    socket.messages.filter((message) => message.type === "warmup_status").length,
    2,
  );
  sidecar.cleanup();
});

test("capacity admission counts active and pending session IDs", async () => {
  const sidecar = new YinshiSidecar();
  const socket = recordingSocket();
  const active = sessionResult();
  sidecar.activeSessions.set("active", {
    piSession: active.session,
    lastActivityMs: 1,
  });
  sidecar._trackPromptSession(socket, "active");
  const creationResolvers = [];
  sidecar._createPiSession = async () => new Promise((resolve) => {
    creationResolvers.push(() => resolve(sessionResult()));
  });

  const warmups = Array.from({ length: 16 }, (_, index) => (
    sidecar.warmupSession(`session-${index}`, socket, {})
  ));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(creationResolvers.length, 15);
  for (const resolveCreation of creationResolvers) {
    resolveCreation();
  }
  await Promise.all(warmups);

  assert.equal(sidecar.activeSessions.size, 16);
  assert.equal(sidecar.activeSessions.has("active"), true);
  assert.equal(
    socket.messages.filter((message) => message.success === false).length,
    1,
  );
  sidecar.cleanup();
});

test("warmup failure returns a fixed message without provider secrets", async () => {
  const sidecar = new YinshiSidecar();
  const socket = recordingSocket();
  sidecar._createPiSession = async () => {
    throw new Error("provider rejected secret-token-123");
  };

  await sidecar.warmupSession("session-1", socket, {
    providerAuth: { secret: "secret-token-123" },
  });

  assert.deepEqual(socket.messages, [
    {
      id: "session-1",
      type: "warmup_status",
      success: false,
      error: "Failed to warm up session",
    },
  ]);
  assert.equal(JSON.stringify(socket.messages).includes("secret-token-123"), false);
  assert.equal(sidecar.pendingPiSessionCreations.size, 0);
  sidecar.cleanup();
});

test("concurrent queries for one ID create and store one session", async () => {
  const sidecar = new YinshiSidecar();
  const socket = recordingSocket();
  let creationCalls = 0;
  let promptCalls = 0;
  const result = sessionResult();
  result.session.prompt = async () => {
    promptCalls += 1;
  };
  sidecar._createPiSession = async () => {
    creationCalls += 1;
    await new Promise((resolve) => setImmediate(resolve));
    return result;
  };

  await Promise.all([
    sidecar.processQuery("session-1", socket, "first", {}),
    sidecar.processQuery("session-1", socket, "second", {}),
  ]);

  assert.equal(creationCalls, 1);
  assert.equal(promptCalls, 2);
  assert.equal(sidecar.activeSessions.size, 1);
  assert.equal(sidecar.activeSessions.get("session-1").piSession, result.session);
  assert.equal(sidecar.pendingPiSessionCreations.size, 0);
  assert.equal(sidecar.activePromptSessionsBySocket.size, 0);
  sidecar.cleanup();
});

test("socket close during session creation prevents prompt execution", async () => {
  const sidecar = new YinshiSidecar();
  const socket = recordingSocket();
  let resolveCreation;
  let promptCalls = 0;
  const result = sessionResult();
  result.session.prompt = async () => {
    promptCalls += 1;
  };
  sidecar._createPiSession = async () => new Promise((resolve) => {
    resolveCreation = () => resolve(result);
  });
  sidecar.handleConnection(socket);

  const queryPromise = sidecar.processQuery("session-1", socket, "hello", {});
  assert.equal(
    sidecar.activePromptSessionsBySocket.get(socket)?.has("session-1"),
    true,
  );
  await new Promise((resolve) => setImmediate(resolve));
  socket.emit("close");
  resolveCreation();
  await queryPromise;

  assert.equal(promptCalls, 0);
  assert.equal(sidecar.activeSessions.has("session-1"), true);
  assert.equal(sidecar.activePromptSessionsBySocket.size, 0);
  sidecar.cleanup();
});

test("socket error and close cancel an active prompt exactly once", async () => {
  const sidecar = new YinshiSidecar();
  const socket = recordingSocket();
  let abortCalls = 0;
  let resolvePrompt;
  let markPromptStarted;
  const promptStarted = new Promise((resolve) => {
    markPromptStarted = resolve;
  });
  const result = sessionResult();
  result.session.prompt = async () => {
    markPromptStarted();
    await new Promise((resolve) => {
      resolvePrompt = resolve;
    });
  };
  result.session.abort = async () => {
    abortCalls += 1;
    resolvePrompt();
  };
  sidecar._createPiSession = async () => result;
  sidecar.handleConnection(socket);

  const queryPromise = sidecar.processQuery("session-1", socket, "hello", {});
  await promptStarted;
  socket.emit("error", new Error("client disconnected"));
  socket.emit("close");
  await new Promise((resolve) => setImmediate(resolve));
  resolvePrompt();
  await queryPromise;

  assert.equal(abortCalls, 1);
  assert.equal(sidecar.activePromptSessionsBySocket.size, 0);
  sidecar.cleanup();
});

test("disconnecting prompt remains reusable after cancellation completes", async () => {
  const sidecar = new YinshiSidecar();
  const socket = recordingSocket();
  let creationCalls = 0;
  let promptCalls = 0;
  let resolveFirstPrompt;
  let markPromptStarted;
  let allowAbort;
  const promptStarted = new Promise((resolve) => {
    markPromptStarted = resolve;
  });
  const abortAllowed = new Promise((resolve) => {
    allowAbort = resolve;
  });
  const result = sessionResult();
  result.session.prompt = async () => {
    promptCalls += 1;
    if (promptCalls !== 1) {
      return;
    }
    markPromptStarted();
    await new Promise((resolve) => {
      resolveFirstPrompt = resolve;
    });
  };
  result.session.abort = async () => {
    await abortAllowed;
    resolveFirstPrompt();
  };
  sidecar._createPiSession = async () => {
    creationCalls += 1;
    return result;
  };
  sidecar.handleConnection(socket);

  const firstQuery = sidecar.processQuery("session-1", socket, "hello", {});
  await promptStarted;
  sidecar.activeSessions.get("session-1").lastActivityMs = 1;
  socket.emit("error", new Error("client disconnected"));
  sidecar.handleRequest({ type: "ping" }, socket, 40 * 60 * 1000);
  allowAbort();
  await firstQuery;

  await sidecar.processQuery("session-1", recordingSocket(), "again", {});

  assert.equal(creationCalls, 1);
  sidecar.cleanup();
});

test("socket close does not cancel an idle warmed session", async () => {
  const sidecar = new YinshiSidecar();
  const socket = recordingSocket();
  let abortCalls = 0;
  const result = sessionResult();
  result.session.abort = async () => {
    abortCalls += 1;
  };
  sidecar._createPiSession = async () => result;
  sidecar.handleConnection(socket);
  await sidecar.warmupSession("session-1", socket, {});

  socket.emit("close");
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(abortCalls, 0);
  assert.equal(sidecar.activeSessions.has("session-1"), true);
  sidecar.cleanup();
});

test("socket close does not cancel a completed prompt", async () => {
  const sidecar = new YinshiSidecar();
  const socket = recordingSocket();
  let abortCalls = 0;
  const result = sessionResult();
  result.session.abort = async () => {
    abortCalls += 1;
  };
  sidecar._createPiSession = async () => result;
  sidecar.handleConnection(socket);

  await sidecar.processQuery("session-1", socket, "hello", {});
  socket.emit("close");
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(abortCalls, 0);
  assert.equal(sidecar.activePromptSessionsBySocket.size, 0);
  sidecar.cleanup();
});
