import { ApiError, type Message } from "../api/client";
import { getSessionHistoryCacheClient } from "./sessionHistoryCacheClient";
import type { RuntimeTransport } from "./runtimeTransport";

const MAX_HISTORY_PAGE_REQUESTS = 10_000;
const MAX_HISTORY_FIELD_REQUESTS = 100_000;
const MAX_HISTORY_MESSAGES = 640_000;
const MAX_HISTORY_FIELD_LENGTH = 1_000_000_000;
const HISTORY_FIELD_WORKERS = 8;
const HISTORY_BUNDLE_PAGE_MESSAGES_MAX = 64;
const HISTORY_BUNDLE_PAGES_MAX = 256;
const HISTORY_BUNDLE_MESSAGES_MAX = 10_000;
const HISTORY_BUNDLE_ENCODED_BYTES_MAX = 900_000;
const HISTORY_BUNDLE_RAW_BYTES_MAX = 4 * 1_024 * 1_024;
const HISTORY_CURSOR_PATTERN = /^[A-Za-z0-9_-]{1,128}$/u;
const HISTORY_BUNDLE_DATA_PATTERN = /^[A-Za-z0-9_-]+$/u;

type HistoryFieldName = "content" | "full_message";

interface MessageHistoryMetadata {
  id: string;
  created_at: string;
  session_id: string;
  role: string;
  content_length: number | null;
  full_message_length: number | null;
  turn_id: string | null;
  turn_status: string | null;
}

interface MessageHistoryPage {
  messages: MessageHistoryMetadata[];
  next_cursor: string | null;
}

interface MessageHistoryFieldChunk {
  value: string;
  offset: number;
  next_offset: number | null;
}

interface RequestBudget {
  fieldRequests: number;
}

interface HistoryFieldJob {
  fieldName: HistoryFieldName;
  length: number;
  messageIndex: number;
}

export interface SessionHistoryLoadOptions {
  isCancelled?: () => boolean;
  onBundledActiveRun?: (activeRunId: string | null) => void;
  cacheUserId?: string;
  onCachedHistory?: (history: Message[], activeRunId: string | null) => void;
  skipCacheRead?: boolean;
}

interface BundledSessionHistory {
  messages: Message[];
  activeRunId: string | null;
  rawEnvelopes: unknown[];
}

interface MessageHistoryBundle {
  version: 1;
  encoding: "gzip+base64url";
  rawBytes: number;
  messageCount: number;
  cursor: string | null;
  nextCursor: string | null;
  through: string | null;
  snapshot: number;
  snapshotCount: number;
  snapshotTail: string | null;
  activeRunId: string | null;
  data: string;
}

interface HistoryCursorKey {
  timestamp: string;
  messageId: string;
}

function assertNotCancelled(options: SessionHistoryLoadOptions): void {
  if (options.isCancelled?.()) {
    throw new Error("Message history loading was cancelled");
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isFieldLength(value: unknown): value is number | null {
  return (
    value === null ||
    (Number.isSafeInteger(value) &&
      (value as number) >= 0 &&
      (value as number) <= MAX_HISTORY_FIELD_LENGTH)
  );
}

function parseMetadata(
  value: unknown,
  sessionId: string,
): MessageHistoryMetadata {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    !/^[0-9a-f]{32}$/u.test(value.id) ||
    typeof value.created_at !== "string" ||
    value.session_id !== sessionId ||
    typeof value.role !== "string" ||
    !isFieldLength(value.content_length) ||
    !isFieldLength(value.full_message_length) ||
    !isNullableString(value.turn_id) ||
    !isNullableString(value.turn_status)
  ) {
    throw new Error("Invalid message history metadata");
  }
  return {
    id: value.id,
    created_at: value.created_at,
    session_id: value.session_id,
    role: value.role,
    content_length: value.content_length,
    full_message_length: value.full_message_length,
    turn_id: value.turn_id,
    turn_status: value.turn_status,
  };
}

function parsePage(value: unknown, sessionId: string): MessageHistoryPage {
  if (
    !isRecord(value) ||
    !Array.isArray(value.messages) ||
    value.messages.length > 64 ||
    !isNullableString(value.next_cursor)
  ) {
    throw new Error("Invalid message history page");
  }
  return {
    messages: value.messages.map((message) =>
      parseMetadata(message, sessionId),
    ),
    next_cursor: value.next_cursor,
  };
}

function parseFieldChunk(value: unknown): MessageHistoryFieldChunk {
  if (
    !isRecord(value) ||
    typeof value.value !== "string" ||
    !Number.isSafeInteger(value.offset) ||
    (value.offset as number) < 0 ||
    !(
      value.next_offset === null ||
      (Number.isSafeInteger(value.next_offset) &&
        (value.next_offset as number) >= 0)
    )
  ) {
    throw new Error("Invalid message history field chunk");
  }
  return {
    value: value.value,
    offset: value.offset as number,
    next_offset: value.next_offset as number | null,
  };
}

function codePointLength(value: string): number {
  return Array.from(value).length;
}

function exactKeys(
  value: Record<string, unknown>,
  expected: string[],
): boolean {
  const actual = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  return (
    actual.length === sortedExpected.length &&
    actual.every((key, index) => key === sortedExpected[index])
  );
}

function decodeBase64Url(value: string): Uint8Array {
  if (!HISTORY_BUNDLE_DATA_PATTERN.test(value)) {
    throw new Error("Invalid message history bundle encoding");
  }
  const padded = `${value.replace(/-/gu, "+").replace(/_/gu, "/")}${"=".repeat(
    -value.length & 3,
  )}`;
  let binary: string;
  try {
    binary = atob(padded);
  } catch {
    throw new Error("Invalid message history bundle encoding");
  }
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  if (base64Url(bytes) !== value) {
    throw new Error("Invalid message history bundle encoding");
  }
  return bytes;
}

function base64Url(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/gu, "-")
    .replace(/\//gu, "_")
    .replace(/=+$/u, "");
}

function parseHistoryCursor(value: string): HistoryCursorKey {
  if (!HISTORY_CURSOR_PATTERN.test(value)) {
    throw new Error("Invalid message history bundle cursor");
  }
  const raw = decodeBase64Url(value);
  if (raw.length < 19 || raw[0] !== 1) {
    throw new Error("Invalid message history bundle cursor");
  }
  const timestampLength = raw[1];
  if (
    timestampLength < 1 ||
    timestampLength > 64 ||
    raw.length !== 2 + timestampLength + 16
  ) {
    throw new Error("Invalid message history bundle cursor");
  }
  let timestamp: string;
  try {
    timestamp = new TextDecoder("utf-8", { fatal: true }).decode(
      raw.subarray(2, 2 + timestampLength),
    );
  } catch {
    throw new Error("Invalid message history bundle cursor");
  }
  if (!Number.isFinite(Date.parse(timestamp))) {
    throw new Error("Invalid message history bundle cursor");
  }
  const messageId = Array.from(raw.subarray(2 + timestampLength), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  return { timestamp, messageId };
}

function compareHistoryKeys(
  left: HistoryCursorKey,
  right: HistoryCursorKey,
): number {
  if (left.timestamp !== right.timestamp) {
    return left.timestamp < right.timestamp ? -1 : 1;
  }
  return left.messageId.localeCompare(right.messageId);
}

function cursorIdentifiesMessage(cursor: string, message: Message): boolean {
  const key = parseHistoryCursor(cursor);
  return (
    key.messageId === message.id &&
    Date.parse(key.timestamp) === Date.parse(message.created_at)
  );
}

function parseBundleEnvelope(value: unknown): MessageHistoryBundle {
  const keys = [
    "version",
    "encoding",
    "raw_bytes",
    "message_count",
    "cursor",
    "next_cursor",
    "through",
    "snapshot",
    "snapshot_count",
    "snapshot_tail",
    "active_run_id",
    "data",
  ];
  if (
    !isRecord(value) ||
    !exactKeys(value, keys) ||
    value.version !== 1 ||
    value.encoding !== "gzip+base64url" ||
    !Number.isSafeInteger(value.raw_bytes) ||
    (value.raw_bytes as number) < 0 ||
    (value.raw_bytes as number) > HISTORY_BUNDLE_RAW_BYTES_MAX ||
    !Number.isSafeInteger(value.message_count) ||
    (value.message_count as number) < 0 ||
    (value.message_count as number) > HISTORY_BUNDLE_PAGE_MESSAGES_MAX ||
    !isNullableString(value.cursor) ||
    !isNullableString(value.next_cursor) ||
    !isNullableString(value.through) ||
    !Number.isSafeInteger(value.snapshot) ||
    (value.snapshot as number) < 0 ||
    !Number.isSafeInteger(value.snapshot_count) ||
    (value.snapshot_count as number) < 0 ||
    !isNullableString(value.snapshot_tail) ||
    !(
      value.active_run_id === null ||
      (typeof value.active_run_id === "string" &&
        /^[0-9a-f]{32}$/u.test(value.active_run_id))
    ) ||
    typeof value.data !== "string" ||
    value.data.length > HISTORY_BUNDLE_ENCODED_BYTES_MAX
  ) {
    throw new Error("Invalid message history bundle");
  }
  if (value.cursor !== null) parseHistoryCursor(value.cursor);
  if (value.next_cursor !== null) parseHistoryCursor(value.next_cursor);
  if (value.through !== null) parseHistoryCursor(value.through);
  if (value.snapshot_tail !== null) parseHistoryCursor(value.snapshot_tail);
  return {
    version: 1,
    encoding: "gzip+base64url",
    rawBytes: value.raw_bytes as number,
    messageCount: value.message_count as number,
    cursor: value.cursor,
    nextCursor: value.next_cursor,
    through: value.through,
    snapshot: value.snapshot as number,
    snapshotCount: value.snapshot_count as number,
    snapshotTail: value.snapshot_tail,
    activeRunId: value.active_run_id,
    data: value.data,
  };
}

function parseBundledMessage(value: unknown, sessionId: string): Message {
  const keys = [
    "id",
    "created_at",
    "session_id",
    "role",
    "content",
    "full_message",
    "turn_id",
    "turn_status",
  ];
  if (
    !isRecord(value) ||
    !exactKeys(value, keys) ||
    typeof value.id !== "string" ||
    !/^[0-9a-f]{32}$/u.test(value.id) ||
    typeof value.created_at !== "string" ||
    !Number.isFinite(Date.parse(value.created_at)) ||
    value.session_id !== sessionId ||
    typeof value.role !== "string" ||
    !isNullableString(value.content) ||
    !isNullableString(value.full_message) ||
    !isNullableString(value.turn_id) ||
    !isNullableString(value.turn_status)
  ) {
    throw new Error("Invalid message history bundle message");
  }
  return {
    id: value.id,
    created_at: value.created_at,
    session_id: value.session_id,
    role: value.role,
    content: value.content,
    full_message: value.full_message,
    turn_id: value.turn_id,
    turn_status: value.turn_status,
  };
}

async function decompressBundle(
  encoded: string,
  options: SessionHistoryLoadOptions,
): Promise<Uint8Array> {
  assertNotCancelled(options);
  const compressed = decodeBase64Url(encoded);
  const source = new ReadableStream<BufferSource>({
    start(controller) {
      const input = new ArrayBuffer(compressed.byteLength);
      new Uint8Array(input).set(compressed);
      controller.enqueue(input);
      controller.close();
    },
  });
  const reader = source
    .pipeThrough(new DecompressionStream("gzip"))
    .getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      assertNotCancelled(options);
      const result = await reader.read();
      if (result.done) break;
      total += result.value.length;
      if (total > HISTORY_BUNDLE_RAW_BYTES_MAX) {
        throw new Error(
          "Message history bundle exceeded its decoded size limit",
        );
      }
      chunks.push(result.value);
    }
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  }
  const payload = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    payload.set(chunk, offset);
    offset += chunk.length;
  }
  return payload;
}

async function decodeBundledSessionHistory(
  getRawEnvelope: (path: string, page: number) => Promise<unknown>,
  sessionId: string,
  options: SessionHistoryLoadOptions,
  expectedEnvelopeCount?: number,
): Promise<BundledSessionHistory> {
  const messages: Message[] = [];
  const rawEnvelopes: unknown[] = [];
  const seenCursors = new Set<string>();
  const seenMessageIds = new Set<string>();
  let cursor: string | null = null;
  let through: string | null = null;
  let snapshot: number | null = null;
  let snapshotCount: number | null = null;
  let snapshotTail: string | null = null;
  let activeRunId: string | null = null;
  for (let page = 0; page < HISTORY_BUNDLE_PAGES_MAX; page += 1) {
    assertNotCancelled(options);
    const path =
      cursor === null
        ? `/api/sessions/${sessionId}/messages/bundle`
        : `/api/sessions/${sessionId}/messages/bundle?cursor=${encodeURIComponent(
            cursor,
          )}&through=${encodeURIComponent(
            through ?? "",
          )}&snapshot=${snapshot ?? ""}&snapshot_count=${
            snapshotCount ?? ""
          }&snapshot_tail=${encodeURIComponent(
            snapshotTail ?? "",
          )}&active_run_id=${activeRunId ?? "none"}`;
    const rawEnvelope = await getRawEnvelope(path, page);
    const envelope = parseBundleEnvelope(rawEnvelope);
    rawEnvelopes.push(rawEnvelope);
    assertNotCancelled(options);
    if (envelope.cursor !== cursor) {
      throw new Error("Message history bundle cursor changed unexpectedly");
    }
    if (cursor === null) {
      through = envelope.through;
      snapshot = envelope.snapshot;
      snapshotCount = envelope.snapshotCount;
      snapshotTail = envelope.snapshotTail;
      activeRunId = envelope.activeRunId;
    } else if (
      envelope.through !== through ||
      envelope.snapshot !== snapshot ||
      envelope.snapshotCount !== snapshotCount ||
      envelope.snapshotTail !== snapshotTail ||
      envelope.activeRunId !== activeRunId
    ) {
      throw new Error("Message history bundle snapshot changed unexpectedly");
    }
    if (
      (cursor === null &&
        envelope.messageCount === 0 &&
        (envelope.through !== null ||
          envelope.snapshot !== 0 ||
          envelope.snapshotCount !== 0 ||
          envelope.snapshotTail !== null ||
          envelope.nextCursor !== null)) ||
      (cursor === null &&
        envelope.messageCount > 0 &&
        (envelope.through === null ||
          envelope.snapshot < 1 ||
          envelope.snapshotCount < envelope.messageCount ||
          envelope.snapshotTail === null)) ||
      (cursor !== null && envelope.messageCount === 0)
    ) {
      throw new Error("Invalid message history bundle snapshot");
    }
    const decoded = await decompressBundle(envelope.data, options);
    if (decoded.length !== envelope.rawBytes) {
      throw new Error("Message history bundle decoded length did not match");
    }
    let payload: unknown;
    try {
      payload = JSON.parse(
        new TextDecoder("utf-8", { fatal: true }).decode(decoded),
      );
    } catch {
      throw new Error("Message history bundle payload is invalid");
    }
    if (!Array.isArray(payload) || payload.length !== envelope.messageCount) {
      throw new Error("Message history bundle count did not match");
    }
    for (const rawMessage of payload) {
      const message = parseBundledMessage(rawMessage, sessionId);
      const previous = messages[messages.length - 1];
      if (previous) {
        const order = compareHistoryKeys(
          { timestamp: previous.created_at, messageId: previous.id },
          { timestamp: message.created_at, messageId: message.id },
        );
        if (order >= 0) {
          throw new Error("Message history bundle order is invalid");
        }
      }
      if (seenMessageIds.has(message.id)) {
        throw new Error("Message history bundle repeated a message");
      }
      seenMessageIds.add(message.id);
      messages.push(message);
      if (messages.length > HISTORY_BUNDLE_MESSAGES_MAX) {
        throw new Error("Message history bundle contains too many messages");
      }
    }
    const finalPageMessage = messages[messages.length - 1];
    if (envelope.nextCursor === null) {
      if (
        envelope.messageCount > 0 &&
        (envelope.through === null ||
          finalPageMessage === undefined ||
          !cursorIdentifiesMessage(envelope.through, finalPageMessage))
      ) {
        throw new Error(
          "Message history bundle snapshot end did not match final message",
        );
      }
      if (
        messages.length > 0 &&
        (envelope.snapshotTail === null ||
          !messages.some((message) =>
            cursorIdentifiesMessage(envelope.snapshotTail as string, message),
          ))
      ) {
        throw new Error("Message history bundle snapshot tail was not found");
      }
      if (
        expectedEnvelopeCount !== undefined &&
        rawEnvelopes.length !== expectedEnvelopeCount
      ) {
        throw new Error("Message history bundle cache has extra pages");
      }
      assertNotCancelled(options);
      return { messages, activeRunId, rawEnvelopes };
    }
    if (
      finalPageMessage === undefined ||
      !cursorIdentifiesMessage(envelope.nextCursor, finalPageMessage)
    ) {
      throw new Error(
        "Message history bundle cursor did not identify final page message",
      );
    }
    const nextCursorKey = parseHistoryCursor(envelope.nextCursor);
    if (
      envelope.messageCount === 0 ||
      seenCursors.has(envelope.nextCursor) ||
      envelope.through === null ||
      (cursor !== null &&
        compareHistoryKeys(nextCursorKey, parseHistoryCursor(cursor)) <= 0) ||
      compareHistoryKeys(nextCursorKey, parseHistoryCursor(envelope.through)) >
        0
    ) {
      throw new Error("Message history bundle cursor did not advance");
    }
    seenCursors.add(envelope.nextCursor);
    cursor = envelope.nextCursor;
  }
  throw new Error("Message history bundle used too many pages");
}

async function loadLiveBundledSessionHistory(
  transport: RuntimeTransport,
  sessionId: string,
  options: SessionHistoryLoadOptions,
): Promise<BundledSessionHistory> {
  return decodeBundledSessionHistory(
    (path) => transport.get<unknown>(path),
    sessionId,
    options,
  );
}

async function loadCachedBundledSessionHistory(
  rawEnvelopes: unknown[],
  sessionId: string,
  options: SessionHistoryLoadOptions,
): Promise<BundledSessionHistory> {
  if (
    rawEnvelopes.length < 1 ||
    rawEnvelopes.length > HISTORY_BUNDLE_PAGES_MAX
  ) {
    throw new Error("Invalid message history bundle cache page count");
  }
  return decodeBundledSessionHistory(
    async (_path, page) => {
      if (page >= rawEnvelopes.length) {
        throw new Error("Message history bundle cache ended early");
      }
      return rawEnvelopes[page];
    },
    sessionId,
    options,
    rawEnvelopes.length,
  );
}

function shouldFallbackFromBundle(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 413 &&
    error.code === "history_bundle_message_too_large"
  );
}

async function loadHistoryField(
  transport: RuntimeTransport,
  sessionId: string,
  messageId: string,
  fieldName: HistoryFieldName,
  advertisedLength: number | null,
  budget: RequestBudget,
  options: SessionHistoryLoadOptions,
): Promise<string | null> {
  if (advertisedLength === null) return null;
  if (advertisedLength === 0) return "";

  const chunks: string[] = [];
  let offset = 0;
  while (offset < advertisedLength) {
    assertNotCancelled(options);
    budget.fieldRequests += 1;
    if (budget.fieldRequests > MAX_HISTORY_FIELD_REQUESTS) {
      throw new Error("Message history used too many field requests");
    }
    const path =
      `/api/sessions/${sessionId}/messages/${messageId}/field` +
      `?name=${fieldName}&offset=${offset}`;
    const chunk = parseFieldChunk(await transport.get<unknown>(path));
    assertNotCancelled(options);
    if (chunk.offset !== offset) {
      throw new Error("Message history field offset changed unexpectedly");
    }
    const nextExpectedOffset = offset + codePointLength(chunk.value);
    if (nextExpectedOffset <= offset || nextExpectedOffset > advertisedLength) {
      throw new Error("Message history field chunk did not advance");
    }
    chunks.push(chunk.value);
    if (chunk.next_offset === null) {
      if (nextExpectedOffset !== advertisedLength) {
        throw new Error(
          "Message history field ended before its advertised length",
        );
      }
      offset = nextExpectedOffset;
      break;
    }
    if (
      chunk.next_offset !== nextExpectedOffset ||
      chunk.next_offset <= offset ||
      chunk.next_offset > advertisedLength
    ) {
      throw new Error("Message history field returned an invalid next offset");
    }
    offset = chunk.next_offset;
  }

  if (offset !== advertisedLength) {
    throw new Error("Message history field length did not match metadata");
  }
  return chunks.join("");
}

function compareMetadataKeys(
  left: MessageHistoryMetadata,
  right: MessageHistoryMetadata,
): number {
  if (left.created_at < right.created_at) return -1;
  if (left.created_at > right.created_at) return 1;
  return left.id.localeCompare(right.id);
}

async function loadLegacySessionHistory(
  transport: RuntimeTransport,
  sessionId: string,
  options: SessionHistoryLoadOptions,
): Promise<Message[]> {
  const metadata: MessageHistoryMetadata[] = [];
  const seenCursors = new Set<string>();
  const seenMessageIds = new Set<string>();
  let cursor: string | null = null;
  let pageRequests = 0;

  while (true) {
    assertNotCancelled(options);
    pageRequests += 1;
    if (pageRequests > MAX_HISTORY_PAGE_REQUESTS) {
      throw new Error("Message history used too many page requests");
    }
    const cursorKey = cursor ?? "<initial>";
    if (seenCursors.has(cursorKey)) {
      throw new Error("Message history cursor did not advance");
    }
    seenCursors.add(cursorKey);
    const path = cursor
      ? `/api/sessions/${sessionId}/messages/page?cursor=${encodeURIComponent(cursor)}`
      : `/api/sessions/${sessionId}/messages/page`;
    const page = parsePage(await transport.get<unknown>(path), sessionId);
    assertNotCancelled(options);
    for (const message of page.messages) {
      const previous = metadata[metadata.length - 1];
      if (previous && compareMetadataKeys(previous, message) >= 0) {
        throw new Error("Message history metadata order is invalid");
      }
      if (seenMessageIds.has(message.id)) {
        throw new Error("Message history repeated a message");
      }
      seenMessageIds.add(message.id);
      metadata.push(message);
      if (metadata.length > MAX_HISTORY_MESSAGES) {
        throw new Error("Message history contains too many messages");
      }
    }
    if (page.next_cursor === null) break;
    if (page.messages.length === 0 || seenCursors.has(page.next_cursor)) {
      throw new Error("Message history cursor did not advance");
    }
    cursor = page.next_cursor;
  }

  const budget: RequestBudget = { fieldRequests: 0 };
  const messages: Message[] = metadata.map((message) => ({
    id: message.id,
    created_at: message.created_at,
    session_id: message.session_id,
    role: message.role,
    content: message.content_length === null ? null : "",
    full_message: message.full_message_length === null ? null : "",
    turn_id: message.turn_id,
    turn_status: message.turn_status,
  }));
  const jobs: HistoryFieldJob[] = [];
  for (const [messageIndex, message] of metadata.entries()) {
    if (message.content_length !== null && message.content_length > 0) {
      jobs.push({
        fieldName: "content",
        length: message.content_length,
        messageIndex,
      });
    }
    if (
      message.full_message_length !== null &&
      message.full_message_length > 0
    ) {
      jobs.push({
        fieldName: "full_message",
        length: message.full_message_length,
        messageIndex,
      });
    }
  }

  let nextJobIndex = 0;
  const workerState: {
    firstFailure: { error: unknown } | null;
    stopped: boolean;
  } = { firstFailure: null, stopped: false };
  async function hydrateFields(): Promise<void> {
    try {
      while (!workerState.stopped) {
        assertNotCancelled(options);
        const job = jobs[nextJobIndex];
        if (!job) return;
        nextJobIndex += 1;
        const message = metadata[job.messageIndex];
        const value = await loadHistoryField(
          transport,
          sessionId,
          message.id,
          job.fieldName,
          job.length,
          budget,
          options,
        );
        messages[job.messageIndex][job.fieldName] = value;
      }
    } catch (error) {
      workerState.stopped = true;
      workerState.firstFailure ??= { error };
    }
  }

  const workerCount = Math.min(HISTORY_FIELD_WORKERS, jobs.length);
  await Promise.all(Array.from({ length: workerCount }, hydrateFields));
  if (workerState.firstFailure) throw workerState.firstFailure.error;
  return messages;
}

export async function loadSessionHistory(
  transport: RuntimeTransport,
  sessionId: string,
  options: SessionHistoryLoadOptions = {},
): Promise<Message[]> {
  const canLoadBundle =
    transport.runtime.location === "managed" &&
    transport.runtime.historyBundleSupported === true &&
    typeof DecompressionStream === "function";
  if (canLoadBundle) {
    const cacheUserId = options.cacheUserId;
    const cacheClient =
      typeof cacheUserId === "string" &&
      cacheUserId.length >= 1 &&
      cacheUserId.length <= 256
        ? getSessionHistoryCacheClient()
        : null;
    const cacheRead =
      cacheClient && !options.skipCacheRead
        ? {
            client: cacheClient,
            promise: cacheClient.get(cacheUserId as string, sessionId),
          }
        : null;
    let liveSucceeded = false;
    const livePromise = loadLiveBundledSessionHistory(
      transport,
      sessionId,
      options,
    );
    void livePromise.then(
      () => {
        liveSucceeded = true;
      },
      () => undefined,
    );
    const cachedPromise =
      cacheRead !== null
        ? cacheRead.promise
            .then(async (rawEnvelopes) => {
              if (rawEnvelopes === null || liveSucceeded) return;
              try {
                const cached = await loadCachedBundledSessionHistory(
                  rawEnvelopes,
                  sessionId,
                  options,
                );
                if (!liveSucceeded) {
                  options.onCachedHistory?.(
                    cached.messages,
                    cached.activeRunId,
                  );
                }
              } catch {
                if (!options.isCancelled?.()) {
                  await cacheRead.client.delete(
                    cacheUserId as string,
                    sessionId,
                  );
                }
              }
            })
            .catch(() => undefined)
        : Promise.resolve();
    try {
      await Promise.race([
        cachedPromise,
        livePromise.then(
          () => undefined,
          () => cachedPromise,
        ),
      ]);
      const live = await livePromise;
      await cachedPromise;
      assertNotCancelled(options);
      options.onBundledActiveRun?.(live.activeRunId);
      if (cacheClient) {
        void cacheClient
          .put(cacheUserId as string, sessionId, live.rawEnvelopes)
          .catch(() => undefined);
      }
      return live.messages;
    } catch (error) {
      if (!shouldFallbackFromBundle(error)) throw error;
    }
  }
  return loadLegacySessionHistory(transport, sessionId, options);
}
