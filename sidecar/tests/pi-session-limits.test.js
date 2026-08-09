// Covers pi session retention over the sidecar wire protocol, without model calls.
import assert from "node:assert/strict";
import test from "node:test";
import { YinshiSidecar } from "../src/sidecar.js";

function writableMessages() {
  const messages = [];
  return {
    messages,
    socket: {
      writable: true,
      write(value) {
        messages.push(JSON.parse(String(value).trim()));
        return true;
      },
    },
  };
}

function fakeSession(record, name) {
  return {
    piSession: {
      dispose() {
        record.push(`dispose:${name}`);
      },
      abortCompaction() {},
      abortRetry() {},
      async abort() {},
    },
    unsubscribe() {
      record.push(`unsubscribe:${name}`);
    },
    cancelRequested: false,
    lastActivityMs: Date.now(),
  };
}

test("an incoming request disposes pi sessions that have been idle too long", () => {
  const sidecar = new YinshiSidecar();
  const record = [];
  const { socket } = writableMessages();
  sidecar.activeSessions.set("idle", {
    ...fakeSession(record, "idle"),
    lastActivityMs: 1,
  });
  sidecar.activeSessions.set("recent", {
    ...fakeSession(record, "recent"),
    lastActivityMs: 40 * 60 * 1000,
  });

  sidecar.handleRequest({ type: "ping" }, socket, 40 * 60 * 1000);

  assert.equal(sidecar.activeSessions.has("idle"), false);
  assert.equal(sidecar.activeSessions.has("recent"), true);
  assert.deepEqual(record, ["unsubscribe:idle", "dispose:idle"]);
});

test("a pi session with no recorded activity is kept and stamped, not destroyed", () => {
  const sidecar = new YinshiSidecar();
  const record = [];
  const { socket } = writableMessages();
  const entry = fakeSession(record, "fresh");
  delete entry.lastActivityMs;
  sidecar.activeSessions.set("fresh", entry);

  sidecar.handleRequest({ type: "ping" }, socket, 1_000);

  assert.equal(sidecar.activeSessions.has("fresh"), true);
  assert.equal(sidecar.activeSessions.get("fresh").lastActivityMs, 1_000);
  assert.deepEqual(record, []);
});

test("a request that names a live pi session keeps that session alive", () => {
  const sidecar = new YinshiSidecar();
  const record = [];
  const { socket } = writableMessages();
  sidecar.activeSessions.set("busy", {
    ...fakeSession(record, "busy"),
    lastActivityMs: 0,
  });

  sidecar.handleRequest({ type: "cancel", id: "busy" }, socket, 10 * 60 * 1000);
  sidecar.handleRequest({ type: "ping" }, socket, 35 * 60 * 1000);

  assert.equal(sidecar.activeSessions.has("busy"), true);
  assert.deepEqual(record, []);
});

test("the process keeps at most sixteen pi sessions, dropping the least recent", () => {
  const sidecar = new YinshiSidecar();
  const record = [];
  const { socket } = writableMessages();
  for (let index = 0; index < 20; index += 1) {
    sidecar.activeSessions.set(`session-${index}`, {
      ...fakeSession(record, `session-${index}`),
      lastActivityMs: 1_000 + index,
    });
  }

  sidecar.handleRequest({ type: "ping" }, socket, 1_100);

  assert.equal(sidecar.activeSessions.size, 16);
  assert.equal(sidecar.activeSessions.has("session-0"), false);
  assert.equal(sidecar.activeSessions.has("session-3"), false);
  assert.equal(sidecar.activeSessions.has("session-4"), true);
  assert.equal(sidecar.activeSessions.has("session-19"), true);
  assert.deepEqual(record.slice(0, 2), [
    "unsubscribe:session-0",
    "dispose:session-0",
  ]);
  assert.equal(record.length, 8);
});

test("a session_release request disposes that pi session and confirms the release", () => {
  const sidecar = new YinshiSidecar();
  const record = [];
  const { messages, socket } = writableMessages();
  sidecar.activeSessions.set("live", fakeSession(record, "live"));

  sidecar.handleRequest({ type: "session_release", id: "live" }, socket);

  assert.equal(sidecar.activeSessions.has("live"), false);
  assert.deepEqual(record, ["unsubscribe:live", "dispose:live"]);
  assert.deepEqual(messages, [
    { id: "live", type: "session_released", released: true },
  ]);
});
