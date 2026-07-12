import { mkdtemp, readFile, rm, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, expect, it } from "vitest";

import {
  DesktopCredentialStore,
  type DesktopCredentialProfile,
  type SafeStorageAdapter,
} from "./credentialStore.js";

const temporaryDirectories: string[] = [];

class TestSafeStorage implements SafeStorageAdapter {
  isEncryptionAvailable(): boolean {
    return true;
  }

  encryptString(value: string): Buffer {
    return Buffer.from(`encrypted:${Buffer.from(value, "utf8").toString("base64")}`, "utf8");
  }

  decryptString(value: Buffer): string {
    const encodedValue = value.toString("utf8").replace(/^encrypted:/, "");
    return Buffer.from(encodedValue, "base64").toString("utf8");
  }
}

afterEach(async () => {
  await Promise.all(
    temporaryDirectories.splice(0).map((directoryPath) =>
      rm(directoryPath, { force: true, recursive: true }),
    ),
  );
});

function profile(userId: string, email: string): DesktopCredentialProfile {
  return {
    version: 1,
    refreshToken: `refresh-${userId}`,
    refreshTokenExpiresAt: 1_800_000_000,
    accountLease: `lease-${userId}`,
    accountLeaseExpiresAt: 1_700_000_000,
    signingPublicKey: `key-${userId}`,
    deviceId: `device-${userId}`,
    user: { id: userId, email },
  };
}

it("round-trips encrypted credentials through owner-only atomic storage", async () => {
  const directoryPath = await mkdtemp(path.join(os.tmpdir(), "yinshi-credentials-"));
  temporaryDirectories.push(directoryPath);
  const store = new DesktopCredentialStore({
    directoryPath,
    safeStorage: new TestSafeStorage(),
  });
  const profile: DesktopCredentialProfile = {
    version: 1,
    refreshToken: "refresh-secret-value",
    refreshTokenExpiresAt: 1_800_000_000,
    accountLease: "signed-account-lease",
    accountLeaseExpiresAt: 1_700_000_000,
    signingPublicKey: "public-key",
    deviceId: "device-id",
    user: { id: "user-id", email: "user@example.com" },
  };

  await store.save(profile);

  const storedBytes = await readFile(path.join(directoryPath, "credentials.bin"));
  const directoryMode = (await stat(directoryPath)).mode & 0o777;
  const fileMode = (await stat(path.join(directoryPath, "credentials.bin"))).mode & 0o777;
  expect(storedBytes.toString("utf8")).not.toContain(profile.refreshToken);
  expect(directoryMode).toBe(0o700);
  expect(fileMode).toBe(0o600);
  await expect(store.load()).resolves.toEqual(profile);
});

it("switches profiles and keeps signed-out local profile metadata", async () => {
  const directoryPath = await mkdtemp(path.join(os.tmpdir(), "yinshi-profiles-"));
  temporaryDirectories.push(directoryPath);
  const store = new DesktopCredentialStore({
    directoryPath,
    safeStorage: new TestSafeStorage(),
  });
  const first = profile("user-one", "one@example.com");
  const second = profile("user-two", "two@example.com");

  await store.save(first);
  await store.save(second);
  await expect(store.list()).resolves.toEqual([
    { user: first.user, hasCredentials: true, active: false },
    { user: second.user, hasCredentials: true, active: true },
  ]);

  await expect(store.select(first.user.id)).resolves.toEqual(first);
  await expect(store.load()).resolves.toEqual(first);
  await store.clear();

  await expect(store.load()).resolves.toBeNull();
  await expect(store.list()).resolves.toEqual([
    { user: first.user, hasCredentials: false, active: false },
    { user: second.user, hasCredentials: true, active: false },
  ]);
  await store.remove(first.user.id);
  await expect(store.list()).resolves.toEqual([
    { user: second.user, hasCredentials: true, active: false },
  ]);
});
