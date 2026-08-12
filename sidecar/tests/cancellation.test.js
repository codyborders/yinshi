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

function session() {
  return {
    abortCompaction() {},
    abortRetry() {},
    async abort() {},
    dispose() {},
  };
}

test("failed cancellation reports once without unhandled rejection and permits retry", async () => {
  const sidecar = new YinshiSidecar();
  const socket = recordingSocket();
  const piSession = session();
  let abortAttempts = 0;
  piSession.abort = async () => {
    abortAttempts += 1;
    if (abortAttempts === 1) {
      throw new Error("private abort details");
    }
  };
  sidecar.activeSessions.set("session-1", {
    piSession,
    cancelRequested: false,
    lastActivityMs: Date.now(),
  });
  const unhandledRejections = [];
  const recordUnhandledRejection = (reason) => {
    unhandledRejections.push(reason);
  };
  process.on("unhandledRejection", recordUnhandledRejection);

  try {
    sidecar.handleRequest({ type: "cancel", id: "session-1" }, socket);
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));

    assert.deepEqual(socket.messages, [
      {
        id: "session-1",
        type: "cancel_status",
        success: false,
        error: "Failed to cancel session",
      },
    ]);
    assert.deepEqual(unhandledRejections, []);
    assert.equal(sidecar.activeSessions.get("session-1").cancelRequested, false);

    sidecar.handleRequest({ type: "cancel", id: "session-1" }, socket);
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(abortAttempts, 2);
    assert.deepEqual(socket.messages, [
      {
        id: "session-1",
        type: "cancel_status",
        success: false,
        error: "Failed to cancel session",
      },
      { id: "session-1", type: "cancel_status", success: true },
    ]);
    assert.deepEqual(unhandledRejections, []);
  } finally {
    process.off("unhandledRejection", recordUnhandledRejection);
    sidecar.cleanup();
  }
});

test("synchronous cancellation setup failures report once and permit retry", async (t) => {
  for (const failingMethod of ["abortCompaction", "abortRetry"]) {
    await t.test(failingMethod, async () => {
      const sidecar = new YinshiSidecar();
      const socket = recordingSocket();
      const piSession = session();
      let attempts = 0;
      piSession[failingMethod] = () => {
        attempts += 1;
        if (attempts === 1) {
          throw new Error("private setup details");
        }
      };
      sidecar.activeSessions.set("session-1", {
        piSession,
        cancelRequested: false,
        lastActivityMs: Date.now(),
      });

      try {
        await sidecar.handleCancelRequest("session-1", socket);

        assert.deepEqual(socket.messages, [
          {
            id: "session-1",
            type: "cancel_status",
            success: false,
            error: "Failed to cancel session",
          },
        ]);
        assert.equal(sidecar.activeSessions.get("session-1").cancelRequested, false);

        await sidecar.handleCancelRequest("session-1", socket);

        assert.equal(attempts, 2);
        assert.deepEqual(socket.messages, [
          {
            id: "session-1",
            type: "cancel_status",
            success: false,
            error: "Failed to cancel session",
          },
          { id: "session-1", type: "cancel_status", success: true },
        ]);
      } finally {
        sidecar.cleanup();
      }
    });
  }
});

test("cancel acknowledgement waits for abort and reports one fixed success", async () => {
  const sidecar = new YinshiSidecar();
  const socket = recordingSocket();
  let allowAbort;
  const abortAllowed = new Promise((resolve) => {
    allowAbort = resolve;
  });
  const piSession = session();
  piSession.abort = async () => abortAllowed;
  sidecar.activeSessions.set("session-1", {
    piSession,
    cancelRequested: false,
    lastActivityMs: Date.now(),
  });

  sidecar.handleRequest({ type: "cancel", id: "session-1" }, socket);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(socket.messages, []);

  allowAbort();
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(socket.messages, [
    { id: "session-1", type: "cancel_status", success: true },
  ]);
  assert.equal(sidecar.activeSessions.get("session-1").cancelRequested, true);
  sidecar.cleanup();
});
