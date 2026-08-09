import { expect, it } from "vitest";

import { startLocalRuntime } from "./localRuntime.js";

it("stops the sidecar when the Python helper fails to start", async () => {
  let sidecarStopped = false;

  await expect(
    startLocalRuntime({
      helper: {
        command: "/missing/helper",
        workingDirectory: "/tmp",
        args: [],
        environment: { SIDECAR_SOCKET_PATH: "/tmp/yinshi-test-sidecar.sock" },
      },
      sidecar: {
        command: "/missing/node",
        workingDirectory: "/tmp",
        args: [],
        environment: { SIDECAR_SOCKET_PATH: "/tmp/yinshi-test-sidecar.sock" },
      },
      socketPath: "/tmp/yinshi-test-sidecar.sock",
      startSidecar: async () => ({
        processId: 10,
        running: true,
        socketPath: "/tmp/yinshi-test-sidecar.sock",
        stop: async () => {
          sidecarStopped = true;
        },
      }),
      startHelper: async () => {
        throw new Error("helper startup failed");
      },
    }),
  ).rejects.toThrow("helper startup failed");
  expect(sidecarStopped).toBe(true);
});
