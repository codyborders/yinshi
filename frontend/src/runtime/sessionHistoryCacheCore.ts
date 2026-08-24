export const HISTORY_CACHE_PROTOCOL_VERSION = 1 as const;
export const HISTORY_CACHE_TTL_MS = 10 * 60 * 1_000;
export const HISTORY_CACHE_MAX_ENTRIES = 8;
export const HISTORY_CACHE_MAX_ENTRY_BYTES = 1_024 * 1_024;
export const HISTORY_CACHE_MAX_TOTAL_BYTES = 4 * 1_024 * 1_024;

const SESSION_ID_PATTERN = /^[0-9a-f]{32}$/u;
const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/u;

interface CacheEntry {
  envelopes: unknown[];
  bytes: number;
  expiresAt: number;
  lastUsed: number;
}

interface StoreOptions {
  now?: () => number;
  maxEntries?: number;
  maxEntryBytes?: number;
  maxTotalBytes?: number;
}

function exactKeys(value: Record<string, unknown>, keys: string[]): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return (
    actual.length === expected.length &&
    actual.every((key, index) => key === expected[index])
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isCanonicalHistoryCacheUserId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 256 &&
    value.trim() === value &&
    !/[\u0000-\u001f\u007f]/u.test(value)
  );
}

function validIdentity(value: Record<string, unknown>): boolean {
  return (
    value.version === HISTORY_CACHE_PROTOCOL_VERSION &&
    typeof value.requestId === "string" &&
    REQUEST_ID_PATTERN.test(value.requestId) &&
    isCanonicalHistoryCacheUserId(value.userId) &&
    typeof value.sessionId === "string" &&
    SESSION_ID_PATTERN.test(value.sessionId)
  );
}

function responseBase(request: Record<string, unknown>) {
  return {
    version: HISTORY_CACHE_PROTOCOL_VERSION,
    requestId: request.requestId as string,
  };
}

export class HistoryCacheStore {
  private readonly entries = new Map<string, CacheEntry>();
  private readonly now: () => number;
  private readonly maxEntries: number;
  private readonly maxEntryBytes: number;
  private readonly maxTotalBytes: number;
  private sequence = 0;
  totalBytes = 0;

  constructor(options: StoreOptions = {}) {
    this.now = options.now ?? Date.now;
    this.maxEntries = options.maxEntries ?? HISTORY_CACHE_MAX_ENTRIES;
    this.maxEntryBytes = options.maxEntryBytes ?? HISTORY_CACHE_MAX_ENTRY_BYTES;
    this.maxTotalBytes = options.maxTotalBytes ?? HISTORY_CACHE_MAX_TOTAL_BYTES;
  }

  private key(userId: string, sessionId: string): string {
    return `${userId.length}:${userId}${sessionId}`;
  }

  private purgeExpired(): void {
    const now = this.now();
    for (const [key, entry] of this.entries) {
      if (entry.expiresAt <= now) this.remove(key);
    }
  }

  private remove(key: string): void {
    const entry = this.entries.get(key);
    if (!entry) return;
    this.totalBytes -= entry.bytes;
    this.entries.delete(key);
  }

  get(userId: string, sessionId: string): unknown[] | null {
    this.purgeExpired();
    const entry = this.entries.get(this.key(userId, sessionId));
    if (!entry) return null;
    entry.lastUsed = ++this.sequence;
    return structuredClone(entry.envelopes);
  }

  put(userId: string, sessionId: string, envelopes: unknown[]): boolean {
    let serialized: string;
    try {
      serialized = JSON.stringify(envelopes);
    } catch {
      return false;
    }
    const bytes = new TextEncoder().encode(serialized).byteLength;
    if (bytes > this.maxEntryBytes || bytes > this.maxTotalBytes) return false;
    let copy: unknown[];
    try {
      copy = JSON.parse(serialized) as unknown[];
    } catch {
      return false;
    }
    this.purgeExpired();
    const key = this.key(userId, sessionId);
    this.remove(key);
    this.entries.set(key, {
      envelopes: copy,
      bytes,
      expiresAt: this.now() + HISTORY_CACHE_TTL_MS,
      lastUsed: ++this.sequence,
    });
    this.totalBytes += bytes;
    while (
      this.entries.size > this.maxEntries ||
      this.totalBytes > this.maxTotalBytes
    ) {
      let oldestKey: string | null = null;
      let oldestUse = Number.POSITIVE_INFINITY;
      for (const [candidateKey, entry] of this.entries) {
        if (entry.lastUsed < oldestUse) {
          oldestKey = candidateKey;
          oldestUse = entry.lastUsed;
        }
      }
      if (oldestKey === null) break;
      this.remove(oldestKey);
    }
    return this.entries.has(key);
  }

  delete(userId: string, sessionId: string): void {
    this.remove(this.key(userId, sessionId));
  }
}

export function processHistoryCacheRequest(
  store: HistoryCacheStore,
  value: unknown,
): Record<string, unknown> | null {
  if (
    !isRecord(value) ||
    typeof value.type !== "string" ||
    !validIdentity(value)
  )
    return null;
  const baseKeys = ["version", "type", "requestId", "userId", "sessionId"];
  if (value.type === "get") {
    if (!exactKeys(value, baseKeys)) return null;
    const envelopes = store.get(
      value.userId as string,
      value.sessionId as string,
    );
    return envelopes === null
      ? { ...responseBase(value), ok: true, hit: false }
      : { ...responseBase(value), ok: true, hit: true, envelopes };
  }
  if (value.type === "put") {
    if (
      !exactKeys(value, [...baseKeys, "envelopes"]) ||
      !Array.isArray(value.envelopes)
    )
      return null;
    if (
      !store.put(
        value.userId as string,
        value.sessionId as string,
        value.envelopes,
      )
    )
      return null;
    return { ...responseBase(value), ok: true };
  }
  if (value.type === "delete") {
    if (!exactKeys(value, baseKeys)) return null;
    store.delete(value.userId as string, value.sessionId as string);
    return { ...responseBase(value), ok: true };
  }
  return null;
}
