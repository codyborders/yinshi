import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import * as pty from "node-pty";

import { YinshiSidecar } from "../src/sidecar.js";

function nextMessage(socket, timeoutMs = 3000) {
  return new Promise((resolve, reject) => {
    let buffer = "";
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error("timed out waiting for sidecar message"));
    }, timeoutMs);

    function cleanup() {
      clearTimeout(timer);
      socket.off("data", onData);
      socket.off("error", onError);
    }

    function onError(error) {
      cleanup();
      reject(error);
    }

    function onData(chunk) {
      buffer += chunk.toString("utf8");
      const newline = buffer.indexOf("\n");
      if (newline === -1) {
        return;
      }
      const line = buffer.slice(0, newline).trim();
      cleanup();
      resolve(JSON.parse(line));
    }

    socket.on("data", onData);
    socket.on("error", onError);
  });
}

function send(socket, message) {
  socket.write(`${JSON.stringify(message)}\n`);
}

function ptyAvailable() {
  try {
    const terminal = pty.spawn(process.execPath, ["--version"], {
      cwd: process.cwd(),
      cols: 80,
      rows: 24,
      env: process.env,
    });
    terminal.kill();
    return true;
  } catch {
    return false;
  }
}

test("terminal attach starts a PTY and streams output", async (t) => {
  if (!ptyAvailable()) {
    t.skip("node-pty cannot spawn on this host runtime");
    return;
  }
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "yinshi-terminal-test-"));
  const socketPath = path.join(tempDir, "sidecar.sock");
  process.env.SIDECAR_SOCKET_PATH = socketPath;
  const sidecar = new YinshiSidecar();
  await sidecar.start();

  const socket = net.createConnection(socketPath);
  try {
    const init = await nextMessage(socket);
    assert.equal(init.type, "init_status");

    send(socket, {
      type: "terminal_attach",
      id: "a".repeat(32),
      options: {
        workspaceId: "a".repeat(32),
        cwd: tempDir,
        cols: 80,
        rows: 24,
        scrollbackLines: 100,
      },
    });
    const ready = await nextMessage(socket);
    assert.equal(ready.type, "terminal_ready");
    assert.equal(ready.cwd, tempDir);

    send(socket, {
      type: "terminal_input",
      id: "a".repeat(32),
      data: "printf YINSHI_TERMINAL_TEST\\n\n",
    });

    let sawOutput = false;
    for (let index = 0; index < 10; index += 1) {
      const message = await nextMessage(socket);
      if (message.type === "terminal_data" && message.data.includes("YINSHI_TERMINAL_TEST")) {
        sawOutput = true;
        break;
      }
    }
    assert.equal(sawOutput, true);
  } finally {
    socket.destroy();
    sidecar.cleanup();
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
});

test("terminal attach rejects invalid workspace ids", async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "yinshi-terminal-test-"));
  const socketPath = path.join(tempDir, "sidecar.sock");
  process.env.SIDECAR_SOCKET_PATH = socketPath;
  const sidecar = new YinshiSidecar();
  await sidecar.start();

  const socket = net.createConnection(socketPath);
  try {
    await nextMessage(socket);
    send(socket, {
      type: "terminal_attach",
      id: "bad",
      options: { workspaceId: "bad", cwd: tempDir },
    });
    const error = await nextMessage(socket);
    assert.equal(error.type, "error");
    assert.match(error.error, /workspaceId/);
  } finally {
    socket.destroy();
    sidecar.cleanup();
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
});
