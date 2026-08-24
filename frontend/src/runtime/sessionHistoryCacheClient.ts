import { api } from "../api/client";

export const HISTORY_CACHE_VERSION = 1 as const;
export const HISTORY_CACHE_TTL_MS = 10 * 60 * 1_000;
export const HISTORY_CACHE_MAX_ENTRIES = 8;
export const HISTORY_CACHE_MAX_ENTRY_BYTES = 1_024 * 1_024;
export const HISTORY_CACHE_MAX_RECORD_BYTES = 1_500_000;
export const HISTORY_CACHE_MAX_TOTAL_BYTES = 4 * 1_024 * 1_024;
export const HISTORY_CACHE_STORAGE_PREFIX = "yinshi:session-history:v1:";
export const HISTORY_CACHE_READ_KEY_TTL_MS = 30_000;

const DEFAULT_KEY_FETCH_TIMEOUT_MS = 250;
const SESSION_ID_PATTERN = /^[0-9a-f]{32}$/u;
const KEY_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/u;
const BASE64URL_PATTERN = /^[A-Za-z0-9_-]+$/u;
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });

type FetchKey = () => Promise<unknown>;

export interface SessionHistoryCacheClientOptions {
  storage?: Storage | null;
  fetchKey?: FetchKey;
  cryptoProvider?: Crypto | null;
  now?: () => number;
  keyFetchTimeoutMs?: number;
}

interface ValidKeyResponse {
  version: 1;
  user_id: string;
  key_id: string;
  keyBytes: Uint8Array;
}

interface CachedKey {
  keyId: string;
  key: CryptoKey;
  loadedAtMs: number;
}

interface ReadKeyResult {
  cachedKey: CachedKey | null;
  refreshed: boolean;
}

interface StoredRecord {
  version: 1;
  key_id: string;
  iv: string;
  ciphertext: string;
  stored_at: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactKeys(
  value: Record<string, unknown>,
  expected: string[],
): boolean {
  const actual = Object.keys(value).sort();
  const keys = [...expected].sort();
  return (
    actual.length === keys.length &&
    actual.every((key, index) => key === keys[index])
  );
}

function isValidUserId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 256 &&
    value.trim() === value &&
    !/[\u0000-\u001f\u007f]/u.test(value)
  );
}

function encodeBase64url(value: Uint8Array): string {
  let binary = "";
  for (let offset = 0; offset < value.length; offset += 0x8000) {
    binary += String.fromCharCode(...value.subarray(offset, offset + 0x8000));
  }
  return btoa(binary)
    .replace(/\+/gu, "-")
    .replace(/\//gu, "_")
    .replace(/=+$/u, "");
}

function decodeCanonicalBase64url(
  value: unknown,
  expectedBytes?: number,
): Uint8Array | null {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    !BASE64URL_PATTERN.test(value) ||
    value.length % 4 === 1
  ) {
    return null;
  }
  try {
    const padding = "=".repeat((4 - (value.length % 4)) % 4);
    const binary = atob(
      value.replace(/-/gu, "+").replace(/_/gu, "/") + padding,
    );
    const bytes = Uint8Array.from(binary, (character) =>
      character.charCodeAt(0),
    );
    if (expectedBytes !== undefined && bytes.byteLength !== expectedBytes)
      return null;
    return encodeBase64url(bytes) === value ? bytes : null;
  } catch {
    return null;
  }
}

function toArrayBuffer(value: Uint8Array): ArrayBuffer {
  return value.buffer.slice(
    value.byteOffset,
    value.byteOffset + value.byteLength,
  ) as ArrayBuffer;
}

function validateKeyResponse(
  value: unknown,
  userId: string,
): ValidKeyResponse | null {
  if (
    !isRecord(value) ||
    !exactKeys(value, ["version", "user_id", "key_id", "key"]) ||
    value.version !== HISTORY_CACHE_VERSION ||
    value.user_id !== userId ||
    !isValidUserId(value.user_id) ||
    typeof value.key_id !== "string" ||
    !KEY_ID_PATTERN.test(value.key_id)
  ) {
    return null;
  }
  const keyBytes = decodeCanonicalBase64url(value.key, 32);
  return keyBytes === null
    ? null
    : {
        version: HISTORY_CACHE_VERSION,
        user_id: userId,
        key_id: value.key_id,
        keyBytes,
      };
}

function validateIdentity(userId: string, sessionId: string): boolean {
  return isValidUserId(userId) && SESSION_ID_PATTERN.test(sessionId);
}

function cacheKey(userId: string, sessionId: string): string {
  return `${HISTORY_CACHE_STORAGE_PREFIX}${encodeURIComponent(userId)}:${sessionId}`;
}

function associatedData(
  userId: string,
  sessionId: string,
  keyId: string,
): Uint8Array {
  return encoder.encode(
    `yinshi-history-cache:${HISTORY_CACHE_VERSION}:${keyId}:${userId.length}:${userId}:${sessionId}`,
  );
}

function parseStoredRecord(serialized: string): StoredRecord | null {
  if (encoder.encode(serialized).byteLength > HISTORY_CACHE_MAX_RECORD_BYTES)
    return null;
  try {
    const value: unknown = JSON.parse(serialized);
    if (
      !isRecord(value) ||
      !exactKeys(value, [
        "version",
        "key_id",
        "iv",
        "ciphertext",
        "stored_at",
      ]) ||
      value.version !== HISTORY_CACHE_VERSION ||
      typeof value.key_id !== "string" ||
      !KEY_ID_PATTERN.test(value.key_id) ||
      decodeCanonicalBase64url(value.iv, 12) === null ||
      decodeCanonicalBase64url(value.ciphertext) === null ||
      typeof value.stored_at !== "number" ||
      !Number.isSafeInteger(value.stored_at) ||
      value.stored_at < 0
    ) {
      return null;
    }
    return value as unknown as StoredRecord;
  } catch {
    return null;
  }
}

function availableSessionStorage(): Storage | null {
  if (typeof window === "undefined" || window.yinshiDesktop !== undefined)
    return null;
  try {
    void window.sessionStorage.length;
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function isSessionHistoryCacheAvailable(): boolean {
  return (
    availableSessionStorage() !== null &&
    typeof globalThis.crypto?.subtle !== "undefined"
  );
}

export class SessionHistoryCacheClient {
  private readonly storage: Storage | null;
  private readonly fetchKey: FetchKey;
  private readonly cryptoProvider: Crypto | null;
  private readonly now: () => number;
  private readonly keyFetchTimeoutMs: number;
  private readonly readKeys = new Map<string, CachedKey>();
  private readonly readKeyRequests = new Map<
    string,
    Promise<CachedKey | null>
  >();
  private readonly mutationEpochs = new Map<string, number>();

  constructor(options: SessionHistoryCacheClientOptions = {}) {
    this.storage =
      options.storage === undefined
        ? availableSessionStorage()
        : options.storage;
    this.fetchKey =
      options.fetchKey ??
      (() => api.get<unknown>("/api/runtime/history-cache-key"));
    this.cryptoProvider = options.cryptoProvider ?? globalThis.crypto ?? null;
    this.now = options.now ?? Date.now;
    this.keyFetchTimeoutMs =
      options.keyFetchTimeoutMs ?? DEFAULT_KEY_FETCH_TIMEOUT_MS;
  }

  private remove(userId: string, sessionId: string): void {
    if (!this.storage || !validateIdentity(userId, sessionId)) return;
    try {
      this.storage.removeItem(cacheKey(userId, sessionId));
    } catch {
      return;
    }
  }

  private async fetchFreshKey(userId: string): Promise<CachedKey | null> {
    if (!this.cryptoProvider?.subtle) return null;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      const response = await Promise.race([
        this.fetchKey(),
        new Promise<null>((resolve) => {
          timeout = setTimeout(() => resolve(null), this.keyFetchTimeoutMs);
        }),
      ]);
      const valid = validateKeyResponse(response, userId);
      if (valid === null) return null;
      const keyBytes = valid.keyBytes;
      try {
        const key = await this.cryptoProvider.subtle.importKey(
          "raw",
          toArrayBuffer(keyBytes),
          { name: "AES-GCM" },
          false,
          ["encrypt", "decrypt"],
        );
        return { keyId: valid.key_id, key, loadedAtMs: this.now() };
      } finally {
        keyBytes.fill(0);
      }
    } catch {
      return null;
    } finally {
      if (timeout !== undefined) clearTimeout(timeout);
    }
  }

  private async getReadKey(
    userId: string,
    forceRefresh = false,
  ): Promise<ReadKeyResult> {
    const existing = this.readKeys.get(userId);
    if (!forceRefresh && existing) {
      const ageMs = this.now() - existing.loadedAtMs;
      if (ageMs >= 0 && ageMs < HISTORY_CACHE_READ_KEY_TTL_MS) {
        return { cachedKey: existing, refreshed: false };
      }
      this.readKeys.delete(userId);
    }

    let pending = forceRefresh ? undefined : this.readKeyRequests.get(userId);
    if (!pending) {
      pending = this.fetchFreshKey(userId);
      this.readKeyRequests.set(userId, pending);
    }
    const key = await pending;
    if (this.readKeyRequests.get(userId) === pending) {
      this.readKeyRequests.delete(userId);
      if (key === null) {
        this.readKeys.delete(userId);
      } else {
        this.readKeys.set(userId, key);
      }
    }
    return { cachedKey: key, refreshed: true };
  }

  private currentEpoch(identity: string): number {
    return this.mutationEpochs.get(identity) ?? 0;
  }

  private incrementEpoch(identity: string): void {
    this.mutationEpochs.set(identity, this.currentEpoch(identity) + 1);
  }

  private enforceBounds(): void {
    if (!this.storage) return;
    const entries: Array<{ key: string; bytes: number; storedAt: number }> = [];
    try {
      for (let index = 0; index < this.storage.length; index += 1) {
        const key = this.storage.key(index);
        if (!key?.startsWith(HISTORY_CACHE_STORAGE_PREFIX)) continue;
        const serialized = this.storage.getItem(key);
        if (serialized === null) continue;
        const record = parseStoredRecord(serialized);
        entries.push({
          key,
          bytes: encoder.encode(serialized).byteLength,
          storedAt: record?.stored_at ?? 0,
        });
      }
      entries.sort(
        (left, right) =>
          left.storedAt - right.storedAt || left.key.localeCompare(right.key),
      );
      let totalBytes = entries.reduce((total, entry) => total + entry.bytes, 0);
      while (
        entries.length > HISTORY_CACHE_MAX_ENTRIES ||
        totalBytes > HISTORY_CACHE_MAX_TOTAL_BYTES
      ) {
        const removed = entries.shift();
        if (!removed) break;
        this.storage.removeItem(removed.key);
        totalBytes -= removed.bytes;
      }
    } catch {
      return;
    }
  }

  async get(userId: string, sessionId: string): Promise<unknown[] | null> {
    if (!this.storage || !validateIdentity(userId, sessionId)) return null;
    let serialized: string | null;
    try {
      serialized = this.storage.getItem(cacheKey(userId, sessionId));
    } catch {
      return null;
    }
    if (serialized === null) return null;
    const record = parseStoredRecord(serialized);
    if (record === null) {
      this.remove(userId, sessionId);
      return null;
    }
    let readKey = await this.getReadKey(userId);
    if (readKey.cachedKey === null) return null;
    if (readKey.cachedKey.keyId !== record.key_id && !readKey.refreshed) {
      readKey = await this.getReadKey(userId, true);
    }
    const cachedKey = readKey.cachedKey;
    if (cachedKey === null) return null;
    if (cachedKey.keyId !== record.key_id) {
      this.remove(userId, sessionId);
      return null;
    }
    const iv = decodeCanonicalBase64url(record.iv, 12);
    const ciphertext = decodeCanonicalBase64url(record.ciphertext);
    if (iv === null || ciphertext === null) {
      this.remove(userId, sessionId);
      return null;
    }
    try {
      const decrypted = await this.cryptoProvider?.subtle.decrypt(
        {
          name: "AES-GCM",
          iv: toArrayBuffer(iv),
          additionalData: toArrayBuffer(
            associatedData(userId, sessionId, record.key_id),
          ),
          tagLength: 128,
        },
        cachedKey.key,
        toArrayBuffer(ciphertext),
      );
      if (!decrypted || decrypted.byteLength > HISTORY_CACHE_MAX_ENTRY_BYTES)
        throw new Error("oversized");
      const plaintext = decoder.decode(decrypted);
      const value: unknown = JSON.parse(plaintext);
      if (
        !isRecord(value) ||
        !exactKeys(value, ["createdAtMs", "envelopes"]) ||
        typeof value.createdAtMs !== "number" ||
        !Number.isSafeInteger(value.createdAtMs) ||
        value.createdAtMs < 0 ||
        value.createdAtMs > this.now() ||
        this.now() - value.createdAtMs >= HISTORY_CACHE_TTL_MS ||
        !Array.isArray(value.envelopes)
      ) {
        throw new Error("invalid plaintext");
      }
      return value.envelopes;
    } catch {
      this.remove(userId, sessionId);
      return null;
    }
  }

  async put(
    userId: string,
    sessionId: string,
    envelopes: unknown[],
  ): Promise<void> {
    if (
      !this.storage ||
      !validateIdentity(userId, sessionId) ||
      !Array.isArray(envelopes)
    )
      return;
    const identity = cacheKey(userId, sessionId);
    this.incrementEpoch(identity);
    const mutationEpoch = this.currentEpoch(identity);
    this.remove(userId, sessionId);
    const cachedKey = await this.fetchFreshKey(userId);
    if (cachedKey === null || !this.cryptoProvider?.subtle) return;
    let plaintext: Uint8Array;
    try {
      plaintext = encoder.encode(
        JSON.stringify({ createdAtMs: this.now(), envelopes }),
      );
    } catch {
      return;
    }
    if (plaintext.byteLength > HISTORY_CACHE_MAX_ENTRY_BYTES) return;
    try {
      const iv = this.cryptoProvider.getRandomValues(new Uint8Array(12));
      const encrypted = await this.cryptoProvider.subtle.encrypt(
        {
          name: "AES-GCM",
          iv: toArrayBuffer(iv),
          additionalData: toArrayBuffer(
            associatedData(userId, sessionId, cachedKey.keyId),
          ),
          tagLength: 128,
        },
        cachedKey.key,
        toArrayBuffer(plaintext),
      );
      const record: StoredRecord = {
        version: HISTORY_CACHE_VERSION,
        key_id: cachedKey.keyId,
        iv: encodeBase64url(iv),
        ciphertext: encodeBase64url(new Uint8Array(encrypted)),
        stored_at: this.now(),
      };
      const serialized = JSON.stringify(record);
      if (
        encoder.encode(serialized).byteLength > HISTORY_CACHE_MAX_RECORD_BYTES
      )
        return;
      if (this.currentEpoch(identity) !== mutationEpoch) return;
      this.storage.setItem(identity, serialized);
      this.enforceBounds();
    } catch {
      return;
    }
  }

  async delete(userId: string, sessionId: string): Promise<void> {
    if (!validateIdentity(userId, sessionId)) return;
    this.incrementEpoch(cacheKey(userId, sessionId));
    this.remove(userId, sessionId);
  }

  dispose(): void {
    this.readKeys.clear();
    this.readKeyRequests.clear();
  }
}

let sharedClient: SessionHistoryCacheClient | null | undefined;

export function getSessionHistoryCacheClient(): SessionHistoryCacheClient | null {
  if (sharedClient !== undefined) return sharedClient;
  if (!isSessionHistoryCacheAvailable()) {
    sharedClient = null;
    return null;
  }
  sharedClient = new SessionHistoryCacheClient();
  return sharedClient;
}

export function invalidateSessionHistoryCache(
  userId: string,
  sessionId: string,
): void {
  const client = getSessionHistoryCacheClient();
  if (!client) return;
  void client.delete(userId, sessionId).catch(() => undefined);
}
