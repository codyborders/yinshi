import { expect, it } from "vitest";

import { startAutomaticUpdates, type UpdateAdapter } from "./automaticUpdates.js";

it("enforces stable signed update policy only for packaged applications", async () => {
  const listeners = new Map<string, () => void>();
  const scheduled: Array<{ delayMs: number; callback: () => void; cancelled: boolean }> = [];
  let checks = 0;
  const updater: UpdateAdapter = {
    autoDownload: false,
    autoInstallOnAppQuit: false,
    allowDowngrade: true,
    allowPrerelease: true,
    disableWebInstaller: false,
    logger: null,
    on: (event, listener) => {
      listeners.set(event, listener);
    },
    checkForUpdates: async () => {
      checks += 1;
    },
  };
  let downloaded = false;

  const updates = startAutomaticUpdates({
    isPackaged: true,
    updater,
    schedule: (delayMs, callback) => {
      const task = { delayMs, callback, cancelled: false };
      scheduled.push(task);
      return { cancel: () => (task.cancelled = true) };
    },
    onDownloaded: () => {
      downloaded = true;
    },
  });

  expect(updater.autoDownload).toBe(true);
  expect(updater.autoInstallOnAppQuit).toBe(true);
  expect(updater.allowDowngrade).toBe(false);
  expect(updater.allowPrerelease).toBe(false);
  expect(updater.disableWebInstaller).toBe(true);
  expect(scheduled[0]?.delayMs).toBe(30_000);
  scheduled[0]?.callback();
  await Promise.resolve();
  expect(checks).toBe(1);
  expect(scheduled[1]?.delayMs).toBe(6 * 60 * 60 * 1_000);
  listeners.get("update-downloaded")?.();
  expect(downloaded).toBe(true);
  updates.stop();
  expect(scheduled.at(-1)?.cancelled).toBe(true);
});
