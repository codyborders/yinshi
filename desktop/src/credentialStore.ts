import { randomUUID } from "node:crypto";
import {
  chmod,
  lstat,
  mkdir,
  open,
  readFile,
  rename,
  rm,
} from "node:fs/promises";
import path from "node:path";

export interface SafeStorageAdapter {
  isEncryptionAvailable(): boolean;
  encryptString(value: string): Buffer;
  decryptString(value: Buffer): string;
}

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

function isNodeError(error: unknown): error is NodeJS.ErrnoException {
  return error instanceof Error && "code" in error;
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

async function ensureOwnerOnlyDirectory(directoryPath: string): Promise<void> {
  await mkdir(directoryPath, { mode: 0o700, recursive: true });
  const information = await lstat(directoryPath);
  if (!information.isDirectory() || information.isSymbolicLink()) {
    throw new Error("desktop credential directory must be a real directory");
  }
  await chmod(directoryPath, 0o700);
}

async function assertOwnerOnlyFile(filePath: string): Promise<boolean> {
  let information;
  try {
    information = await lstat(filePath);
  } catch (error) {
    if (isNodeError(error) && error.code === "ENOENT") {
      return false;
    }
    throw error;
  }
  if (!information.isFile() || information.isSymbolicLink()) {
    throw new Error("desktop credential path must be a regular file");
  }
  if ((information.mode & 0o077) !== 0) {
    throw new Error("desktop credential file permissions are unsafe");
  }
  return true;
}

export class DesktopCredentialStore {
  readonly #directoryPath: string;
  readonly #filePath: string;
  readonly #safeStorage: SafeStorageAdapter;

  constructor(options: DesktopCredentialStoreOptions) {
    if (typeof options.directoryPath !== "string" || !path.isAbsolute(options.directoryPath)) {
      throw new TypeError("directoryPath must be an absolute path");
    }
    if (typeof options.safeStorage?.isEncryptionAvailable !== "function") {
      throw new TypeError("safeStorage must implement Electron safeStorage methods");
    }
    this.#directoryPath = options.directoryPath;
    this.#filePath = path.join(options.directoryPath, "credentials.bin");
    this.#safeStorage = options.safeStorage;
  }

  async save(profile: DesktopCredentialProfile): Promise<void> {
    const validatedProfile = validateProfile(profile);
    if (!this.#safeStorage.isEncryptionAvailable()) {
      throw new Error("Keychain encryption is unavailable");
    }
    await ensureOwnerOnlyDirectory(this.#directoryPath);
    await assertOwnerOnlyFile(this.#filePath);

    const encryptedProfile = this.#safeStorage.encryptString(JSON.stringify(validatedProfile));
    if (!Buffer.isBuffer(encryptedProfile) || encryptedProfile.length === 0) {
      throw new Error("Keychain encryption returned no credential data");
    }
    const temporaryPath = path.join(
      this.#directoryPath,
      `credentials.${randomUUID()}.tmp`,
    );
    try {
      const temporaryFile = await open(temporaryPath, "wx", 0o600);
      try {
        await temporaryFile.writeFile(encryptedProfile);
        await temporaryFile.sync();
      } finally {
        await temporaryFile.close();
      }
      await rename(temporaryPath, this.#filePath);
      await chmod(this.#filePath, 0o600);
    } finally {
      await rm(temporaryPath, { force: true });
    }
  }

  async load(): Promise<DesktopCredentialProfile | null> {
    await ensureOwnerOnlyDirectory(this.#directoryPath);
    if (!(await assertOwnerOnlyFile(this.#filePath))) {
      return null;
    }
    if (!this.#safeStorage.isEncryptionAvailable()) {
      throw new Error("Keychain encryption is unavailable");
    }
    const encryptedProfile = await readFile(this.#filePath);
    if (encryptedProfile.length === 0) {
      throw new Error("desktop credential file is empty");
    }
    let parsedProfile: unknown;
    try {
      parsedProfile = JSON.parse(this.#safeStorage.decryptString(encryptedProfile));
    } catch {
      throw new Error("desktop credential file cannot be decrypted");
    }
    return validateProfile(parsedProfile);
  }

  async clear(): Promise<void> {
    await ensureOwnerOnlyDirectory(this.#directoryPath);
    if (!(await assertOwnerOnlyFile(this.#filePath))) {
      return;
    }
    await rm(this.#filePath);
  }
}
