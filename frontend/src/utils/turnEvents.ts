import type { SSEEvent } from "../api/client";

export type TurnStatus = "completed" | "cancelled" | "failed";

export interface TurnBlock {
  id: string;
  type: "text" | "thinking" | "tool_use" | "error" | "status";
  text?: string;
  severity?: "info" | "warning" | "error";
  toolName?: string;
  toolInput?: unknown;
  toolId?: string;
  toolOutput?: string;
  toolError?: boolean;
}

export interface TurnEventApplyResult {
  changed: boolean;
  status?: TurnStatus;
}

const STORED_TURN_SCHEMA = "yinshi.assistant_turn.v1";

function appendOrCreate(
  blocks: TurnBlock[],
  type: "text" | "thinking",
  text: string,
  createBlockId: () => string,
): boolean {
  if (!text) {
    return false;
  }
  const last = blocks[blocks.length - 1];
  if (last && last.type === type) {
    last.text = (last.text || "") + text;
  } else {
    blocks.push({ id: createBlockId(), type, text });
  }
  return true;
}

function stringifyOutput(content: unknown): string {
  if (typeof content === "string") {
    return content;
  }
  if (content === null || content === undefined) {
    return "";
  }
  return JSON.stringify(content, null, 2);
}

function parseToolInput(block: TurnBlock): void {
  if (block.type !== "tool_use") {
    return;
  }
  if (typeof block.toolInput !== "string") {
    return;
  }
  try {
    block.toolInput = JSON.parse(block.toolInput);
  } catch {
    // Partial tool-call JSON is still useful to show as raw text.
  }
}

function applyAssistantEvent(
  blocks: TurnBlock[],
  event: Extract<SSEEvent, { type: "assistant" }>,
  createBlockId: () => string,
): boolean {
  let changed = false;
  for (const block of event.message?.content ?? []) {
    if (block.type === "text") {
      changed = appendOrCreate(blocks, "text", block.text || "", createBlockId) || changed;
    } else if (block.type === "thinking") {
      changed = appendOrCreate(
        blocks,
        "thinking",
        block.thinking || block.text || "",
        createBlockId,
      ) || changed;
    } else if (block.type === "tool_use") {
      blocks.push({
        id: block.id || createBlockId(),
        type: "tool_use",
        toolName: block.name || "unknown",
        toolInput: block.input,
        toolId: block.id,
      });
      changed = true;
    }
  }
  return changed;
}

function applyContentBlockStart(
  blocks: TurnBlock[],
  event: Extract<SSEEvent, { type: "content_block_start" }>,
  createBlockId: () => string,
): boolean {
  const contentBlock = event.content_block;
  if (contentBlock.type === "tool_use") {
    blocks.push({
      id: contentBlock.id || createBlockId(),
      type: "tool_use",
      toolName: contentBlock.name || "unknown",
      toolInput: contentBlock.input,
      toolId: contentBlock.id,
    });
    return true;
  }
  if (contentBlock.type === "thinking") {
    blocks.push({
      id: createBlockId(),
      type: "thinking",
      text: contentBlock.thinking || "",
    });
    return true;
  }
  if (contentBlock.type === "text") {
    blocks.push({
      id: createBlockId(),
      type: "text",
      text: contentBlock.text || "",
    });
    return true;
  }
  return false;
}

function applyContentBlockDelta(
  blocks: TurnBlock[],
  event: Extract<SSEEvent, { type: "content_block_delta" }>,
  createBlockId: () => string,
): boolean {
  const delta = event.delta;
  if (delta.type === "text_delta") {
    return appendOrCreate(blocks, "text", delta.text || "", createBlockId);
  }
  if (delta.type === "thinking_delta") {
    return appendOrCreate(blocks, "thinking", delta.thinking || delta.text || "", createBlockId);
  }
  if (delta.type === "input_json_delta") {
    const last = blocks[blocks.length - 1];
    if (last && last.type === "tool_use") {
      const partial = delta.partial_json || "";
      last.toolInput = ((last.toolInput as string) || "") + partial;
      return true;
    }
  }
  return false;
}

function applyToolResult(
  blocks: TurnBlock[],
  event: Extract<SSEEvent, { type: "tool_result" }>,
): boolean {
  const matching = blocks.find(
    (block) => block.type === "tool_use" && block.toolId === event.tool_use_id,
  );
  if (!matching) {
    return false;
  }
  matching.toolOutput = stringifyOutput(event.content);
  matching.toolError = event.is_error;
  return true;
}

export function applyTurnEventToBlocks(
  blocks: TurnBlock[],
  event: SSEEvent,
  createBlockId: () => string,
): TurnEventApplyResult {
  switch (event.type) {
    case "assistant":
      return { changed: applyAssistantEvent(blocks, event, createBlockId) };
    case "tool_use":
      blocks.push({
        id: event.id || createBlockId(),
        type: "tool_use",
        toolName: event.name || event.tool_name || "unknown",
        toolInput: event.input,
        toolId: event.id,
      });
      return { changed: true };
    case "content_block_start":
      return { changed: applyContentBlockStart(blocks, event, createBlockId) };
    case "content_block_delta":
      return { changed: applyContentBlockDelta(blocks, event, createBlockId) };
    case "content_block_stop": {
      const last = blocks[blocks.length - 1];
      if (last) {
        parseToolInput(last);
      }
      return { changed: true };
    }
    case "tool_result":
      return { changed: applyToolResult(blocks, event) };
    case "result":
    case "message_stop":
      return { changed: false, status: "completed" };
    case "cancelled":
      return { changed: false, status: "cancelled" };
    case "status":
      blocks.push({
        id: createBlockId(),
        type: "status",
        text: event.message || event.status,
        severity: event.severity || "info",
      });
      return { changed: true };
    case "error":
      blocks.push({ id: createBlockId(), type: "error", text: event.error });
      return { changed: true, status: "failed" };
    case "message_start":
    case "message_delta":
      return { changed: false };
    default:
      return { changed: false };
  }
}

export function blocksToContent(blocks: TurnBlock[]): string {
  return blocks
    .filter((block) => block.type === "text")
    .map((block) => block.text || "")
    .join("");
}

function normalizeStoredEvent(rawEvent: unknown): SSEEvent | null {
  if (!rawEvent || typeof rawEvent !== "object") {
    return null;
  }
  const record = rawEvent as Record<string, unknown>;
  if (record.type === "message") {
    return normalizeStoredEvent(record.data);
  }
  if (record.type === "tool_use") {
    return {
      type: "tool_use",
      name: String(record.toolName || record.name || record.tool_name || "unknown"),
      id: typeof record.id === "string" ? record.id : undefined,
      input: record.toolInput ?? record.input,
    };
  }
  return record as SSEEvent;
}

function extractStoredEvents(fullMessage: string): SSEEvent[] {
  const parsed = JSON.parse(fullMessage) as unknown;
  if (!parsed || typeof parsed !== "object") {
    return [];
  }
  const record = parsed as Record<string, unknown>;
  if (record.schema === STORED_TURN_SCHEMA && Array.isArray(record.events)) {
    return record.events.flatMap((event) => {
      const normalizedEvent = normalizeStoredEvent(event);
      return normalizedEvent ? [normalizedEvent] : [];
    });
  }
  const normalizedEvent = normalizeStoredEvent(record);
  return normalizedEvent ? [normalizedEvent] : [];
}

export function parseStoredTurnBlocks(
  fullMessage: string | null | undefined,
  createBlockId: () => string,
): TurnBlock[] {
  if (!fullMessage) {
    return [];
  }

  let events: SSEEvent[];
  try {
    events = extractStoredEvents(fullMessage);
  } catch {
    return [];
  }

  const blocks: TurnBlock[] = [];
  for (const event of events) {
    applyTurnEventToBlocks(blocks, event, createBlockId);
  }
  return blocks;
}
