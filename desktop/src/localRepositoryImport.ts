import { execFile } from "node:child_process";
import { constants } from "node:fs";
import { access, lstat, mkdir, realpath, rm } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

const executeFile = promisify(execFile);

export class DirtyRepositoryError extends Error {
  constructor() {
    super("selected repository has uncommitted changes");
    this.name = "DirtyRepositoryError";
  }
}

export class InvalidRepositoryError extends Error {
  constructor() {
    super("selected directory is not a safe Git repository");
    this.name = "InvalidRepositoryError";
  }
}

export interface CloneRepositoryOptions {
  readonly gitCommand: string;
  readonly sourcePath: string;
  readonly managedBasePath: string;
  readonly destinationPath: string;
  readonly allowDirty: boolean;
}

export interface ClonedRepository {
  readonly name: string;
  readonly dirty: boolean;
  readonly path: string;
}

function isInside(candidatePath: string, basePath: string): boolean {
  const relativePath = path.relative(basePath, candidatePath);
  return relativePath !== "" && !relativePath.startsWith(`..${path.sep}`) && relativePath !== "..";
}

async function pathExists(candidatePath: string): Promise<boolean> {
  try {
    await lstat(candidatePath);
    return true;
  } catch (error) {
    if (error instanceof Error && "code" in error && error.code === "ENOENT") {
      return false;
    }
    throw error;
  }
}

async function runGit(
  gitCommand: string,
  arguments_: readonly string[],
): Promise<{ stdout: string; stderr: string }> {
  try {
    return await executeFile(gitCommand, [...arguments_], {
      encoding: "utf8",
      env: {
        HOME: process.env.HOME ?? "/var/empty",
        PATH: process.env.PATH ?? "/usr/bin:/bin",
        GIT_CONFIG_GLOBAL: "/dev/null",
        GIT_CONFIG_NOSYSTEM: "1",
        GIT_TERMINAL_PROMPT: "0",
      },
      maxBuffer: 2 * 1024 * 1024,
      timeout: 120_000,
    });
  } catch {
    throw new InvalidRepositoryError();
  }
}

export async function cloneRepositoryIntoProfile(
  options: CloneRepositoryOptions,
): Promise<ClonedRepository> {
  for (const [name, value] of [
    ["gitCommand", options.gitCommand],
    ["sourcePath", options.sourcePath],
    ["managedBasePath", options.managedBasePath],
    ["destinationPath", options.destinationPath],
  ] as const) {
    if (typeof value !== "string" || !path.isAbsolute(value) || value.includes("\0")) {
      throw new TypeError(`${name} must be an absolute path`);
    }
  }
  await access(options.gitCommand, constants.X_OK);
  await mkdir(options.managedBasePath, { mode: 0o700, recursive: true });
  const managedBasePath = await realpath(options.managedBasePath);
  const sourcePath = await realpath(options.sourcePath);
  const destinationParent = await realpath(path.dirname(options.destinationPath));
  const destinationPath = path.join(destinationParent, path.basename(options.destinationPath));
  if (!isInside(destinationPath, managedBasePath) || destinationParent !== managedBasePath) {
    throw new TypeError("destinationPath must be a direct child of managedBasePath");
  }
  if (await pathExists(destinationPath)) {
    throw new Error("managed repository destination already exists");
  }

  const topLevel = (
    await runGit(options.gitCommand, [
      "-C",
      sourcePath,
      "rev-parse",
      "--show-toplevel",
    ])
  ).stdout.trim();
  if (!topLevel || (await realpath(topLevel)) !== sourcePath) {
    throw new InvalidRepositoryError();
  }
  const status = await runGit(options.gitCommand, [
    "-C",
    sourcePath,
    "status",
    "--porcelain=v1",
    "--untracked-files=all",
  ]);
  const dirty = status.stdout.trim().length > 0;
  if (dirty && !options.allowDirty) {
    throw new DirtyRepositoryError();
  }

  try {
    await runGit(options.gitCommand, [
      "clone",
      "--local",
      "--no-hardlinks",
      "--no-checkout",
      "--",
      sourcePath,
      destinationPath,
    ]);
    await runGit(options.gitCommand, [
      "-C",
      destinationPath,
      "-c",
      "core.hooksPath=/dev/null",
      "checkout",
      "--force",
      "HEAD",
    ]);
  } catch (error) {
    await rm(destinationPath, { recursive: true, force: true });
    throw error;
  }
  const repositoryName = path.basename(sourcePath);
  if (!repositoryName || repositoryName === "." || repositoryName === "..") {
    await rm(destinationPath, { recursive: true, force: true });
    throw new InvalidRepositoryError();
  }
  return { name: repositoryName, dirty, path: destinationPath };
}
