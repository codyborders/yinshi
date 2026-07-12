// Verifies the desktop supervisor receives one deterministic first-line readiness signal.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import test from "node:test";

const projectDirectory = path.resolve(import.meta.dirname, "..");

test("sidecar emits socket readiness as its first stdout line", async () => {
  const directoryPath = await mkdtemp(path.join(os.tmpdir(), "yinshi-sidecar-ready-"));
  const socketPath = path.join(directoryPath, "sidecar.sock");
  const child = spawn(process.execPath, [path.join(projectDirectory, "src", "index.js")], {
    cwd: projectDirectory,
    env: {
      HOME: os.homedir(),
      PATH: process.env.PATH ?? "/usr/bin:/bin",
      SIDECAR_LOAD_DOTENV: "0",
      SIDECAR_SOCKET_PATH: socketPath,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  try {
    assert.ok(child.stdout);
    const lines = readline.createInterface({ input: child.stdout });
    let timeout;
    const firstLine = await Promise.race([
      new Promise((resolve) => lines.once("line", resolve)),
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error("sidecar readiness timed out")), 5_000);
      }),
    ]).finally(() => clearTimeout(timeout));
    assert.equal(firstLine, `SOCKET_PATH=${socketPath}`);
    lines.close();
  } finally {
    child.kill("SIGTERM");
    await new Promise((resolve) => child.once("exit", resolve));
    await rm(directoryPath, { recursive: true, force: true });
  }
});
