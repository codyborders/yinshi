const UPDATE_CHECK_DELAY_INITIAL_MS = 30_000;
const UPDATE_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1_000;

export interface UpdateAdapter {
  autoDownload: boolean;
  autoInstallOnAppQuit: boolean;
  allowDowngrade: boolean;
  allowPrerelease: boolean;
  disableWebInstaller: boolean;
  logger: unknown;
  on(event: "update-downloaded" | "error", listener: () => void): void;
  checkForUpdates(): Promise<unknown>;
}

export interface ScheduledUpdateTask {
  cancel(): void;
}

export interface AutomaticUpdateOptions {
  readonly isPackaged: boolean;
  readonly updater: UpdateAdapter;
  readonly schedule: (delayMs: number, callback: () => void) => ScheduledUpdateTask;
  readonly onDownloaded: () => void;
}

export interface AutomaticUpdateController {
  stop(): void;
}

const sanitizedLogger = Object.freeze({
  debug: (): void => undefined,
  error: (): void => undefined,
  info: (): void => undefined,
  warn: (): void => undefined,
});

export function startAutomaticUpdates(
  options: AutomaticUpdateOptions,
): AutomaticUpdateController {
  if (!options.isPackaged) {
    return Object.freeze({ stop: (): void => undefined });
  }
  if (typeof options.schedule !== "function" || typeof options.onDownloaded !== "function") {
    throw new TypeError("automatic update callbacks are invalid");
  }

  options.updater.autoDownload = true;
  options.updater.autoInstallOnAppQuit = true;
  options.updater.allowDowngrade = false;
  options.updater.allowPrerelease = false;
  options.updater.disableWebInstaller = true;
  options.updater.logger = sanitizedLogger;

  let stopped = false;
  let scheduledTask: ScheduledUpdateTask | undefined;
  const scheduleCheck = (delayMs: number): void => {
    if (stopped) {
      return;
    }
    scheduledTask = options.schedule(delayMs, () => {
      void (async () => {
        if (stopped) {
          return;
        }
        try {
          await options.updater.checkForUpdates();
        } catch {
          // Update errors are intentionally sanitized and retried on the fixed interval.
        }
        scheduleCheck(UPDATE_CHECK_INTERVAL_MS);
      })();
    });
  };
  options.updater.on("update-downloaded", () => {
    if (!stopped) {
      options.onDownloaded();
    }
  });
  options.updater.on("error", () => undefined);
  scheduleCheck(UPDATE_CHECK_DELAY_INITIAL_MS);

  return Object.freeze({
    stop(): void {
      if (stopped) {
        return;
      }
      stopped = true;
      scheduledTask?.cancel();
      scheduledTask = undefined;
    },
  });
}
