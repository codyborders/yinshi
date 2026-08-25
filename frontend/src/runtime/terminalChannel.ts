import type { RuntimeTransport } from "./runtimeTransport";

const RESOURCE_ID_PATTERN = /^[0-9a-f]{32}$/;
const TERMINAL_OWNER_STORAGE_KEY = "yinshi:terminal-owner-id:v1";
const TERMINAL_OWNER_CHANNEL_NAME = "yinshi:terminal-owner-claims:v1";
const TERMINAL_OWNER_CLAIM_WAIT_MS = 40;
const TERMINAL_OWNER_CLAIM_ATTEMPTS_MAX = 4;
let terminalOwnerIdInMemory: string | null = null;
let terminalOwnerIdPromise: Promise<string> | null = null;
let terminalOwnerChannel: BroadcastChannel | null = null;
const terminalOwnerDocumentId = randomResourceId();
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

interface TerminalOwnerMessage {
  readonly type: "probe" | "claim";
  readonly owner_id: string;
  readonly document_id: string;
  readonly target_document_id?: string;
  readonly established?: boolean;
}

function randomResourceId(): string {
  const randomBytes = crypto.getRandomValues(new Uint8Array(16));
  const resourceId = Array.from(randomBytes, (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
  if (!RESOURCE_ID_PATTERN.test(resourceId)) {
    throw new Error("Terminal owner generation failed");
  }
  return resourceId;
}

function storedTerminalOwnerId(): string | null {
  try {
    const storedOwnerId = window.sessionStorage.getItem(
      TERMINAL_OWNER_STORAGE_KEY,
    );
    return storedOwnerId !== null && RESOURCE_ID_PATTERN.test(storedOwnerId)
      ? storedOwnerId
      : null;
  } catch {
    return null;
  }
}

function storeTerminalOwnerId(ownerId: string): void {
  terminalOwnerIdInMemory = ownerId;
  try {
    window.sessionStorage.setItem(TERMINAL_OWNER_STORAGE_KEY, ownerId);
  } catch {
    // The random identifier is not secret and remains valid in module memory.
  }
}

function ownerClaimDelay(): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, TERMINAL_OWNER_CLAIM_WAIT_MS);
  });
}

function validTerminalOwnerMessage(
  value: unknown,
): value is TerminalOwnerMessage {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const message = value as Record<string, unknown>;
  if (message.type !== "probe" && message.type !== "claim") return false;
  if (
    typeof message.owner_id !== "string" ||
    !RESOURCE_ID_PATTERN.test(message.owner_id) ||
    typeof message.document_id !== "string" ||
    !RESOURCE_ID_PATTERN.test(message.document_id)
  ) {
    return false;
  }
  if (
    message.target_document_id !== undefined &&
    (typeof message.target_document_id !== "string" ||
      !RESOURCE_ID_PATTERN.test(message.target_document_id))
  ) {
    return false;
  }
  return (
    message.established === undefined ||
    typeof message.established === "boolean"
  );
}

async function claimTerminalOwnerId(): Promise<string> {
  let candidateOwnerId =
    storedTerminalOwnerId() ?? terminalOwnerIdInMemory ?? randomResourceId();
  if (typeof BroadcastChannel !== "function") {
    storeTerminalOwnerId(candidateOwnerId);
    return candidateOwnerId;
  }

  let channel: BroadcastChannel;
  try {
    channel = new BroadcastChannel(TERMINAL_OWNER_CHANNEL_NAME);
  } catch {
    storeTerminalOwnerId(candidateOwnerId);
    return candidateOwnerId;
  }
  terminalOwnerChannel = channel;
  let pendingOwnerId: string | null = null;
  let establishedOwnerId: string | null = null;
  let collisionDetected = false;

  const rotateEstablishedOwner = () => {
    const replacementOwnerId = randomResourceId();
    establishedOwnerId = replacementOwnerId;
    pendingOwnerId = null;
    storeTerminalOwnerId(replacementOwnerId);
    terminalOwnerIdPromise = Promise.resolve(replacementOwnerId);
    channel.postMessage({
      type: "probe",
      owner_id: replacementOwnerId,
      document_id: terminalOwnerDocumentId,
    } satisfies TerminalOwnerMessage);
  };

  channel.onmessage = (event: MessageEvent<unknown>) => {
    if (!validTerminalOwnerMessage(event.data)) return;
    const message = event.data;
    if (message.document_id === terminalOwnerDocumentId) return;
    if (message.type === "probe") {
      const established = establishedOwnerId === message.owner_id;
      const contending = pendingOwnerId === message.owner_id;
      if (!established && !contending) return;
      channel.postMessage({
        type: "claim",
        owner_id: message.owner_id,
        document_id: terminalOwnerDocumentId,
        target_document_id: message.document_id,
        established,
      } satisfies TerminalOwnerMessage);
      const otherDocumentWins =
        message.document_id.localeCompare(terminalOwnerDocumentId) < 0;
      if (contending && otherDocumentWins) collisionDetected = true;
      if (established && otherDocumentWins) rotateEstablishedOwner();
      return;
    }
    if (message.target_document_id !== terminalOwnerDocumentId) return;
    const otherDocumentWins =
      message.document_id.localeCompare(terminalOwnerDocumentId) < 0;
    if (message.owner_id === pendingOwnerId) {
      if (message.established === true || otherDocumentWins) {
        collisionDetected = true;
      }
      return;
    }
    if (
      message.owner_id === establishedOwnerId &&
      message.established === true &&
      otherDocumentWins
    ) {
      rotateEstablishedOwner();
    }
  };
  window.addEventListener(
    "pagehide",
    (event: PageTransitionEvent) => {
      establishedOwnerId = null;
      pendingOwnerId = null;
      channel.close();
      if (terminalOwnerChannel === channel) terminalOwnerChannel = null;
      if (event.persisted) terminalOwnerIdPromise = null;
    },
    { once: true },
  );
  window.addEventListener(
    "pageshow",
    (event: PageTransitionEvent) => {
      if (event.persisted) terminalOwnerIdPromise = null;
    },
    { once: true },
  );

  for (
    let attempt = 0;
    attempt < TERMINAL_OWNER_CLAIM_ATTEMPTS_MAX;
    attempt += 1
  ) {
    pendingOwnerId = candidateOwnerId;
    collisionDetected = false;
    channel.postMessage({
      type: "probe",
      owner_id: candidateOwnerId,
      document_id: terminalOwnerDocumentId,
    } satisfies TerminalOwnerMessage);
    await ownerClaimDelay();
    if (!collisionDetected) {
      establishedOwnerId = candidateOwnerId;
      pendingOwnerId = null;
      storeTerminalOwnerId(candidateOwnerId);
      return candidateOwnerId;
    }
    candidateOwnerId = randomResourceId();
    storeTerminalOwnerId(candidateOwnerId);
  }

  establishedOwnerId = candidateOwnerId;
  pendingOwnerId = null;
  storeTerminalOwnerId(candidateOwnerId);
  return candidateOwnerId;
}

function terminalOwnerId(): Promise<string> {
  terminalOwnerIdPromise ??= claimTerminalOwnerId();
  return terminalOwnerIdPromise;
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
  const ownerId = await terminalOwnerId();
  const openedValue = await transport.post<unknown>(
    `/api/workspaces/${workspaceId}/terminals`,
    { cols: options.cols, rows: options.rows, owner_id: ownerId },
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
