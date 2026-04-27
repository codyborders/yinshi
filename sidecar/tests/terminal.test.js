import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import * as pty from "node-pty";

import { YinshiSidecar, buildTerminalEnvironment } from "../src/sidecar.js";

function createMessageReader(socket) {
  let buffer = "";
  const messages = [];
  const waiters = [];

  function deliver(message) {
    const waiter = waiters.shift();
    if (!waiter) {
      messages.push(message);
      return;
    }
    clearTimeout(waiter.timer);
    waiter.resolve(message);
  }

  function rejectWaiters(error) {
    while (waiters.length > 0) {
      const waiter = waiters.shift();
      clearTimeout(waiter.timer);
      waiter.reject(error);
    }
  }

  function onData(chunk) {
    buffer += chunk.toString("utf8");
    let newline = buffer.indexOf("\n");
    while (newline !== -1) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) {
        deliver(JSON.parse(line));
      }
      newline = buffer.indexOf("\n");
    }
  }

  function onError(error) {
    rejectWaiters(error);
  }

  socket.on("data", onData);
  socket.on("error", onError);

  return {
    next(timeoutMs = 3000) {
      const message = messages.shift();
      if (message) {
        return Promise.resolve(message);
      }
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          const index = waiters.findIndex((waiter) => waiter.resolve === resolve);
          if (index !== -1) {
            waiters.splice(index, 1);
          }
          reject(new Error("timed out waiting for sidecar message"));
        }, timeoutMs);
        waiters.push({ resolve, reject, timer });
      });
    },
    dispose() {
      socket.off("data", onData);
      socket.off("error", onError);
      rejectWaiters(new Error("message reader disposed"));
    },
  };
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

async function nextTerminalReady(reader, options = {}) {
  for (let index = 0; index < 10; index += 1) {
    const message = await reader.next();
    if (message.type === "terminal_ready") {
      return message;
    }
    if (options.rejectTerminalExit && message.type === "terminal_exit") {
      throw new Error("terminal_exit received during restart");
    }
  }
  throw new Error("terminal_ready not received");
}

async function expectTerminalOutput(reader, expectedText, options = {}) {
  for (let index = 0; index < 10; index += 1) {
    const message = await reader.next();
    if (message.type === "terminal_data" && message.data.includes(expectedText)) {
      return;
    }
    if (message.type === "terminal_exit" && options.rejectTerminalExit) {
      throw new Error("terminal_exit received during restart");
    }
    if (message.type === "error") {
      throw new Error(message.error);
    }
  }
  throw new Error(`${expectedText} not received`);
}

test("terminal environment uses an explicit allowlist", () => {
  process.env.YINSHI_TERMINAL_SECRET = "terminal-secret-must-not-leak";
  process.env.NPM_CONFIG_PREFIX = "/home/yinshi/.npm-global";
  try {
    const environment = buildTerminalEnvironment("/data/workspace", "/bin/bash");

    assert.equal(environment.YINSHI_TERMINAL_SECRET, undefined);
    assert.equal(environment.NPM_CONFIG_PREFIX, "/home/yinshi/.npm-global");
    assert.equal(environment.PWD, "/data/workspace");
    assert.equal(environment.SHELL, "/bin/bash");
    assert.equal(environment.TERM, "xterm-256color");
  } finally {
    delete process.env.YINSHI_TERMINAL_SECRET;
    delete process.env.NPM_CONFIG_PREFIX;
  }
});

test("terminal attach starts a PTY and streams output", async (t) => {
  if (!ptyAvailable()) {
    t.skip("node-pty cannot spawn on this host runtime");
    return;
  }
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "yinshi-terminal-test-"));
  const socketPath = path.join(tempDir, "sidecar.sock");
  const terminalId = "a".repeat(32);
  process.env.SIDECAR_SOCKET_PATH = socketPath;
  process.env.YINSHI_TERMINAL_SECRET = "terminal-secret-must-not-leak";
  const sidecar = new YinshiSidecar();
  await sidecar.start();

  const socket = net.createConnection(socketPath);
  const reader = createMessageReader(socket);
  try {
    const init = await reader.next();
    assert.equal(init.type, "init_status");

    send(socket, {
      type: "terminal_attach",
      id: terminalId,
      options: {
        workspaceId: terminalId,
        cwd: tempDir,
        cols: 80,
        rows: 24,
        scrollbackLines: 100,
      },
    });
    const ready = await nextTerminalReady(reader);
    assert.equal(ready.cwd, tempDir);

    send(socket, {
      type: "terminal_input",
      id: terminalId,
      data: "printf YINSHI_TERMINAL_TEST\\n\n",
    });

    await expectTerminalOutput(reader, "YINSHI_TERMINAL_TEST");

    send(socket, {
      type: "terminal_input",
      id: terminalId,
      data: "env | grep YINSHI_TERMINAL_SECRET || printf NO_SECRET\\n\n",
    });

    let sawNoSecret = false;
    for (let index = 0; index < 10; index += 1) {
      const message = await reader.next();
      if (message.type !== "terminal_data") {
        continue;
      }
      assert.equal(message.data.includes("terminal-secret-must-not-leak"), false);
      if (message.data.includes("NO_SECRET")) {
        sawNoSecret = true;
        break;
      }
    }
    assert.equal(sawNoSecret, true);

    send(socket, {
      type: "terminal_restart",
      id: terminalId,
      options: {
        workspaceId: terminalId,
        cwd: tempDir,
        cols: 80,
        rows: 24,
        scrollbackLines: 100,
      },
    });
    await nextTerminalReady(reader, { rejectTerminalExit: true });
    send(socket, {
      type: "terminal_input",
      id: terminalId,
      data: "printf YINSHI_TERMINAL_RESTART\\n\n",
    });
    await expectTerminalOutput(reader, "YINSHI_TERMINAL_RESTART", { rejectTerminalExit: true });
  } finally {
    reader.dispose();
    socket.destroy();
    sidecar.cleanup();
    delete process.env.YINSHI_TERMINAL_SECRET;
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
  const reader = createMessageReader(socket);
  try {
    await reader.next();
    send(socket, {
      type: "terminal_attach",
      id: "bad",
      options: { workspaceId: "bad", cwd: tempDir },
    });
    const error = await reader.next();
    assert.equal(error.type, "error");
    assert.match(error.error, /workspaceId/);
  } finally {
    reader.dispose();
    socket.destroy();
    sidecar.cleanup();
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
});
