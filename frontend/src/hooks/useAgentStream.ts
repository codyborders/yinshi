import { useCallback, useRef, useState } from "react";
import { cancelSession, streamPrompt, type SSEEvent } from "../api/client";

export interface TurnBlock {
  id: string;
  type: "text" | "thinking" | "tool_use" | "error";
  text?: string;
  toolName?: string;
  toolInput?: unknown;
  toolId?: string;
  toolOutput?: string;
  toolError?: boolean;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "error";
  content: string;
  blocks: TurnBlock[];
  streaming?: boolean;
  timestamp: number;
}

let messageIdCounter = 0;
function nextId(): string {
  return `msg-${Date.now()}-${++messageIdCounter}`;
}

/** Append text to the last block if it matches type, otherwise create a new block. */
function appendOrCreate(blocks: TurnBlock[], type: "text" | "thinking", text: string) {
  const last = blocks[blocks.length - 1];
  if (last && last.type === type) {
    last.text = (last.text || "") + text;
  } else {
    blocks.push({ id: nextId(), type, text });
  }
}

export function useAgentStream(sessionId: string | undefined) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const sendPrompt = useCallback(
    async (prompt: string, model?: string) => {
      if (!sessionId || streaming) return;

      setMessages((prev) => [
        ...prev,
        {
          id: nextId(),
          role: "user",
          content: prompt,
          blocks: [],
          timestamp: Date.now(),
        },
      ]);

      setStreaming(true);
      const controller = new AbortController();
      abortRef.current = controller;

      const turnId = nextId();
      const blocks: TurnBlock[] = [];
      let rafId: number | null = null;

      function upsertTurn(done = false) {
        const allText = blocks
          .filter((b) => b.type === "text")
          .map((b) => b.text || "")
          .join("");
        const snapshot = blocks.map((b) => ({ ...b }));

        setMessages((prev) => {
          const msg: ChatMessage = {
            id: turnId,
            role: "assistant",
            content: allText,
            blocks: snapshot,
            streaming: !done,
            timestamp: Date.now(),
          };
          const idx = prev.findIndex((m) => m.id === turnId);
          if (idx >= 0) {
            const next = [...prev];
            next[idx] = msg;
            return next;
          }
          return [...prev, msg];
        });
      }

      function scheduleUpsert(done = false) {
        if (done) {
          if (rafId) cancelAnimationFrame(rafId);
          rafId = null;
          upsertTurn(true);
          return;
        }
        if (rafId) return;
        rafId = requestAnimationFrame(() => {
          rafId = null;
          upsertTurn(false);
        });
      }

      try {
        for await (const event of streamPrompt(
          sessionId,
          prompt,
          model,
          controller.signal,
        )) {
          if (event.type === "error") {
            blocks.push({ id: nextId(), type: "error", text: event.error });
            scheduleUpsert(true);
            break;
          }

          if (event.type === "assistant") {
            for (const block of event.message?.content ?? []) {
              if (block.type === "text" && block.text) {
                appendOrCreate(blocks, "text", block.text);
              } else if (
                block.type === "thinking" &&
                (block.thinking || block.text)
              ) {
                appendOrCreate(blocks, "thinking", block.thinking || block.text || "");
              } else if (block.type === "tool_use") {
                blocks.push({
                  id: block.id || nextId(),
                  type: "tool_use",
                  toolName: block.name || "unknown",
                  toolInput: block.input,
                  toolId: block.id,
                });
              }
            }
            scheduleUpsert();
          } else if (event.type === "tool_use") {
            blocks.push({
              id: event.id || nextId(),
              type: "tool_use",
              toolName: event.name || "unknown",
              toolInput: event.input,
              toolId: event.id,
            });
            scheduleUpsert();
          } else if (
            (event as Record<string, unknown>).type === "content_block_start"
          ) {
            const ev = event as Record<string, unknown>;
            const cb = ev.content_block as Record<string, unknown> | undefined;
            if (cb?.type === "tool_use" && cb.name) {
              blocks.push({
                id: (cb.id as string) || nextId(),
                type: "tool_use",
                toolName: cb.name as string,
                toolInput: cb.input,
                toolId: cb.id as string,
              });
              scheduleUpsert();
            } else if (cb?.type === "thinking") {
              blocks.push({
                id: nextId(),
                type: "thinking",
                text: (cb.thinking as string) || "",
              });
              scheduleUpsert();
            }
          } else if (
            (event as Record<string, unknown>).type === "content_block_delta"
          ) {
            const ev = event as Record<string, unknown>;
            const delta = ev.delta as Record<string, unknown> | undefined;
            if (delta?.type === "text_delta" && delta.text) {
              appendOrCreate(blocks, "text", delta.text as string);
              scheduleUpsert();
            } else if (delta?.type === "input_json_delta") {
              const last = blocks[blocks.length - 1];
              if (last && last.type === "tool_use") {
                const partial = (delta.partial_json as string) || "";
                last.toolInput =
                  ((last.toolInput as string) || "") + partial;
              }
              scheduleUpsert();
            } else if (delta?.type === "thinking_delta" && delta.thinking) {
              appendOrCreate(blocks, "thinking", delta.thinking as string);
              scheduleUpsert();
            }
          } else if (
            (event as Record<string, unknown>).type === "content_block_stop"
          ) {
            const last = blocks[blocks.length - 1];
            if (
              last &&
              last.type === "tool_use" &&
              typeof last.toolInput === "string"
            ) {
              try {
                last.toolInput = JSON.parse(last.toolInput);
              } catch {
                /* keep as string */
              }
            }
            scheduleUpsert();
          } else if (event.type === "tool_result") {
            const output =
              typeof event.content === "string"
                ? event.content
                : JSON.stringify(event.content, null, 2);
            const matching = blocks.find(
              (b) =>
                b.type === "tool_use" && b.toolId === event.tool_use_id,
            );
            if (matching) {
              matching.toolOutput = output;
              matching.toolError = event.is_error;
            }
            scheduleUpsert();
          } else if (
            event.type === "result" ||
            (event as Record<string, unknown>).type === "message_stop"
          ) {
            scheduleUpsert(true);
          }
        }
      } catch (e) {
        if (!(e instanceof DOMException && e.name === "AbortError")) {
          blocks.push({
            id: nextId(),
            type: "error",
            text: e instanceof Error ? e.message : "Stream failed",
          });
          scheduleUpsert(true);
        }
      } finally {
        if (rafId) cancelAnimationFrame(rafId);
        setStreaming(false);
        abortRef.current = null;
      }
    },
    [sessionId, streaming],
  );

  const cancel = useCallback(async () => {
    abortRef.current?.abort();
    setStreaming(false);
    if (sessionId) {
      try {
        await cancelSession(sessionId);
      } catch {
        /* best-effort */
      }
    }
  }, [sessionId]);

  return { messages, sendPrompt, cancel, streaming, setMessages };
}
