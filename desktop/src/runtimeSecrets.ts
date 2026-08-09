import { randomBytes } from "node:crypto";

import type { SafeStorageAdapter } from "./credentialStore.js";
import { SecureJsonStore } from "./secureJsonStore.js";

export interface RuntimeSecrets {
  readonly version: 1;
  readonly secretKey: string;
  readonly encryptionPepper: string;
  readonly keyEncryptionKey: string;
}

export interface RuntimeSecretStoreOptions {
  readonly directoryPath: string;
  readonly safeStorage: SafeStorageAdapter;
}

function validateRuntimeSecrets(value: unknown): RuntimeSecrets {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("desktop runtime secrets are invalid");
  }
  const secrets = value as Record<string, unknown>;
  if (secrets.version !== 1) {
    throw new Error("desktop runtime secret version is unsupported");
  }
  if (typeof secrets.secretKey !== "string" || !/^[A-Za-z0-9_-]{64}$/.test(secrets.secretKey)) {
    throw new Error("desktop runtime session secret is invalid");
  }
  if (
    typeof secrets.encryptionPepper !== "string" ||
    !/^[a-f0-9]{64}$/.test(secrets.encryptionPepper)
  ) {
    throw new Error("desktop runtime encryption pepper is invalid");
  }
  if (
    typeof secrets.keyEncryptionKey !== "string" ||
    !/^[a-f0-9]{64}$/.test(secrets.keyEncryptionKey)
  ) {
    throw new Error("desktop runtime key-encryption key is invalid");
  }
  return {
    version: 1,
    secretKey: secrets.secretKey,
    encryptionPepper: secrets.encryptionPepper,
    keyEncryptionKey: secrets.keyEncryptionKey,
  };
}

export class RuntimeSecretStore {
  readonly #store: SecureJsonStore<RuntimeSecrets>;
  #pending: Promise<RuntimeSecrets> | undefined;

  constructor(options: RuntimeSecretStoreOptions) {
    this.#store = new SecureJsonStore({
      directoryPath: options.directoryPath,
      fileName: "runtime-secrets.bin",
      safeStorage: options.safeStorage,
      validate: validateRuntimeSecrets,
    });
  }

  async loadOrCreate(): Promise<RuntimeSecrets> {
    if (this.#pending !== undefined) {
      return this.#pending;
    }
    const operation = (async () => {
      const stored = await this.#store.load();
      if (stored !== null) {
        return stored;
      }
      const created: RuntimeSecrets = {
        version: 1,
        secretKey: randomBytes(48).toString("base64url"),
        encryptionPepper: randomBytes(32).toString("hex"),
        keyEncryptionKey: randomBytes(32).toString("hex"),
      };
      await this.#store.save(created);
      return created;
    })();
    this.#pending = operation;
    try {
      return await operation;
    } finally {
      if (this.#pending === operation) {
        this.#pending = undefined;
      }
    }
  }
}
