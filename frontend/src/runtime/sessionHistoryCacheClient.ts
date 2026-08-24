import { HISTORY_CACHE_PROTOCOL_VERSION } from "./sessionHistoryCacheCore";

const REQUEST_TIMEOUT_MS = 250;
const MAX_PENDING_REQUESTS = 32;
const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/u;

type Operation = "get" | "put" | "delete";
interface PendingRequest {
  operation: Operation;
  timeout: ReturnType<typeof setTimeout>;
  resolve: (value: unknown[] | null) => void;
}
interface WorkerLike {
  port: MessagePort;
  onerror: ((event: ErrorEvent) => void) | null;
}
type WorkerFactory = () => WorkerLike;

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

export class SessionHistoryCacheClient {
  private worker: WorkerLike | null = null;
  private port: MessagePort | null = null;
  private unavailable = false;
  private disposed = false;
  private counter = 0;
  private readonly pending = new Map<string, PendingRequest>();

  constructor(private readonly workerFactory: WorkerFactory) {}

  private ensurePort(): MessagePort | null {
    if (this.disposed || this.unavailable) return null;
    if (this.port) return this.port;
    try {
      const worker = this.workerFactory();
      const port = worker.port;
      this.worker = worker;
      this.port = port;
      worker.onerror = (event) => {
        event.preventDefault();
        this.markUnavailable();
      };
      port.onmessage = (event) => this.receive(event.data);
      port.onmessageerror = () => this.markUnavailable();
      port.start();
      return port;
    } catch {
      this.markUnavailable();
      return null;
    }
  }

  private markUnavailable(): void {
    this.unavailable = true;
    this.failAll();
    if (this.worker) this.worker.onerror = null;
    if (this.port) {
      this.port.onmessage = null;
      this.port.onmessageerror = null;
      this.port.close();
    }
    this.worker = null;
    this.port = null;
  }

  private failAll(): void {
    for (const [requestId, request] of this.pending) {
      clearTimeout(request.timeout);
      this.pending.delete(requestId);
      request.resolve(null);
    }
  }

  private receive(value: unknown): void {
    if (
      !isRecord(value) ||
      value.version !== HISTORY_CACHE_PROTOCOL_VERSION ||
      typeof value.requestId !== "string"
    )
      return;
    const pending = this.pending.get(value.requestId);
    if (!pending) return;
    this.pending.delete(value.requestId);
    clearTimeout(pending.timeout);
    if (pending.operation === "get") {
      if (
        exactKeys(value, ["version", "requestId", "ok", "hit"]) &&
        value.ok === true &&
        value.hit === false
      ) {
        pending.resolve(null);
        return;
      }
      if (
        exactKeys(value, ["version", "requestId", "ok", "hit", "envelopes"]) &&
        value.ok === true &&
        value.hit === true &&
        Array.isArray(value.envelopes)
      ) {
        pending.resolve(value.envelopes);
        return;
      }
      pending.resolve(null);
      return;
    }
    pending.resolve(
      exactKeys(value, ["version", "requestId", "ok"]) && value.ok === true
        ? []
        : null,
    );
  }

  private request(
    operation: Operation,
    userId: string,
    sessionId: string,
    envelopes?: unknown[],
  ): Promise<unknown[] | null> {
    const port = this.ensurePort();
    if (!port || this.pending.size >= MAX_PENDING_REQUESTS)
      return Promise.resolve(null);
    const requestId = `cache-${Date.now().toString(36)}-${(++this.counter).toString(36)}`;
    if (!REQUEST_ID_PATTERN.test(requestId)) return Promise.resolve(null);
    return new Promise((resolve) => {
      const timeout = setTimeout(() => {
        if (!this.pending.delete(requestId)) return;
        resolve(null);
      }, REQUEST_TIMEOUT_MS);
      this.pending.set(requestId, { operation, timeout, resolve });
      try {
        port.postMessage({
          version: HISTORY_CACHE_PROTOCOL_VERSION,
          type: operation,
          requestId,
          userId,
          sessionId,
          ...(operation === "put" ? { envelopes } : {}),
        });
      } catch {
        clearTimeout(timeout);
        this.pending.delete(requestId);
        resolve(null);
      }
    });
  }

  async get(userId: string, sessionId: string): Promise<unknown[] | null> {
    return this.request("get", userId, sessionId);
  }
  async put(
    userId: string,
    sessionId: string,
    envelopes: unknown[],
  ): Promise<void> {
    await this.request("put", userId, sessionId, envelopes);
  }
  async delete(userId: string, sessionId: string): Promise<void> {
    await this.request("delete", userId, sessionId);
  }
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.markUnavailable();
  }
}

let sharedClient: SessionHistoryCacheClient | null | undefined;

export function getSessionHistoryCacheClient(): SessionHistoryCacheClient | null {
  if (sharedClient !== undefined) return sharedClient;
  if (
    typeof window === "undefined" ||
    typeof SharedWorker !== "function" ||
    window.yinshiDesktop !== undefined
  ) {
    sharedClient = null;
    return null;
  }
  sharedClient = new SessionHistoryCacheClient(
    () =>
      new SharedWorker(
        new URL("./sessionHistoryCache.shared-worker.ts", import.meta.url),
        { type: "module", name: "managed-session-history-v1" },
      ),
  );
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
