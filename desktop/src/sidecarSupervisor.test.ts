import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, expect, it } from "vitest";

import { startSidecar } from "./sidecarSupervisor.js";

const temporaryDirectories: string[] = [];

const sidecarScript = `
const fs = require("node:fs");
const net = require("node:net");
const socketPath = process.env.SIDECAR_SOCKET_PATH;
const server = net.createServer();
server.listen(socketPath, () => {
  fs.chmodSync(socketPath, 0o600);
  process.stdout.write("SOCKET_PATH=" + socketPath + "\\n");
});
process.on("SIGTERM", () => server.close(() => process.exit(0)));
`;

afterEach(async () => {
  await Promise.all(
    temporaryDirectories.splice(0).map((directoryPath) =>
      rm(directoryPath, { recursive: true, force: true }),
    ),
  );
});

it("waits for an owner-only sidecar socket and stops the child deterministically", async () => {
  const directoryPath = await mkdtemp(path.join(os.tmpdir(), "yinshi-sidecar-"));
  temporaryDirectories.push(directoryPath);
  const socketPath = path.join(directoryPath, "sidecar.sock");

  const sidecar = await startSidecar({
    command: process.execPath,
    args: ["-e", sidecarScript],
    environment: { SIDECAR_SOCKET_PATH: socketPath },
    socketPath,
    startupTimeoutMs: 5_000,
    shutdownTimeoutMs: 2_000,
  });

  expect(sidecar.running).toBe(true);
  expect(sidecar.processId).toBeGreaterThan(0);
  expect(sidecar.socketPath).toBe(socketPath);
  await sidecar.stop();
  await sidecar.stop();
  expect(sidecar.running).toBe(false);
});
