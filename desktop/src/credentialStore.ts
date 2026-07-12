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

export class DesktopCredentialStore {
  readonly #store: SecureJsonStore<DesktopCredentialProfile>;

  constructor(options: DesktopCredentialStoreOptions) {
    this.#store = new SecureJsonStore({
      directoryPath: options.directoryPath,
      fileName: "credentials.bin",
      safeStorage: options.safeStorage,
      validate: validateProfile,
    });
  }

  save(profile: DesktopCredentialProfile): Promise<void> {
    return this.#store.save(profile);
  }

  load(): Promise<DesktopCredentialProfile | null> {
    return this.#store.load();
  }

  clear(): Promise<void> {
    return this.#store.clear();
  }
}
