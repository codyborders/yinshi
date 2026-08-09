import { createHash } from "node:crypto";
import { cp, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { execFile } from "node:child_process";

const executeFile = promisify(execFile);
const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const desktopDirectory = path.resolve(scriptDirectory, "..");
const projectDirectory = path.resolve(desktopDirectory, "..");
const runtimeDirectory = path.join(desktopDirectory, "runtime");
const nodeVersion = "v24.18.0";
const nodeArchiveName = `node-${nodeVersion}-darwin-arm64.tar.gz`;
const nodeArchiveSha256 = "e1a97e14c99c803e96c7339403282ea05a499c32f8d83defe9ef5ec66f979ed1";
const nodeArchiveUrl = `https://nodejs.org/dist/${nodeVersion}/${nodeArchiveName}`;
const gitVersion = "2.55.0";
const gitArchiveName = `git-${gitVersion}.tar.xz`;
const gitArchiveSha256 = "457fdb04dc8728e007d4688695e6912e6f680727920f2a40bf11eacc17505357";
const gitArchiveUrl = `https://www.kernel.org/pub/software/scm/git/${gitArchiveName}`;

async function downloadVerifiedArchive({ url, sha256, targetPath, label }) {
  const response = await fetch(url, {
    redirect: "error",
    signal: AbortSignal.timeout(120_000),
  });
  if (!response.ok || response.headers.get("content-type")?.includes("text/html")) {
    throw new Error(`${label} download failed with status ${response.status}`);
  }
  const archive = Buffer.from(await response.arrayBuffer());
  const digest = createHash("sha256").update(archive).digest("hex");
  if (digest !== sha256) {
    throw new Error(`${label} checksum did not match the pinned release`);
  }
  await writeFile(targetPath, archive, { mode: 0o600 });
}

async function prepareNodeRuntime() {
  const archivePath = path.join(runtimeDirectory, nodeArchiveName);
  const extractedDirectory = path.join(runtimeDirectory, `node-${nodeVersion}-darwin-arm64`);
  const nodeDirectory = path.join(runtimeDirectory, "node");
  await downloadVerifiedArchive({
    url: nodeArchiveUrl,
    sha256: nodeArchiveSha256,
    targetPath: archivePath,
    label: "Node runtime",
  });
  await executeFile("/usr/bin/tar", ["-xzf", archivePath, "-C", runtimeDirectory], {
    timeout: 120_000,
  });
  await rename(extractedDirectory, nodeDirectory);
  await rm(archivePath, { force: true });
  const version = await executeFile(path.join(nodeDirectory, "bin", "node"), [
    "-p",
    "process.version + ':' + process.versions.modules",
  ]);
  if (version.stdout.trim() !== `${nodeVersion}:137`) {
    throw new Error("Pinned Node runtime reported an unexpected ABI");
  }
  return nodeDirectory;
}

async function prepareSidecar(nodeDirectory) {
  const sourceDirectory = path.join(projectDirectory, "sidecar");
  const targetDirectory = path.join(runtimeDirectory, "sidecar");
  await mkdir(targetDirectory, { recursive: true, mode: 0o700 });
  await cp(path.join(sourceDirectory, "src"), path.join(targetDirectory, "src"), {
    recursive: true,
    force: true,
  });
  for (const fileName of ["package.json", "package-lock.json"]) {
    await cp(path.join(sourceDirectory, fileName), path.join(targetDirectory, fileName), {
      force: true,
    });
  }
  const npmPath = path.join(nodeDirectory, "bin", "npm");
  const childPath = `${path.join(nodeDirectory, "bin")}:${process.env.PATH ?? "/usr/bin:/bin"}`;
  await executeFile(npmPath, ["ci", "--omit=dev", "--ignore-scripts=false"], {
    cwd: targetDirectory,
    env: { HOME: process.env.HOME ?? desktopDirectory, PATH: childPath },
    maxBuffer: 10 * 1024 * 1024,
    timeout: 600_000,
  });
  await executeFile(path.join(nodeDirectory, "bin", "node"), [
    "-e",
    "import('node-pty').then(() => process.stdout.write('node-pty-ok'))",
  ], {
    cwd: targetDirectory,
    env: { HOME: process.env.HOME ?? desktopDirectory, PATH: childPath },
    timeout: 30_000,
  });
  const packageLock = await readFile(path.join(targetDirectory, "package-lock.json"), "utf8");
  if (!packageLock.includes('"lockfileVersion"')) {
    throw new Error("Staged sidecar dependency lock is invalid");
  }

  const nodePtyDirectory = path.join(targetDirectory, "node_modules", "node-pty");
  for (const entry of ["deps", "scripts", "src", "third_party", "typings"]) {
    await rm(path.join(nodePtyDirectory, entry), { recursive: true, force: true });
  }
  const prebuildDirectory = path.join(nodePtyDirectory, "prebuilds");
  for (const entry of ["darwin-x64", "win32-arm64", "win32-x64"]) {
    await rm(path.join(prebuildDirectory, entry), { recursive: true, force: true });
  }
}

async function prepareGitRuntime() {
  const sourceArchivePath = path.join(runtimeDirectory, "sources", gitArchiveName);
  const sourceDirectory = path.join(runtimeDirectory, `git-${gitVersion}`);
  const installDirectory = path.join(runtimeDirectory, "git");
  await mkdir(path.dirname(sourceArchivePath), { recursive: true, mode: 0o700 });
  await downloadVerifiedArchive({
    url: gitArchiveUrl,
    sha256: gitArchiveSha256,
    targetPath: sourceArchivePath,
    label: "Git source",
  });
  await executeFile("/usr/bin/tar", ["-xJf", sourceArchivePath, "-C", runtimeDirectory], {
    timeout: 120_000,
  });
  const makeOptions = [
    `-j${Math.max(1, os.availableParallelism())}`,
    `prefix=${installDirectory}`,
    "NO_GETTEXT=YesPlease",
    "NO_INSTALL_HARDLINKS=YesPlease",
    "NO_PERL=YesPlease",
    "NO_PYTHON=YesPlease",
    "NO_RUST=YesPlease",
    "NO_TCLTK=YesPlease",
    "RUNTIME_PREFIX=YesPlease",
  ];
  await executeFile("/usr/bin/make", makeOptions, {
    cwd: sourceDirectory,
    maxBuffer: 100 * 1024 * 1024,
    timeout: 900_000,
  });
  await executeFile("/usr/bin/make", [...makeOptions.slice(1), "install"], {
    cwd: sourceDirectory,
    maxBuffer: 100 * 1024 * 1024,
    timeout: 300_000,
  });
  await cp(path.join(sourceDirectory, "COPYING"), path.join(installDirectory, "COPYING"));
  const version = await executeFile(path.join(installDirectory, "bin", "git"), ["--version"], {
    timeout: 30_000,
  });
  if (version.stdout.trim() !== `git version ${gitVersion}`) {
    throw new Error("Bundled Git reported an unexpected version");
  }
  await rm(sourceDirectory, { recursive: true, force: true });
}

async function pruneNodeRuntime(nodeDirectory) {
  for (const entry of [
    "CHANGELOG.md",
    "README.md",
    "include",
    "lib",
    "share",
    path.join("bin", "corepack"),
    path.join("bin", "npm"),
    path.join("bin", "npx"),
  ]) {
    await rm(path.join(nodeDirectory, entry), { recursive: true, force: true });
  }
}

await rm(runtimeDirectory, { recursive: true, force: true });
await mkdir(runtimeDirectory, { recursive: true, mode: 0o700 });
const nodeDirectory = await prepareNodeRuntime();
await prepareSidecar(nodeDirectory);
await pruneNodeRuntime(nodeDirectory);
await prepareGitRuntime();
