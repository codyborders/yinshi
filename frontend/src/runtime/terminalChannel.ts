import type { RuntimeTransport } from "./runtimeTransport";

const RESOURCE_ID_PATTERN = /^[0-9a-f]{32}$/;
const TERMINAL_EVENT_TYPES = new Set([
  "error",
  "terminal_data",
  "terminal_exit",
  "terminal_ready",
]);

export interface RuntimeTerminalOptions {
  readonly cols: number;
  readonly rows: number;
  readonly signal?: AbortSignal;
  readonly pollDelayMs?: number;
}

export interface RuntimeTerminalEvent {
  readonly type: string;
  readonly [key: string]: unknown;
}

export interface RuntimeTerminalChannel {
  readonly id: string;
  events(): AsyncGenerator<RuntimeTerminalEvent>;
  sendInput(data: string): Promise<void>;
  resize(cols: number, rows: number): Promise<void>;
  restart(): Promise<void>;
  close(): Promise<void>;
}

function validateResourceId(value: unknown, field: string): string {
  if (typeof value !== "string" || !RESOURCE_ID_PATTERN.test(value)) {
    throw new Error(`Terminal ${field} is invalid`);
  }
  return value;
}

function validateSize(cols: number, rows: number): void {
  if (!Number.isSafeInteger(cols) || cols < 2 || cols > 500) {
    throw new Error("Terminal cols must be between 2 and 500");
  }
  if (!Number.isSafeInteger(rows) || rows < 2 || rows > 500) {
    throw new Error("Terminal rows must be between 2 and 500");
  }
}

function validateEvent(value: unknown): RuntimeTerminalEvent {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Terminal event is invalid");
  }
  const event = value as Record<string, unknown>;
  if (typeof event.type !== "string" || !TERMINAL_EVENT_TYPES.has(event.type)) {
    throw new Error("Terminal event type is invalid");
  }
  if (event.type === "terminal_data" && typeof event.data !== "string") {
    throw new Error("Terminal output data is invalid");
  }
  return event as RuntimeTerminalEvent;
}

function abortError(): DOMException {
  return new DOMException("Terminal polling aborted", "AbortError");
}

async function delay(
  milliseconds: number,
  signals: readonly (AbortSignal | undefined)[],
): Promise<void> {
  if (signals.some((signal) => signal?.aborted)) throw abortError();
  if (milliseconds === 0) return;
  await new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(resolve, milliseconds);
    const abort = () => {
      window.clearTimeout(timer);
      reject(abortError());
    };
    for (const signal of signals)
      signal?.addEventListener("abort", abort, { once: true });
  });
}

export async function openRuntimeTerminal(
  transport: RuntimeTransport,
  workspaceIdValue: string,
  options: RuntimeTerminalOptions,
): Promise<RuntimeTerminalChannel> {
  if (
    !transport ||
    typeof transport.post !== "function" ||
    typeof transport.get !== "function"
  ) {
    throw new TypeError("Terminal runtime transport is invalid");
  }
  const workspaceId = validateResourceId(workspaceIdValue, "workspace ID");
  validateSize(options.cols, options.rows);
  const pollDelayMs = options.pollDelayMs ?? 0;
  if (
    !Number.isSafeInteger(pollDelayMs) ||
    pollDelayMs < 0 ||
    pollDelayMs > 5_000
  ) {
    throw new Error("Terminal poll delay is invalid");
  }
  const openedValue = await transport.post<unknown>(
    `/api/workspaces/${workspaceId}/terminals`,
    { cols: options.cols, rows: options.rows },
  );
  if (
    openedValue === null ||
    typeof openedValue !== "object" ||
    Array.isArray(openedValue)
  ) {
    throw new Error("Terminal start response is invalid");
  }
  const opened = openedValue as Record<string, unknown>;
  if (
    Object.keys(opened).length !== 3 ||
    opened.workspace_id !== workspaceId ||
    opened.status !== "attached"
  ) {
    throw new Error("Terminal start response did not match the workspace");
  }
  const terminalId = validateResourceId(opened.id, "channel ID");
  const closeController = new AbortController();
  let closed = false;
  let consumed = false;

  function channelPath(suffix = ""): string {
    return `/api/workspaces/${workspaceId}/terminals/${terminalId}${suffix}`;
  }

  return {
    id: terminalId,
    async *events(): AsyncGenerator<RuntimeTerminalEvent> {
      if (consumed)
        throw new Error("Terminal event stream can only be consumed once");
      consumed = true;
      let nextSequence = 0;
      let retryDelayMs = pollDelayMs;
      while (!closed) {
        if (options.signal?.aborted || closeController.signal.aborted)
          throw abortError();
        let rawBatch: unknown;
        try {
          rawBatch = await transport.get<unknown>(
            channelPath(`/events/${nextSequence}`),
          );
          retryDelayMs = pollDelayMs;
        } catch (error) {
          if (error instanceof DOMException && error.name === "AbortError") {
            throw error;
          }
          if (
            error !== null &&
            typeof error === "object" &&
            "status" in error
          ) {
            const status = (error as { status?: unknown }).status;
            if (typeof status === "number" && status >= 400 && status < 500)
              throw error;
          }
          retryDelayMs = Math.min(Math.max(retryDelayMs * 2, 100), 5_000);
          await delay(retryDelayMs, [options.signal, closeController.signal]);
          continue;
        }
        if (
          rawBatch === null ||
          typeof rawBatch !== "object" ||
          Array.isArray(rawBatch)
        ) {
          throw new Error("Terminal event batch is invalid");
        }
        const batch = rawBatch as Record<string, unknown>;
        if (
          Object.keys(batch).length !== 4 ||
          batch.terminal_id !== terminalId ||
          !Array.isArray(batch.events) ||
          !Number.isSafeInteger(batch.next_sequence) ||
          typeof batch.closed !== "boolean"
        ) {
          throw new Error("Terminal event batch did not match the channel");
        }
        const events = batch.events.map(validateEvent);
        const serverSequence = batch.next_sequence as number;
        if (serverSequence !== nextSequence + events.length) {
          throw new Error("Terminal event sequence is not contiguous");
        }
        nextSequence = serverSequence;
        for (const event of events) yield event;
        if (batch.closed) {
          closed = true;
          closeController.abort();
          await transport.delete(channelPath());
          return;
        }
        await delay(pollDelayMs, [options.signal, closeController.signal]);
      }
    },
    async sendInput(data: string): Promise<void> {
      if (closed) throw new Error("Terminal channel is closed");
      if (
        typeof data !== "string" ||
        !data ||
        new TextEncoder().encode(data).length > 16_384
      ) {
        throw new Error("Terminal input has an invalid length");
      }
      await transport.post(channelPath("/input"), { data });
    },
    async resize(cols: number, rows: number): Promise<void> {
      if (closed) throw new Error("Terminal channel is closed");
      validateSize(cols, rows);
      await transport.post(channelPath("/resize"), { cols, rows });
    },
    async restart(): Promise<void> {
      if (closed) throw new Error("Terminal channel is closed");
      await transport.post(channelPath("/restart"));
    },
    async close(): Promise<void> {
      if (closed) return;
      closed = true;
      closeController.abort();
      await transport.delete(channelPath());
    },
  };
}
