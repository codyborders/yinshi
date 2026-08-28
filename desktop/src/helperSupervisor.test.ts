// Covers Electron helper supervision through a private readiness pipe and bounded shutdown.

import { describe, expect, it } from "vitest";

import { startManagedHelper, type ManagedHelper } from "./helperSupervisor.js";

const READY_NONCE = "abcdefghijklmnopqrstuvwxyz_1234567890-ABCD";

function delay(milliseconds: number): Promise<string> {
  return new Promise((resolve) => setTimeout(() => resolve("unbounded"), milliseconds));
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "non-error rejection";
}

function fakeHelperScript(): string {
  return [
    "const fs = require('node:fs');",
    `fs.writeSync(3, JSON.stringify({type:'ready',protocolVersion:1,port:43123,instanceNonce:'${READY_NONCE}'}) + '\\n');`,
    "process.on('SIGTERM', () => process.exit(0));",
    "setInterval(() => {}, 1000);",
  ].join("");
}

describe("startManagedHelper", () => {
  it("reads readiness from fd 3 and stops the live child cleanly", async () => {
    let helper: ManagedHelper | undefined;
    try {
      helper = await startManagedHelper({
        command: process.execPath,
        arguments: ["--eval", fakeHelperScript()],
        environment: {},
        readinessTimeoutMs: 2_000,
        shutdownTimeoutMs: 2_000,
      });

      expect(helper.ready).toEqual({ port: 43123, instanceNonce: READY_NONCE });
      expect(helper.processId).toBeGreaterThan(0);
      expect(helper.running).toBe(true);
    } finally {
      await helper?.stop();
    }

    expect(helper?.running).toBe(false);
  });

  it("rejects startup when the helper cannot spawn", async () => {
    await expect(
      startManagedHelper({
        command: "/nonexistent/yinshi-helper-binary",
        arguments: [],
        environment: {},
        readinessTimeoutMs: 2_000,
        shutdownTimeoutMs: 2_000,
      }),
    ).rejects.toThrow();

    await expect(
      startManagedHelper({
        command: process.execPath,
        arguments: ["--eval", fakeHelperScript()],
        environment: {},
        workingDirectory: "/nonexistent/yinshi-working-directory",
        readinessTimeoutMs: 2_000,
        shutdownTimeoutMs: 2_000,
      }),
    ).rejects.toThrow();
  });

  it.each([true, false])(
    "rejects oversized %s readiness input before parsing",
    async (terminatedWithNewline) => {
      const payload = "x".repeat(4097) + (terminatedWithNewline ? "\\n" : "");
      await expect(
        startManagedHelper({
          command: process.execPath,
          arguments: [
            "--eval",
            `require('node:fs').writeSync(3, ${JSON.stringify(payload)});`,
          ],
          environment: {},
          readinessTimeoutMs: 2_000,
          shutdownTimeoutMs: 2_000,
        }),
      ).rejects.toThrow("helper readiness line exceeds 4096 bytes");
    },
  );

  it("times out and stops a helper that never signals readiness", async () => {
    const startPromise = startManagedHelper({
      command: process.execPath,
      arguments: ["--eval", "setTimeout(() => process.exit(0), 800);"],
      environment: {},
      readinessTimeoutMs: 50,
      shutdownTimeoutMs: 50,
    });
    const outcome = await Promise.race([
      startPromise.then(() => "started", errorMessage),
      delay(500),
    ]);
    await startPromise.catch(() => undefined);

    expect(outcome).toBe("helper readiness timed out");
  });

  it("escalates shutdown when a helper ignores SIGTERM", async () => {
    const script = [
      fakeHelperScript(),
      "process.removeAllListeners('SIGTERM');",
      "process.on('SIGTERM', () => {});",
      "setTimeout(() => process.exit(0), 1000);",
    ].join("");
    const helper = await startManagedHelper({
      command: process.execPath,
      arguments: ["--eval", script],
      environment: {},
      readinessTimeoutMs: 500,
      shutdownTimeoutMs: 50,
    });

    const outcome = await Promise.race([
      helper.stop().then(() => "stopped", errorMessage),
      delay(500),
    ]);
    if (outcome === "unbounded") {
      await helper.stop();
    }

    expect(outcome).toBe("stopped");
    expect(helper.running).toBe(false);
  });
});
