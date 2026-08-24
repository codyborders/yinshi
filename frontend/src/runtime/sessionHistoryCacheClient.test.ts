import { afterEach, describe, expect, it, vi } from "vitest";

import {
  HISTORY_CACHE_MAX_ENTRY_BYTES,
  HISTORY_CACHE_STORAGE_PREFIX,
  SessionHistoryCacheClient,
} from "./sessionHistoryCacheClient";

const USER = "user-one";
const OTHER_USER = "user-two";
const SESSION = "0123456789abcdef0123456789abcdef";
const MARKER = "private conversation marker";

function base64url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/gu, "-")
    .replace(/\//gu, "_")
    .replace(/=+$/u, "");
}

function keyResponse(userId: string, fill: number) {
  return {
    version: 1,
    user_id: userId,
    key_id: fill.toString(16).padStart(16, "0"),
    key: base64url(new Uint8Array(32).fill(fill)),
  };
}

function storageKey(userId: string, sessionId: string): string {
  return `${HISTORY_CACHE_STORAGE_PREFIX}${encodeURIComponent(userId)}:${sessionId}`;
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}

function clientFor(
  userId = USER,
  fill = 1,
  options: Partial<
    ConstructorParameters<typeof SessionHistoryCacheClient>[0]
  > = {},
) {
  const fetchKey = vi.fn().mockResolvedValue(keyResponse(userId, fill));
  return {
    fetchKey,
    client: new SessionHistoryCacheClient({
      storage: sessionStorage,
      fetchKey,
      ...options,
    }),
  };
}

afterEach(() => {
  sessionStorage.clear();
  vi.useRealTimers();
});

describe("SessionHistoryCacheClient", () => {
  it("stores only bounded AES-GCM ciphertext and decrypts it after refresh", async () => {
    const first = clientFor();
    const envelopes = [{ data: MARKER }];

    await first.client.put(USER, SESSION, envelopes);

    const stored = sessionStorage.getItem(storageKey(USER, SESSION));
    expect(stored).not.toBeNull();
    const record = JSON.parse(stored as string) as Record<string, unknown>;
    expect(Object.keys(record).sort()).toEqual([
      "ciphertext",
      "iv",
      "key_id",
      "stored_at",
      "version",
    ]);
    expect(record.version).toBe(1);
    expect(record.key_id).toBe("0000000000000001");
    expect(stored).not.toContain(keyResponse(USER, 1).key);
    expect(stored).not.toContain(MARKER);

    const refreshed = clientFor();
    await expect(refreshed.client.get(USER, SESSION)).resolves.toEqual(
      envelopes,
    );
    expect(refreshed.fetchKey).toHaveBeenCalledTimes(1);
  });

  it("accepts a cache key response that arrives after 500 ms", async () => {
    const envelopes = [{ data: MARKER }];
    await clientFor().client.put(USER, SESSION, envelopes);
    const fetchKey = vi.fn(
      () =>
        new Promise<unknown>((resolve) => {
          setTimeout(() => resolve(keyResponse(USER, 1)), 500);
        }),
    );
    const refreshed = new SessionHistoryCacheClient({
      storage: sessionStorage,
      fetchKey,
    });

    await expect(refreshed.get(USER, SESSION)).resolves.toEqual(envelopes);
    expect(fetchKey).toHaveBeenCalledTimes(1);
  });

  it("fetches and validates a fresh key for every write after a cookie switch", async () => {
    const fetchKey = vi
      .fn()
      .mockResolvedValueOnce(keyResponse(USER, 1))
      .mockResolvedValueOnce(keyResponse(OTHER_USER, 2));
    const client = new SessionHistoryCacheClient({
      storage: sessionStorage,
      fetchKey,
    });

    await client.put(USER, SESSION, [{ data: "before switch" }]);
    const previous = sessionStorage.getItem(storageKey(USER, SESSION));
    expect(JSON.parse(previous as string).key_id).toBe("0000000000000001");

    await client.put(USER, SESSION, [{ data: "after switch" }]);

    expect(fetchKey).toHaveBeenCalledTimes(2);
    expect(sessionStorage.getItem(storageKey(USER, SESSION))).toBe(previous);
  });

  it("preserves current ciphertext when replacement preparation fails", async () => {
    const seeded = clientFor();
    await seeded.client.put(USER, SESSION, [{ data: "current" }]);
    const previous = sessionStorage.getItem(storageKey(USER, SESSION));
    expect(previous).not.toBeNull();

    const failedKey = new SessionHistoryCacheClient({
      storage: sessionStorage,
      fetchKey: vi.fn().mockRejectedValue(new Error("offline")),
    });
    await failedKey.put(USER, SESSION, [{ data: "replacement" }]);
    expect(sessionStorage.getItem(storageKey(USER, SESSION))).toBe(previous);

    vi.useFakeTimers();
    const timedOutKey = new SessionHistoryCacheClient({
      storage: sessionStorage,
      fetchKey: vi.fn(() => new Promise(() => undefined)),
      keyFetchTimeoutMs: 25,
    });
    const timedOutPut = timedOutKey.put(USER, SESSION, [
      { data: "replacement" },
    ]);
    await vi.advanceTimersByTimeAsync(26);
    await timedOutPut;
    vi.useRealTimers();
    expect(sessionStorage.getItem(storageKey(USER, SESSION))).toBe(previous);

    await seeded.client.put(USER, SESSION, [
      "x".repeat(HISTORY_CACHE_MAX_ENTRY_BYTES),
    ]);
    expect(sessionStorage.getItem(storageKey(USER, SESSION))).toBe(previous);
  });

  it("preserves current ciphertext when encryption or storage replacement fails", async () => {
    const seeded = clientFor();
    await seeded.client.put(USER, SESSION, [{ data: "current" }]);
    const previous = sessionStorage.getItem(storageKey(USER, SESSION));
    expect(previous).not.toBeNull();

    const nativeCrypto = globalThis.crypto;
    const subtle = new Proxy(nativeCrypto.subtle, {
      get(target, property) {
        if (property === "encrypt") {
          return vi.fn().mockRejectedValue(new Error("encryption failed"));
        }
        const value: unknown = Reflect.get(target, property, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const failedEncryption = new SessionHistoryCacheClient({
      storage: sessionStorage,
      fetchKey: vi.fn().mockResolvedValue(keyResponse(USER, 1)),
      cryptoProvider: {
        subtle,
        getRandomValues: nativeCrypto.getRandomValues.bind(nativeCrypto),
      } as Crypto,
    });
    await failedEncryption.put(USER, SESSION, [{ data: "replacement" }]);
    expect(sessionStorage.getItem(storageKey(USER, SESSION))).toBe(previous);

    const fullStorage: Storage = {
      length: sessionStorage.length,
      clear: vi.fn(() => sessionStorage.clear()),
      getItem: vi.fn((key) => sessionStorage.getItem(key)),
      key: vi.fn((index) => sessionStorage.key(index)),
      removeItem: vi.fn((key) => sessionStorage.removeItem(key)),
      setItem: vi.fn(() => {
        throw new DOMException("quota", "QuotaExceededError");
      }),
    };
    const failedStorage = new SessionHistoryCacheClient({
      storage: fullStorage,
      fetchKey: vi.fn().mockResolvedValue(keyResponse(USER, 1)),
    });
    await failedStorage.put(USER, SESSION, [{ data: "replacement" }]);
    expect(sessionStorage.getItem(storageKey(USER, SESSION))).toBe(previous);
  });

  it("makes deletion final against an in-flight write", async () => {
    const pendingKey = deferred<unknown>();
    const fetchKey = vi.fn(() => pendingKey.promise);
    const client = new SessionHistoryCacheClient({
      storage: sessionStorage,
      fetchKey,
    });

    const pendingWrite = client.put(USER, SESSION, [{ data: "late write" }]);
    await vi.waitFor(() => expect(fetchKey).toHaveBeenCalledTimes(1));
    await client.delete(USER, SESSION);
    pendingKey.resolve(keyResponse(USER, 1));
    await pendingWrite;

    expect(sessionStorage.getItem(storageKey(USER, SESSION))).toBeNull();
  });

  it("keeps newer ciphertext when an older write encrypts last", async () => {
    const firstEncryptionStarted = deferred<void>();
    const allowFirstEncryption = deferred<void>();
    const nativeCrypto = globalThis.crypto;
    let encryptionCall = 0;
    const subtle = new Proxy(nativeCrypto.subtle, {
      get(target, property) {
        if (property === "encrypt") {
          return async (...args: unknown[]) => {
            encryptionCall += 1;
            if (encryptionCall === 1) {
              firstEncryptionStarted.resolve();
              await allowFirstEncryption.promise;
            }
            return Reflect.apply(
              target.encrypt,
              target,
              args,
            ) as Promise<ArrayBuffer>;
          };
        }
        const value: unknown = Reflect.get(target, property, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const client = new SessionHistoryCacheClient({
      storage: sessionStorage,
      fetchKey: vi.fn().mockResolvedValue(keyResponse(USER, 1)),
      cryptoProvider: {
        subtle,
        getRandomValues: nativeCrypto.getRandomValues.bind(nativeCrypto),
      } as Crypto,
    });

    const olderWrite = client.put(USER, SESSION, [{ data: "older" }]);
    await firstEncryptionStarted.promise;
    await client.put(USER, SESSION, [{ data: "newer" }]);
    allowFirstEncryption.resolve();
    await olderWrite;

    await expect(client.get(USER, SESSION)).resolves.toEqual([
      { data: "newer" },
    ]);
  });

  it("keeps deletion final while encryption is in flight", async () => {
    const encryptionStarted = deferred<void>();
    const allowEncryption = deferred<void>();
    const nativeCrypto = globalThis.crypto;
    const subtle = new Proxy(nativeCrypto.subtle, {
      get(target, property) {
        if (property === "encrypt") {
          return async (...args: unknown[]) => {
            encryptionStarted.resolve();
            await allowEncryption.promise;
            return Reflect.apply(
              target.encrypt,
              target,
              args,
            ) as Promise<ArrayBuffer>;
          };
        }
        const value: unknown = Reflect.get(target, property, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const cryptoProvider = {
      subtle,
      getRandomValues: nativeCrypto.getRandomValues.bind(nativeCrypto),
    } as Crypto;
    const client = new SessionHistoryCacheClient({
      storage: sessionStorage,
      fetchKey: vi.fn().mockResolvedValue(keyResponse(USER, 1)),
      cryptoProvider,
    });

    const pendingWrite = client.put(USER, SESSION, [
      { data: "late encrypted write" },
    ]);
    await encryptionStarted.promise;
    await client.delete(USER, SESSION);
    allowEncryption.resolve();
    await pendingWrite;

    expect(sessionStorage.getItem(storageKey(USER, SESSION))).toBeNull();
  });

  it("expires read keys within thirty seconds", async () => {
    const now = vi.fn(() => 1_000);
    const writer = clientFor(USER, 1, { now });
    await writer.client.put(USER, SESSION, [{ data: "cached" }]);
    const reader = clientFor(USER, 1, { now });

    await expect(reader.client.get(USER, SESSION)).resolves.toEqual([
      { data: "cached" },
    ]);
    now.mockReturnValue(30_999);
    await expect(reader.client.get(USER, SESSION)).resolves.toEqual([
      { data: "cached" },
    ]);
    expect(reader.fetchKey).toHaveBeenCalledTimes(1);

    now.mockReturnValue(31_000);
    await expect(reader.client.get(USER, SESSION)).resolves.toEqual([
      { data: "cached" },
    ]);
    expect(reader.fetchKey).toHaveBeenCalledTimes(2);
  });

  it("refreshes a mismatched read key and writes only the rotated key", async () => {
    const now = vi.fn(() => 1_000);
    let fill = 1;
    const fetchKey = vi.fn(() => Promise.resolve(keyResponse(USER, fill)));
    const client = new SessionHistoryCacheClient({
      storage: sessionStorage,
      fetchKey,
      now,
    });

    await client.put(USER, SESSION, [{ data: "old key" }]);
    await expect(client.get(USER, SESSION)).resolves.toEqual([
      { data: "old key" },
    ]);
    fill = 2;

    await client.put(USER, SESSION, [{ data: "new key" }]);
    const rotatedRecord = JSON.parse(
      sessionStorage.getItem(storageKey(USER, SESSION)) as string,
    ) as Record<string, unknown>;
    expect(rotatedRecord.key_id).toBe("0000000000000002");
    await expect(client.get(USER, SESSION)).resolves.toEqual([
      { data: "new key" },
    ]);
    expect(fetchKey).toHaveBeenCalledTimes(4);
  });

  it("fails closed for copied cross-user ciphertext", async () => {
    const first = clientFor(USER, 1);
    await first.client.put(USER, SESSION, [{ data: MARKER }]);
    const copied = sessionStorage.getItem(storageKey(USER, SESSION));
    expect(copied).not.toBeNull();
    const copiedRecord = JSON.parse(copied as string) as Record<
      string,
      unknown
    >;
    copiedRecord.key_id = "0000000000000002";
    sessionStorage.setItem(
      storageKey(OTHER_USER, SESSION),
      JSON.stringify(copiedRecord),
    );

    const second = clientFor(OTHER_USER, 2);
    await expect(second.client.get(OTHER_USER, SESSION)).resolves.toBeNull();
    expect(sessionStorage.getItem(storageKey(OTHER_USER, SESSION))).toBeNull();
  });

  it("deletes corrupt, expired, malformed, oversized and wrong-key records", async () => {
    const now = vi.fn(() => 1_000_000);
    const seeded = clientFor(USER, 1, { now });
    await seeded.client.put(USER, SESSION, [{ data: "valid" }]);
    const valid = sessionStorage.getItem(storageKey(USER, SESSION)) as string;

    const cases = [
      "not-json",
      JSON.stringify({ version: 1 }),
      valid.replace('"version":1', '"version":2'),
      valid.replace(/"ciphertext":"[^"]+/u, '"ciphertext":"AAAA'),
      "x".repeat(1_500_001),
    ];
    for (const value of cases) {
      sessionStorage.setItem(storageKey(USER, SESSION), value);
      const candidate = clientFor(USER, 1, { now });
      await expect(candidate.client.get(USER, SESSION)).resolves.toBeNull();
      expect(sessionStorage.getItem(storageKey(USER, SESSION))).toBeNull();
    }

    sessionStorage.setItem(storageKey(USER, SESSION), valid);
    now.mockReturnValue(1_000_000 + 10 * 60_000 + 1);
    const expired = clientFor(USER, 1, { now });
    await expect(expired.client.get(USER, SESSION)).resolves.toBeNull();
    expect(sessionStorage.getItem(storageKey(USER, SESSION))).toBeNull();

    now.mockReturnValue(1_000_000);
    sessionStorage.setItem(storageKey(USER, SESSION), valid);
    const wrongKeyFetch = vi.fn().mockResolvedValue({
      ...keyResponse(USER, 2),
      key_id: "0000000000000001",
    });
    const wrongKey = new SessionHistoryCacheClient({
      storage: sessionStorage,
      fetchKey: wrongKeyFetch,
      now,
    });
    await expect(wrongKey.get(USER, SESSION)).resolves.toBeNull();
    expect(sessionStorage.getItem(storageKey(USER, SESSION))).toBeNull();
  });

  it("rejects malformed key responses, user mismatches and key fetch failures", async () => {
    const invalidResponses: unknown[] = [
      null,
      { ...keyResponse(USER, 1), extra: true },
      { ...keyResponse(USER, 1), version: 2 },
      { ...keyResponse(USER, 1), user_id: OTHER_USER },
      { ...keyResponse(USER, 1), key_id: "x".repeat(65) },
      { ...keyResponse(USER, 1), key: "A".repeat(42) },
    ];
    for (const response of invalidResponses) {
      const client = new SessionHistoryCacheClient({
        storage: sessionStorage,
        fetchKey: vi.fn().mockResolvedValue(response),
      });
      await expect(client.get(USER, SESSION)).resolves.toBeNull();
      await expect(client.put(USER, SESSION, [])).resolves.toBeUndefined();
      expect(sessionStorage.getItem(storageKey(USER, SESSION))).toBeNull();
    }

    const seeded = clientFor();
    await seeded.client.put(USER, SESSION, [{ data: "survives outage" }]);
    const failed = new SessionHistoryCacheClient({
      storage: sessionStorage,
      fetchKey: vi.fn().mockRejectedValue(new Error("offline")),
    });
    await expect(failed.get(USER, SESSION)).resolves.toBeNull();
    expect(sessionStorage.getItem(storageKey(USER, SESSION))).not.toBeNull();
  });

  it("bounds key fetch latency and treats unavailable or full storage as a miss", async () => {
    vi.useFakeTimers();
    const timedOut = new SessionHistoryCacheClient({
      storage: sessionStorage,
      fetchKey: vi.fn(() => new Promise(() => undefined)),
      keyFetchTimeoutMs: 25,
    });
    const pending = timedOut.put(USER, SESSION, []);
    await vi.advanceTimersByTimeAsync(26);
    await expect(pending).resolves.toBeUndefined();
    vi.useRealTimers();

    const unavailable = new SessionHistoryCacheClient({
      storage: null,
      fetchKey: vi.fn().mockResolvedValue(keyResponse(USER, 1)),
    });
    await expect(unavailable.get(USER, SESSION)).resolves.toBeNull();
    await expect(unavailable.put(USER, SESSION, [])).resolves.toBeUndefined();

    const fullStorage: Storage = {
      length: 0,
      clear: vi.fn(),
      getItem: vi.fn(() => null),
      key: vi.fn(() => null),
      removeItem: vi.fn(),
      setItem: vi.fn(() => {
        throw new DOMException("quota", "QuotaExceededError");
      }),
    };
    const quota = new SessionHistoryCacheClient({
      storage: fullStorage,
      fetchKey: vi.fn().mockResolvedValue(keyResponse(USER, 1)),
    });
    await expect(quota.put(USER, SESSION, [])).resolves.toBeUndefined();
    await expect(quota.get(USER, SESSION)).resolves.toBeNull();
  });

  it("enforces plaintext, entry-count and aggregate bounds", async () => {
    const bounded = clientFor();
    await bounded.client.put(USER, SESSION, [
      "x".repeat(HISTORY_CACHE_MAX_ENTRY_BYTES),
    ]);
    expect(sessionStorage.getItem(storageKey(USER, SESSION))).toBeNull();

    for (let index = 0; index < 9; index += 1) {
      const sessionId = index.toString(16).padStart(32, "0");
      await bounded.client.put(USER, sessionId, [{ index }]);
    }
    const keys = Object.keys(sessionStorage).filter((key) =>
      key.startsWith(HISTORY_CACHE_STORAGE_PREFIX),
    );
    expect(keys).toHaveLength(8);
    expect(sessionStorage.getItem(storageKey(USER, "0".repeat(32)))).toBeNull();

    const aggregate = clientFor();
    for (let index = 10; index < 15; index += 1) {
      await aggregate.client.put(USER, index.toString(16).padStart(32, "0"), [
        { data: "y".repeat(700_000) },
      ]);
    }
    const aggregateKeys = Object.keys(sessionStorage).filter((key) =>
      key.startsWith(HISTORY_CACHE_STORAGE_PREFIX),
    );
    const aggregateBytes = aggregateKeys.reduce(
      (total, key) =>
        total +
        new TextEncoder().encode(sessionStorage.getItem(key) ?? "").byteLength,
      0,
    );
    expect(aggregateBytes).toBeLessThanOrEqual(4 * 1024 * 1024);
    expect(aggregateKeys.length).toBeLessThanOrEqual(8);
  });
});
