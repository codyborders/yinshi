import { spawn, type ChildProcess } from "node:child_process";
import { lstat, mkdir, rm } from "node:fs/promises";
import path from "node:path";

const READINESS_LINE_LENGTH_MAX = 4_096;

export interface SidecarOptions {
  readonly command: string;
  readonly args: readonly string[];
  readonly environment: Readonly<Record<string, string>>;
  readonly workingDirectory?: string;
  readonly socketPath: string;
  readonly startupTimeoutMs: number;
  readonly shutdownTimeoutMs: number;
}

export interface ManagedSidecar {
  readonly processId: number;
  readonly running: boolean;
  readonly socketPath: string;
  stop(): Promise<void>;
}

function validateOptions(options: SidecarOptions): void {
  if (!path.isAbsolute(options.command) || options.command.includes("\0")) {
    throw new TypeError("sidecar command must be an absolute path");
  }
  if (!path.isAbsolute(options.socketPath) || options.socketPath.includes("\0")) {
    throw new TypeError("sidecar socketPath must be an absolute path");
  }
  if (!Number.isInteger(options.startupTimeoutMs) || options.startupTimeoutMs < 1) {
    throw new TypeError("sidecar startupTimeoutMs must be a positive integer");
  }
  if (!Number.isInteger(options.shutdownTimeoutMs) || options.shutdownTimeoutMs < 1) {
    throw new TypeError("sidecar shutdownTimeoutMs must be a positive integer");
  }
  if (options.args.some((argument) => typeof argument !== "string" || argument.includes("\0"))) {
    throw new TypeError("sidecar arguments must be strings without null bytes");
  }
}

async function removeOwnedStaleSocket(socketPath: string): Promise<void> {
  let information;
  try {
    information = await lstat(socketPath);
  } catch (error) {
    if (error instanceof Error && "code" in error && error.code === "ENOENT") {
      return;
    }
    throw error;
  }
  const userId = process.getuid?.();
  if (!information.isSocket() || (userId !== undefined && information.uid !== userId)) {
    throw new Error("sidecar socket path is occupied by an unsafe file");
  }
  await rm(socketPath);
}

async function assertSocketSecurity(socketPath: string): Promise<void> {
  const information = await lstat(socketPath);
  const userId = process.getuid?.();
  if (!information.isSocket()) {
    throw new Error("sidecar did not create a Unix socket");
  }
  if ((information.mode & 0o077) !== 0) {
    throw new Error("sidecar socket permissions are unsafe");
  }
  if (userId !== undefined && information.uid !== userId) {
    throw new Error("sidecar socket owner is invalid");
  }
}

function waitForExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve(true);
  }
  return new Promise((resolve) => {
    const timeout = setTimeout(() => {
      child.off("exit", onExit);
      resolve(false);
    }, timeoutMs);
    const onExit = (): void => {
      clearTimeout(timeout);
      resolve(true);
    };
    child.once("exit", onExit);
  });
}

async function terminateChild(child: ChildProcess, timeoutMs: number): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return;
  }
  child.kill("SIGTERM");
  if (await waitForExit(child, timeoutMs)) {
    return;
  }
  child.kill("SIGKILL");
  if (!(await waitForExit(child, timeoutMs))) {
    throw new Error("sidecar process did not exit after SIGKILL");
  }
}

async function waitForReadiness(
  child: ChildProcess,
  expectedLine: string,
  timeoutMs: number,
): Promise<void> {
  const stdout = child.stdout;
  const stderr = child.stderr;
  if (stdout === null || stderr === null) {
    throw new Error("sidecar output pipes are unavailable");
  }
  stderr.on("data", () => undefined);
  return new Promise((resolve, reject) => {
    let buffer = "";
    let settled = false;
    const finish = (error?: Error): void => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timeout);
      child.off("error", onError);
      child.off("exit", onExit);
      stdout.off("data", onData);
      if (error === undefined) {
        stdout.on("data", () => undefined);
        resolve();
      } else {
        reject(error);
      }
    };
    const onError = (): void => finish(new Error("sidecar process failed during startup"));
    const onExit = (): void => finish(new Error("sidecar process exited before readiness"));
    const onData = (chunk: Buffer | string): void => {
      buffer += chunk.toString();
      if (buffer.length > READINESS_LINE_LENGTH_MAX) {
        finish(new Error("sidecar readiness output exceeded its limit"));
        return;
      }
      const newline = buffer.indexOf("\n");
      if (newline < 0) {
        return;
      }
      const line = buffer.slice(0, newline).replace(/\r$/, "");
      if (line !== expectedLine) {
        finish(new Error("sidecar readiness output was invalid"));
        return;
      }
      finish();
    };
    const timeout = setTimeout(
      () => finish(new Error("sidecar readiness timed out")),
      timeoutMs,
    );
    child.once("error", onError);
    child.once("exit", onExit);
    stdout.on("data", onData);
  });
}

export async function startSidecar(options: SidecarOptions): Promise<ManagedSidecar> {
  validateOptions(options);
  const socketDirectory = path.dirname(options.socketPath);
  await mkdir(socketDirectory, { mode: 0o700, recursive: true });
  await removeOwnedStaleSocket(options.socketPath);

  const child = spawn(options.command, [...options.args], {
    cwd: options.workingDirectory,
    env: { ...options.environment },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  try {
    await waitForReadiness(
      child,
      `SOCKET_PATH=${options.socketPath}`,
      options.startupTimeoutMs,
    );
    await assertSocketSecurity(options.socketPath);
  } catch (error) {
    await terminateChild(child, options.shutdownTimeoutMs);
    await removeOwnedStaleSocket(options.socketPath);
    throw error;
  }
  if (child.pid === undefined || child.pid < 1) {
    await terminateChild(child, options.shutdownTimeoutMs);
    throw new Error("sidecar process ID is unavailable");
  }

  let stopOperation: Promise<void> | undefined;
  return {
    processId: child.pid,
    get running(): boolean {
      return child.exitCode === null && child.signalCode === null;
    },
    socketPath: options.socketPath,
    stop(): Promise<void> {
      stopOperation ??= (async () => {
        await terminateChild(child, options.shutdownTimeoutMs);
        await removeOwnedStaleSocket(options.socketPath);
      })();
      return stopOperation;
    },
  };
}
