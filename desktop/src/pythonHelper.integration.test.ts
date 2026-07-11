// Verifies Electron supervision interoperates with the real Python loopback helper.

import { constants } from "node:fs";
import { access, mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, it } from "vitest";

import { startManagedHelper, type ManagedHelper } from "./helperSupervisor.js";

const repositoryRoot = fileURLToPath(new URL("../..", import.meta.url));
const backendDirectory = path.join(repositoryRoot, "backend");
const pythonExecutable = path.join(backendDirectory, ".venv", "bin", "python");

it("starts the real Python helper and reaches loopback health", async () => {
  await access(pythonExecutable, constants.X_OK);
  const dataDirectory = await mkdtemp(path.join(os.tmpdir(), "yinshi-desktop-integration-"));
  let helper: ManagedHelper | undefined;

  try {
    helper = await startManagedHelper({
      command: pythonExecutable,
      arguments: ["-m", "yinshi.desktop_runtime", "--ready-fd", "3"],
      environment: {
        PATH: process.env.PATH ?? "/usr/bin:/bin",
        PYTHONPATH: path.join(backendDirectory, "src"),
        DB_PATH: path.join(dataDirectory, "legacy.db"),
        CONTROL_DB_PATH: path.join(dataDirectory, "control.db"),
        USER_DATA_DIR: path.join(dataDirectory, "users"),
        ENCRYPTION_PEPPER: "a".repeat(64),
        SECRET_KEY: "desktop-integration-secret-0123456789",
        DISABLE_AUTH: "true",
        CONTAINER_ENABLED: "false",
        TENANT_DB_ENCRYPTION: "disabled",
        CONTROL_FIELD_ENCRYPTION: "disabled",
        USER_DATA_ENCRYPTION: "disabled",
        REQUIRE_HTTPS: "disabled",
        TRUSTED_HOSTS: "127.0.0.1,localhost,[::1]",
        HOST: "127.0.0.1",
      },
      readinessTimeoutMs: 10_000,
      shutdownTimeoutMs: 5_000,
    });

    const origin = `http://127.0.0.1:${helper.ready.port}`;
    const unauthenticated = await fetch(`${origin}/health`, {
      signal: AbortSignal.timeout(5_000),
    });
    expect(unauthenticated.status).toBe(401);

    const bootstrap = await fetch(`${origin}/desktop/bootstrap`, {
      method: "POST",
      headers: { "X-Yinshi-Bootstrap": helper.ready.instanceNonce },
      redirect: "error",
      signal: AbortSignal.timeout(5_000),
    });
    expect(bootstrap.status).toBe(204);
    const cookie = bootstrap.headers.get("set-cookie");
    expect(cookie).toContain("HttpOnly");
    expect(cookie).not.toContain(helper.ready.instanceNonce);

    const response = await fetch(`${origin}/health`, {
      headers: { Cookie: cookie?.split(";", 1)[0] ?? "" },
      signal: AbortSignal.timeout(5_000),
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "ok" });
  } finally {
    await helper?.stop();
    await rm(dataDirectory, { force: true, recursive: true });
  }
});
