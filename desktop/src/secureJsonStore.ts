import { randomUUID } from "node:crypto";
import { constants } from "node:fs";
import {
  chmod,
  lstat,
  mkdir,
  open,
  rename,
  rm,
} from "node:fs/promises";
import path from "node:path";

export interface SafeStorageAdapter {
  isEncryptionAvailable(): boolean;
  encryptString(value: string): Buffer;
  decryptString(value: Buffer): string;
}

export interface SecureJsonStoreOptions<Value> {
  readonly directoryPath: string;
  readonly fileName: string;
  readonly safeStorage: SafeStorageAdapter;
  readonly validate: (value: unknown) => Value;
}

function isNodeError(error: unknown): error is NodeJS.ErrnoException {
  return error instanceof Error && "code" in error;
}

async function ensureOwnerOnlyDirectory(directoryPath: string): Promise<void> {
  await mkdir(directoryPath, { mode: 0o700, recursive: true });
  const information = await lstat(directoryPath);
  if (!information.isDirectory() || information.isSymbolicLink()) {
    throw new Error("secure storage directory must be a real directory");
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
    throw new Error("secure storage path must be a regular file");
  }
  if ((information.mode & 0o077) !== 0) {
    throw new Error("secure storage file permissions are unsafe");
  }
  return true;
}

async function syncDirectory(directoryPath: string): Promise<void> {
  const directory = await open(directoryPath, constants.O_RDONLY);
  try {
    await directory.sync();
  } finally {
    await directory.close();
  }
}

export class SecureJsonStore<Value> {
  readonly #directoryPath: string;
  readonly #filePath: string;
  readonly #safeStorage: SafeStorageAdapter;
  readonly #validate: (value: unknown) => Value;

  constructor(options: SecureJsonStoreOptions<Value>) {
    if (typeof options.directoryPath !== "string" || !path.isAbsolute(options.directoryPath)) {
      throw new TypeError("directoryPath must be an absolute path");
    }
    if (
      typeof options.fileName !== "string" ||
      !/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(options.fileName) ||
      options.fileName === "." ||
      options.fileName === ".."
    ) {
      throw new TypeError("fileName must be one safe relative file name");
    }
    if (typeof options.safeStorage?.isEncryptionAvailable !== "function") {
      throw new TypeError("safeStorage must implement Electron safeStorage methods");
    }
    if (typeof options.validate !== "function") {
      throw new TypeError("validate must be a function");
    }
    this.#directoryPath = options.directoryPath;
    this.#filePath = path.join(options.directoryPath, options.fileName);
    this.#safeStorage = options.safeStorage;
    this.#validate = options.validate;
  }

  async save(value: Value): Promise<void> {
    const validatedValue = this.#validate(value);
    if (!this.#safeStorage.isEncryptionAvailable()) {
      throw new Error("Keychain encryption is unavailable");
    }
    await ensureOwnerOnlyDirectory(this.#directoryPath);
    await assertOwnerOnlyFile(this.#filePath);

    const encryptedValue = this.#safeStorage.encryptString(JSON.stringify(validatedValue));
    if (!Buffer.isBuffer(encryptedValue) || encryptedValue.length === 0) {
      throw new Error("Keychain encryption returned no data");
    }
    const temporaryPath = path.join(this.#directoryPath, `${randomUUID()}.tmp`);
    try {
      const temporaryFile = await open(
        temporaryPath,
        constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY,
        0o600,
      );
      try {
        await temporaryFile.writeFile(encryptedValue);
        await temporaryFile.sync();
      } finally {
        await temporaryFile.close();
      }
      await rename(temporaryPath, this.#filePath);
      await chmod(this.#filePath, 0o600);
      await syncDirectory(this.#directoryPath);
    } finally {
      await rm(temporaryPath, { force: true });
    }
  }

  async load(): Promise<Value | null> {
    await ensureOwnerOnlyDirectory(this.#directoryPath);
    if (!(await assertOwnerOnlyFile(this.#filePath))) {
      return null;
    }
    if (!this.#safeStorage.isEncryptionAvailable()) {
      throw new Error("Keychain encryption is unavailable");
    }

    const storedFile = await open(this.#filePath, constants.O_RDONLY | constants.O_NOFOLLOW);
    let encryptedValue: Buffer;
    try {
      const information = await storedFile.stat();
      if (!information.isFile() || (information.mode & 0o077) !== 0) {
        throw new Error("secure storage file changed during read");
      }
      encryptedValue = await storedFile.readFile();
    } finally {
      await storedFile.close();
    }
    if (encryptedValue.length === 0) {
      throw new Error("secure storage file is empty");
    }
    let parsedValue: unknown;
    try {
      parsedValue = JSON.parse(this.#safeStorage.decryptString(encryptedValue));
    } catch {
      throw new Error("secure storage file cannot be decrypted");
    }
    return this.#validate(parsedValue);
  }

  async clear(): Promise<void> {
    await ensureOwnerOnlyDirectory(this.#directoryPath);
    if (!(await assertOwnerOnlyFile(this.#filePath))) {
      return;
    }
    await rm(this.#filePath);
    await syncDirectory(this.#directoryPath);
  }
}
