import { spawn, type ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";
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
  if (!childIsRunning(child)) {
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

async function readFirstReadyLine(readyPipe: Readable, timeoutMs: number): Promise<string> {
  const lines = createInterface({ input: readyPipe, crlfDelay: Infinity });
  let timeout: NodeJS.Timeout | undefined;
  const timeoutResult = new Promise<never>((_, reject) => {
    timeout = setTimeout(
      () => reject(new Error("helper readiness timed out")),
      timeoutMs,
    );
  });

  try {
    const firstLine = await Promise.race([
      lines[Symbol.asyncIterator]().next(),
      timeoutResult,
    ]);
    if (firstLine.done || typeof firstLine.value !== "string") {
      throw new Error("helper closed readiness pipe without a message");
    }
    return firstLine.value;
  } finally {
    if (timeout !== undefined) {
      clearTimeout(timeout);
    }
    lines.close();
  }
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
  const readyPipe = child.stdio[3];
  if (!(readyPipe instanceof Readable)) {
    await stopChild(child, options.shutdownTimeoutMs);
    throw new Error("helper readiness pipe was unavailable");
  }

  let ready: HelperReadyMessage;
  try {
    const readyLine = await readFirstReadyLine(readyPipe, options.readinessTimeoutMs);
    ready = parseHelperReadyLine(readyLine);
  } catch (error) {
    await stopChild(child, options.shutdownTimeoutMs);
    throw error;
  }
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
