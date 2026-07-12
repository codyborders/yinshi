import { mkdtemp, readFile, rm, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, expect, it } from "vitest";

import type { SafeStorageAdapter } from "./credentialStore.js";
import { RuntimeSecretStore } from "./runtimeSecrets.js";

const temporaryDirectories: string[] = [];

class TestSafeStorage implements SafeStorageAdapter {
  isEncryptionAvailable(): boolean {
    return true;
  }

  encryptString(value: string): Buffer {
    return Buffer.from(`encrypted:${Buffer.from(value).toString("base64")}`);
  }

  decryptString(value: Buffer): string {
    return Buffer.from(value.toString().replace(/^encrypted:/, ""), "base64").toString();
  }
}

afterEach(async () => {
  await Promise.all(
    temporaryDirectories.splice(0).map((directoryPath) =>
      rm(directoryPath, { recursive: true, force: true }),
    ),
  );
});

it("creates Keychain-encrypted runtime secrets once and reuses them", async () => {
  const directoryPath = await mkdtemp(path.join(os.tmpdir(), "yinshi-runtime-secrets-"));
  temporaryDirectories.push(directoryPath);
  const store = new RuntimeSecretStore({
    directoryPath,
    safeStorage: new TestSafeStorage(),
  });

  const created = await store.loadOrCreate();
  const loaded = await store.loadOrCreate();
  const storedBytes = await readFile(path.join(directoryPath, "runtime-secrets.bin"));

  expect(created).toEqual(loaded);
  expect(created.secretKey).toMatch(/^[A-Za-z0-9_-]{64}$/);
  expect(created.encryptionPepper).toMatch(/^[a-f0-9]{64}$/);
  expect(created.keyEncryptionKey).toMatch(/^[a-f0-9]{64}$/);
  expect(storedBytes.toString()).not.toContain(created.secretKey);
  expect(storedBytes.toString()).not.toContain(created.encryptionPepper);
  expect((await stat(path.join(directoryPath, "runtime-secrets.bin"))).mode & 0o777).toBe(0o600);
});
