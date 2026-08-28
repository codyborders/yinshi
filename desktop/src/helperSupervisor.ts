import { spawn, type ChildProcess } from "node:child_process";
import { Readable } from "node:stream";

import { parseHelperReadyLine, type HelperReadyMessage } from "./helperProtocol.js";

export interface ManagedHelper {
  readonly ready: HelperReadyMessage;
  readonly processId: number;
  readonly running: boolean;
  stop(): Promise<void>;
}

export interface StartManagedHelperOptions {
  command: string;
  arguments: string[];
  environment: Record<string, string>;
  workingDirectory?: string;
  readinessTimeoutMs: number;
  shutdownTimeoutMs: number;
}

function childIsRunning(child: ChildProcess): boolean {
  return child.exitCode === null && child.signalCode === null;
}

function waitForExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
  if (!childIsRunning(child)) {
    return Promise.resolve(true);
  }
  return new Promise((resolve) => {
    const timeout = setTimeout(() => finish(false), timeoutMs);
    const onExit = () => finish(true);

    function finish(exited: boolean): void {
      clearTimeout(timeout);
      child.off("exit", onExit);
      resolve(exited);
    }

    child.once("exit", onExit);
  });
}

async function stopChild(child: ChildProcess, timeoutMs: number): Promise<void> {
  if (child.pid === undefined || !childIsRunning(child)) {
    return;
  }

  const gracefulExit = waitForExit(child, timeoutMs);
  child.kill("SIGTERM");
  if (await gracefulExit) {
    return;
  }

  const forcedExit = waitForExit(child, timeoutMs);
  child.kill("SIGKILL");
  if (!(await forcedExit)) {
    throw new Error("helper did not stop after SIGKILL");
  }
}

const MAX_HELPER_READY_LINE_BYTES = 4096;

function readFirstReadyLine(readyPipe: Readable, timeoutMs: number): Promise<string> {
  return new Promise((resolve, reject) => {
    let line = Buffer.alloc(0);
    let timeout: NodeJS.Timeout | undefined;

    const cleanup = (): void => {
      if (timeout !== undefined) {
        clearTimeout(timeout);
      }
      readyPipe.off("data", onData);
      readyPipe.off("end", onEnd);
      readyPipe.off("error", onError);
      readyPipe.pause();
    };
    const fail = (error: Error): void => {
      cleanup();
      reject(error);
    };
    const onData = (chunk: Buffer | string): void => {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      const newlineIndex = bytes.indexOf(0x0a);
      const lineBytes =
        newlineIndex === -1 ? bytes : bytes.subarray(0, newlineIndex);
      if (line.length + lineBytes.length > MAX_HELPER_READY_LINE_BYTES) {
        fail(
          new Error(
            `helper readiness line exceeds ${MAX_HELPER_READY_LINE_BYTES} bytes`,
          ),
        );
        return;
      }
      const next = Buffer.concat([line, lineBytes]);
      if (newlineIndex !== -1) {
        const lineEnd = next[next.length - 1] === 0x0d ? next.length - 1 : next.length;
        cleanup();
        resolve(next.subarray(0, lineEnd).toString("utf8"));
        return;
      }
      line = next;
    };
    const onEnd = (): void => {
      fail(new Error("helper closed readiness pipe without a message"));
    };
    const onError = (error: Error): void => {
      fail(error);
    };

    readyPipe.on("data", onData);
    readyPipe.once("end", onEnd);
    readyPipe.once("error", onError);
    timeout = setTimeout(
      () => fail(new Error("helper readiness timed out")),
      timeoutMs,
    );
    readyPipe.resume();
  });
}

export async function startManagedHelper(
  options: StartManagedHelperOptions,
): Promise<ManagedHelper> {
  if (!Number.isInteger(options.readinessTimeoutMs) || options.readinessTimeoutMs < 1) {
    throw new TypeError("readinessTimeoutMs must be a positive integer");
  }
  if (!Number.isInteger(options.shutdownTimeoutMs) || options.shutdownTimeoutMs < 1) {
    throw new TypeError("shutdownTimeoutMs must be a positive integer");
  }

  const child = spawn(options.command, options.arguments, {
    cwd: options.workingDirectory,
    env: options.environment,
    stdio: ["ignore", "ignore", "ignore", "pipe"],
  });

  // Node reports ENOENT, EACCES, and invalid working directories through
  // the "error" event. Keep a listener attached for the child's whole
  // lifetime so the event can never become an unhandled EventEmitter
  // failure, and surface the error while startup is still pending.
  let startupPending = true;
  let failStartup: ((error: Error) => void) | undefined;
  const spawnFailure = new Promise<never>((_, reject) => {
    failStartup = reject;
  });
  spawnFailure.catch(() => undefined);
  child.on("error", (error: Error) => {
    if (startupPending) {
      failStartup?.(error);
    }
  });

  const readyPipe = child.stdio[3];
  if (!(readyPipe instanceof Readable)) {
    await stopChild(child, options.shutdownTimeoutMs);
    throw new Error("helper readiness pipe was unavailable");
  }

  let ready: HelperReadyMessage;
  try {
    const readyLine = await Promise.race([
      readFirstReadyLine(readyPipe, options.readinessTimeoutMs),
      spawnFailure,
    ]);
    ready = parseHelperReadyLine(readyLine);
  } catch (error) {
    await stopChild(child, options.shutdownTimeoutMs);
    throw error;
  }
  startupPending = false;
  if (child.pid === undefined || child.pid < 1) {
    await stopChild(child, options.shutdownTimeoutMs);
    throw new Error("helper process id was unavailable");
  }

  let stopPromise: Promise<void> | undefined;
  return {
    ready,
    processId: child.pid,
    get running(): boolean {
      return childIsRunning(child);
    },
    stop(): Promise<void> {
      stopPromise ??= stopChild(child, options.shutdownTimeoutMs);
      return stopPromise;
    },
  };
}
