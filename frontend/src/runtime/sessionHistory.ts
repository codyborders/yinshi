import type { Message } from "../api/client";
import type { RuntimeTransport } from "./runtimeTransport";

const MAX_HISTORY_PAGE_REQUESTS = 10_000;
const MAX_HISTORY_FIELD_REQUESTS = 100_000;
const MAX_HISTORY_MESSAGES = 640_000;
const MAX_HISTORY_FIELD_LENGTH = 1_000_000_000;
const HISTORY_FIELD_WORKERS = 4;

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

interface SessionHistoryLoadOptions {
  isCancelled?: () => boolean;
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

export async function loadSessionHistory(
  transport: RuntimeTransport,
  sessionId: string,
  options: SessionHistoryLoadOptions = {},
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
