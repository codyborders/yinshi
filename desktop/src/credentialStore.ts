import {
  SecureJsonStore,
  type SafeStorageAdapter,
} from "./secureJsonStore.js";

export type { SafeStorageAdapter } from "./secureJsonStore.js";

export interface DesktopCredentialProfile {
  readonly version: 1;
  readonly refreshToken: string;
  readonly refreshTokenExpiresAt: number;
  readonly accountLease: string;
  readonly accountLeaseExpiresAt: number;
  readonly signingPublicKey: string;
  readonly deviceId: string;
  readonly user: {
    readonly id: string;
    readonly email: string;
  };
}

export interface DesktopProfileSummary {
  readonly user: DesktopCredentialProfile["user"];
  readonly hasCredentials: boolean;
  readonly active: boolean;
}

interface DesktopCredentialVaultEntry {
  readonly user: DesktopCredentialProfile["user"];
  readonly profile: DesktopCredentialProfile | null;
}

interface DesktopCredentialVault {
  readonly version: 2;
  readonly activeUserId: string | null;
  readonly entries: readonly DesktopCredentialVaultEntry[];
}

export interface DesktopCredentialStoreOptions {
  readonly directoryPath: string;
  readonly safeStorage: SafeStorageAdapter;
}

function requiredString(value: unknown, name: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`desktop credential ${name} is invalid`);
  }
  return value;
}

function positiveInteger(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) {
    throw new Error(`desktop credential ${name} is invalid`);
  }
  return value;
}

function validateProfile(value: unknown): DesktopCredentialProfile {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("desktop credential profile is invalid");
  }
  const profile = value as Record<string, unknown>;
  if (profile.version !== 1) {
    throw new Error("desktop credential profile version is unsupported");
  }
  const userValue = profile.user;
  if (typeof userValue !== "object" || userValue === null || Array.isArray(userValue)) {
    throw new Error("desktop credential user is invalid");
  }
  const user = userValue as Record<string, unknown>;
  const refreshTokenExpiresAt = positiveInteger(
    profile.refreshTokenExpiresAt,
    "refreshTokenExpiresAt",
  );
  const accountLeaseExpiresAt = positiveInteger(
    profile.accountLeaseExpiresAt,
    "accountLeaseExpiresAt",
  );
  if (accountLeaseExpiresAt > refreshTokenExpiresAt) {
    throw new Error("desktop credential expiry order is invalid");
  }
  return {
    version: 1,
    refreshToken: requiredString(profile.refreshToken, "refreshToken"),
    refreshTokenExpiresAt,
    accountLease: requiredString(profile.accountLease, "accountLease"),
    accountLeaseExpiresAt,
    signingPublicKey: requiredString(profile.signingPublicKey, "signingPublicKey"),
    deviceId: requiredString(profile.deviceId, "deviceId"),
    user: {
      id: requiredString(user.id, "user.id"),
      email: requiredString(user.email, "user.email"),
    },
  };
}

function validateVault(value: unknown): DesktopCredentialVault {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    const candidate = value as Record<string, unknown>;
    if (candidate.version === 1) {
      const profile = validateProfile(value);
      return {
        version: 2,
        activeUserId: profile.user.id,
        entries: [{ user: profile.user, profile }],
      };
    }
    if (candidate.version === 2) {
      if (!Array.isArray(candidate.entries) || candidate.entries.length > 32) {
        throw new Error("desktop credential vault entries are invalid");
      }
      const entries = candidate.entries.map((entryValue) => {
        if (typeof entryValue !== "object" || entryValue === null || Array.isArray(entryValue)) {
          throw new Error("desktop credential vault entry is invalid");
        }
        const entry = entryValue as Record<string, unknown>;
        const userValue = entry.user;
        if (typeof userValue !== "object" || userValue === null || Array.isArray(userValue)) {
          throw new Error("desktop credential vault user is invalid");
        }
        const userRecord = userValue as Record<string, unknown>;
        const user = {
          id: requiredString(userRecord.id, "vault.user.id"),
          email: requiredString(userRecord.email, "vault.user.email"),
        };
        const profile = entry.profile === null ? null : validateProfile(entry.profile);
        if (
          profile !== null &&
          (profile.user.id !== user.id || profile.user.email !== user.email)
        ) {
          throw new Error("desktop credential vault profile identity changed");
        }
        return { user, profile };
      });
      const userIds = entries.map((entry) => entry.user.id);
      if (new Set(userIds).size !== userIds.length) {
        throw new Error("desktop credential vault contains duplicate users");
      }
      const activeUserId = candidate.activeUserId;
      if (activeUserId !== null && typeof activeUserId !== "string") {
        throw new Error("desktop credential vault active user is invalid");
      }
      if (
        activeUserId !== null &&
        !entries.some(
          (entry) => entry.user.id === activeUserId && entry.profile !== null,
        )
      ) {
        throw new Error("desktop credential vault active profile is unavailable");
      }
      return { version: 2, activeUserId, entries };
    }
  }
  throw new Error("desktop credential vault is invalid");
}

const EMPTY_VAULT: DesktopCredentialVault = {
  version: 2,
  activeUserId: null,
  entries: [],
};

export class DesktopCredentialStore {
  readonly #store: SecureJsonStore<DesktopCredentialVault>;
  #operation: Promise<void> = Promise.resolve();

  constructor(options: DesktopCredentialStoreOptions) {
    this.#store = new SecureJsonStore({
      directoryPath: options.directoryPath,
      fileName: "credentials.bin",
      safeStorage: options.safeStorage,
      validate: validateVault,
    });
  }

  #enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.#operation.then(operation, operation);
    this.#operation = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }

  save(profileValue: DesktopCredentialProfile): Promise<void> {
    const profile = validateProfile(profileValue);
    return this.#enqueue(async () => {
      const vault = (await this.#store.load()) ?? EMPTY_VAULT;
      const entries = vault.entries.filter((entry) => entry.user.id !== profile.user.id);
      entries.push({ user: profile.user, profile });
      await this.#store.save({
        version: 2,
        activeUserId: profile.user.id,
        entries,
      });
    });
  }

  load(): Promise<DesktopCredentialProfile | null> {
    return this.#enqueue(async () => {
      const vault = await this.#store.load();
      if (vault === null || vault.activeUserId === null) return null;
      return (
        vault.entries.find((entry) => entry.user.id === vault.activeUserId)?.profile ??
        null
      );
    });
  }

  list(): Promise<DesktopProfileSummary[]> {
    return this.#enqueue(async () => {
      const vault = await this.#store.load();
      if (vault === null) return [];
      return vault.entries.map((entry) => ({
        user: entry.user,
        hasCredentials: entry.profile !== null,
        active: entry.user.id === vault.activeUserId,
      }));
    });
  }

  select(userIdValue: string): Promise<DesktopCredentialProfile | null> {
    const userId = requiredString(userIdValue, "selectedUserId");
    return this.#enqueue(async () => {
      const vault = await this.#store.load();
      if (vault === null) return null;
      const selectedEntry = vault.entries.find((entry) => entry.user.id === userId);
      if (selectedEntry?.profile === null || selectedEntry === undefined) return null;
      await this.#store.save({ ...vault, activeUserId: userId });
      return selectedEntry.profile;
    });
  }

  clear(): Promise<void> {
    return this.#enqueue(async () => {
      const vault = await this.#store.load();
      if (vault === null || vault.activeUserId === null) return;
      const entries = vault.entries.map((entry) =>
        entry.user.id === vault.activeUserId ? { ...entry, profile: null } : entry,
      );
      await this.#store.save({ version: 2, activeUserId: null, entries });
    });
  }

  remove(userIdValue: string): Promise<void> {
    const userId = requiredString(userIdValue, "removedUserId");
    return this.#enqueue(async () => {
      const vault = await this.#store.load();
      if (vault === null) return;
      const entries = vault.entries.filter((entry) => entry.user.id !== userId);
      if (entries.length === 0) {
        await this.#store.clear();
        return;
      }
      await this.#store.save({
        version: 2,
        activeUserId: vault.activeUserId === userId ? null : vault.activeUserId,
        entries,
      });
    });
  }
}
