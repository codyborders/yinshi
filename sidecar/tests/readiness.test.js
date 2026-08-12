// Verifies the desktop supervisor receives one deterministic first-line readiness signal.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import test from "node:test";

const projectDirectory = path.resolve(import.meta.dirname, "..");

test("sidecar library loads with the pinned SDK", async () => {
  let importError = null;
  try {
    await import("../src/sidecar.js");
  } catch (error) {
    importError = error;
  }

  assert.equal(importError, null, importError?.message);
});

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
    assert.ok(child.stderr);
    const lines = readline.createInterface({ input: child.stdout });
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    let timeout;
    const firstLine = await Promise.race([
      new Promise((resolve) => lines.once("line", resolve)),
      new Promise((_, reject) => child.once("exit", (code, signal) => {
        reject(new assert.AssertionError({
          message: `sidecar exited before readiness: code=${code} signal=${signal}`,
          actual: { code, signal },
          expected: { code: null, signal: null },
        }));
      })),
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error("sidecar readiness timed out")), 5_000);
      }),
    ]).finally(() => clearTimeout(timeout));
    assert.equal(firstLine, `SOCKET_PATH=${socketPath}`);
    lines.close();
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGTERM");
      await new Promise((resolve) => child.once("exit", resolve));
    }
    await rm(directoryPath, { recursive: true, force: true });
  }
});
