// Regression tests for the sidecar Unix socket lifecycle and ownership.
//
// A YinshiSidecar instance owns only the filesystem socket entry created by
// its own successful start(). cleanup() on an unstarted or non-owning
// instance must never delete the path, so test instances can no longer
// destroy a live production listener (the SIDECAR_SOCKET_PATH incident).
//
// Ownership is tracked by device+inode: cleanup removes the entry only when
// the path still holds exactly the entry this instance bound. start()
// recovers stale entries left by crashed processes, refuses to disturb a
// live listener, and records ownership after a successful bind.

import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

import { YinshiSidecar } from "../src/sidecar.js";

function lstatIdentity(socketPath) {
  const stats = fs.lstatSync(socketPath);
  return { dev: stats.dev, ino: stats.ino };
}

// Identity of the entry at socketPath, or null when nothing is there. Keeps
// assertions about presence/absence as clean AssertionErrors instead of raw
// filesystem crashes.
function tryIdentity(socketPath) {
  try {
    return lstatIdentity(socketPath);
  } catch {
    return null;
  }
}

// Leaves a stale (unowned, unreachable) socket entry at socketPath by
// SIGKILLing a child that just bound it: exactly what a crashed sidecar
// leaves behind. server.close() would unlink the entry, so a child process
// is the only way to stage this scenario.
function createStaleSocketEntry(socketPath) {
  spawnSync(
    process.execPath,
    [
      "-e",
      `const net = require("node:net");
       net.createServer().listen(process.argv[1], () => {
         process.kill(process.pid, "SIGKILL");
       });`,
      socketPath,
    ],
    { timeout: 10_000 },
  );
  assert.ok(tryIdentity(socketPath), "failed to create a stale socket entry");
}

// Connects to the socket and resolves once the live sidecar answers with its
// init_status greeting. Rejects if the path cannot be connected to at all.
function connectAndWaitInit(socketPath, timeoutMs = 3000) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath);
    let buffer = "";
    const timer = setTimeout(() => {
      socket.destroy();
      reject(new Error("timed out waiting for init_status from sidecar"));
    }, timeoutMs);
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      let newline = buffer.indexOf("\n");
      while (newline !== -1) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) {
          continue;
        }
        const message = JSON.parse(line);
        if (message.type === "init_status") {
          clearTimeout(timer);
          resolve(socket);
          return;
        }
      }
    });
    socket.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
  });
}

function connectOnce(socketPath) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath);
    socket.once("connect", () => {
      socket.destroy();
      resolve();
    });
    socket.once("error", reject);
  });
}

test("cleanup on an unstarted sidecar never deletes a live listener's socket", async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "yinshi-socket-lifecycle-"));
  const socketPath = path.join(tempDir, "sidecar.sock");
  process.env.SIDECAR_SOCKET_PATH = socketPath;
  const owner = new YinshiSidecar();
  try {
    await owner.start();
    const liveIdentity = lstatIdentity(socketPath);

    // The production incident: an instance that never started (e.g. created
    // by a test suite) inherits the production path, and its cleanup()
    // unlinked the live listener's socket entry out from under it.
    const unstarted = new YinshiSidecar();
    unstarted.cleanup();

    assert.deepEqual(tryIdentity(socketPath), liveIdentity);
    const client = await connectAndWaitInit(socketPath);
    client.destroy();
  } finally {
    owner.cleanup();
    delete process.env.SIDECAR_SOCKET_PATH;
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
});

test("start recovers a stale socket entry left by a crashed process", async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "yinshi-socket-lifecycle-"));
  const socketPath = path.join(tempDir, "sidecar.sock");
  process.env.SIDECAR_SOCKET_PATH = socketPath;
  createStaleSocketEntry(socketPath);

  const sidecar = new YinshiSidecar();
  let startError = null;
  try {
    await sidecar.start();
  } catch (error) {
    startError = error;
  }
  try {
    // The stale entry is unreachable, so start() removes it and binds
    // successfully instead of failing with EADDRINUSE.
    assert.equal(startError, null, `start() failed to recover stale socket: ${startError?.message}`);
    assert.ok(tryIdentity(socketPath), "socket entry missing after start");
    const client = await connectAndWaitInit(socketPath);
    client.destroy();
  } finally {
    sidecar.cleanup();
    delete process.env.SIDECAR_SOCKET_PATH;
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
});

test("start against an active listener fails clearly and preserves it", async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "yinshi-socket-lifecycle-"));
  const socketPath = path.join(tempDir, "sidecar.sock");
  process.env.SIDECAR_SOCKET_PATH = socketPath;
  const owner = new YinshiSidecar();
  try {
    await owner.start();
    const liveIdentity = lstatIdentity(socketPath);

    const rival = new YinshiSidecar();
    let collisionError = null;
    try {
      await rival.start();
    } catch (error) {
      collisionError = error;
    }

    // The failure must be clear and actionable, not a mystery kernel error.
    assert.ok(collisionError, "start() must fail against an active listener");
    assert.equal(collisionError.code, "EADDRINUSE");
    assert.match(collisionError.message, /active listener/);
    assert.ok(collisionError.message.includes(socketPath));

    // The live listener and its entry are completely untouched.
    assert.deepEqual(tryIdentity(socketPath), liveIdentity);
    const client = await connectAndWaitInit(socketPath);
    client.destroy();
  } finally {
    owner.cleanup();
    delete process.env.SIDECAR_SOCKET_PATH;
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
});

test("owner cleanup removes its own socket entry exactly once", async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "yinshi-socket-lifecycle-"));
  const socketPath = path.join(tempDir, "sidecar.sock");
  process.env.SIDECAR_SOCKET_PATH = socketPath;
  const sidecar = new YinshiSidecar();
  try {
    await sidecar.start();
    assert.ok(tryIdentity(socketPath), "socket entry missing while running");

    sidecar.cleanup();
    assert.equal(tryIdentity(socketPath), null, "owner cleanup must remove its own entry");

    // A repeated cleanup is idempotent and must not throw or resurrect
    // anything at the path.
    sidecar.cleanup();
    assert.equal(tryIdentity(socketPath), null);
  } finally {
    sidecar.cleanup();
    delete process.env.SIDECAR_SOCKET_PATH;
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
});

test("repeated owner cleanup preserves a replacement listener", async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "yinshi-socket-lifecycle-"));
  const socketPath = path.join(tempDir, "sidecar.sock");
  process.env.SIDECAR_SOCKET_PATH = socketPath;
  const sidecar = new YinshiSidecar();
  const replacement = net.createServer((socket) => socket.end());
  let replacementStarted = false;
  try {
    await sidecar.start();
    const ownedIdentity = lstatIdentity(socketPath);

    // Simulate a startup/cleanup race: another listener replaces the path
    // while the owner is still live. Every cleanup must preserve it because
    // Node removes the current pathname whenever the old server is closed.
    fs.unlinkSync(socketPath);
    await new Promise((resolve, reject) => {
      replacement.once("error", reject);
      replacement.listen(socketPath, resolve);
    });
    replacementStarted = true;
    const replacementIdentity = lstatIdentity(socketPath);
    assert.notDeepEqual(replacementIdentity, ownedIdentity);

    sidecar.cleanup();
    sidecar.cleanup();

    assert.deepEqual(lstatIdentity(socketPath), replacementIdentity);
    await connectOnce(socketPath);
  } finally {
    sidecar.cleanup();
    if (replacementStarted) {
      await new Promise((resolve) => replacement.close(resolve));
    }
    delete process.env.SIDECAR_SOCKET_PATH;
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
});
