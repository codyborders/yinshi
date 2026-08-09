import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

import { afterEach, expect, it } from "vitest";

import {
  cloneRepositoryIntoProfile,
  DirtyRepositoryError,
} from "./localRepositoryImport.js";

const executeFile = promisify(execFile);
const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(
    temporaryDirectories.splice(0).map((directoryPath) =>
      rm(directoryPath, { recursive: true, force: true }),
    ),
  );
});

it("requires dirty confirmation and clones committed state without editing the selection", async () => {
  const directoryPath = await mkdtemp(path.join(os.tmpdir(), "yinshi-import-"));
  temporaryDirectories.push(directoryPath);
  const sourcePath = path.join(directoryPath, "selected");
  const managedBasePath = path.join(directoryPath, "managed");
  const destinationPath = path.join(managedBasePath, "clone");
  await executeFile("/usr/bin/git", ["init", sourcePath]);
  await executeFile("/usr/bin/git", ["-C", sourcePath, "config", "user.name", "Test"]);
  await executeFile("/usr/bin/git", ["-C", sourcePath, "config", "user.email", "test@example.com"]);
  await writeFile(path.join(sourcePath, "tracked.txt"), "committed\n", "utf8");
  await executeFile("/usr/bin/git", ["-C", sourcePath, "add", "tracked.txt"]);
  await executeFile("/usr/bin/git", ["-C", sourcePath, "commit", "-m", "Initial"]);
  await writeFile(path.join(sourcePath, "tracked.txt"), "uncommitted\n", "utf8");

  await expect(
    cloneRepositoryIntoProfile({
      gitCommand: "/usr/bin/git",
      sourcePath,
      managedBasePath,
      destinationPath,
      allowDirty: false,
    }),
  ).rejects.toBeInstanceOf(DirtyRepositoryError);

  const result = await cloneRepositoryIntoProfile({
    gitCommand: "/usr/bin/git",
    sourcePath,
    managedBasePath,
    destinationPath,
    allowDirty: true,
  });

  expect(result.dirty).toBe(true);
  expect(result.name).toBe("selected");
  expect(await readFile(path.join(sourcePath, "tracked.txt"), "utf8")).toBe("uncommitted\n");
  expect(await readFile(path.join(destinationPath, "tracked.txt"), "utf8")).toBe("committed\n");
  const sourceStatus = await executeFile("/usr/bin/git", ["-C", sourcePath, "status", "--porcelain"]);
  expect(sourceStatus.stdout).toContain(" M tracked.txt");
});
