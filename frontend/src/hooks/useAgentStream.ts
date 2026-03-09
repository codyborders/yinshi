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

      try {
        for await (const event of streamPrompt(
          sessionId,
          prompt,
          model,
          controller.signal,
        )) {
          if (event.type === "error") {
            blocks.push({ id: nextId(), type: "error", text: event.error });
            upsertTurn(true);
            break;
          }

          if (event.type === "assistant") {
            for (const block of event.message?.content ?? []) {
              if (block.type === "text" && block.text) {
                const last = blocks[blocks.length - 1];
                if (last && last.type === "text") {
                  last.text = (last.text || "") + block.text;
                } else {
                  blocks.push({ id: nextId(), type: "text", text: block.text });
                }
              } else if (
                block.type === "thinking" &&
                (block.thinking || block.text)
              ) {
                const text = block.thinking || block.text || "";
                const last = blocks[blocks.length - 1];
                if (last && last.type === "thinking") {
                  last.text = (last.text || "") + text;
                } else {
                  blocks.push({ id: nextId(), type: "thinking", text });
                }
              } else if (block.type === "tool_use") {
                const b = block as unknown as Record<string, unknown>;
                const name =
                  (b.name as string) ||
                  (b.tool_name as string) ||
                  (b.tool as string) ||
                  "unknown";
                blocks.push({
                  id: block.id || nextId(),
                  type: "tool_use",
                  toolName: name,
                  toolInput: block.input,
                  toolId: block.id,
                });
              }
            }
            upsertTurn();
          } else if (event.type === "tool_use") {
            // Standalone tool_use event -- sidecar uses camelCase toolName
            const ev = event as Record<string, unknown>;
            const toolName =
              (ev.toolName as string) ||
              (ev.name as string) ||
              (ev.tool_name as string) ||
              "unknown";
            blocks.push({
              id: (ev.id as string) || nextId(),
              type: "tool_use",
              toolName,
              toolInput: ev.toolInput ?? ev.input,
              toolId: ev.id as string,
            });
            upsertTurn();
          } else if (
            (event as Record<string, unknown>).type === "content_block_start"
          ) {
            // Anthropic streaming format: content_block_start with nested content_block
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
              upsertTurn();
            } else if (cb?.type === "thinking") {
              blocks.push({
                id: nextId(),
                type: "thinking",
                text: (cb.thinking as string) || "",
              });
              upsertTurn();
            }
          } else if (
            (event as Record<string, unknown>).type === "content_block_delta"
          ) {
            const ev = event as Record<string, unknown>;
            const delta = ev.delta as Record<string, unknown> | undefined;
            if (delta?.type === "text_delta" && delta.text) {
              const last = blocks[blocks.length - 1];
              if (last && last.type === "text") {
                last.text = (last.text || "") + (delta.text as string);
              } else {
                blocks.push({
                  id: nextId(),
                  type: "text",
                  text: delta.text as string,
                });
              }
              upsertTurn();
            } else if (delta?.type === "input_json_delta") {
              // Tool input streaming -- find last tool_use block and append
              const last = blocks[blocks.length - 1];
              if (last && last.type === "tool_use") {
                const partial = (delta.partial_json as string) || "";
                last.toolInput =
                  ((last.toolInput as string) || "") + partial;
              }
              upsertTurn();
            } else if (delta?.type === "thinking_delta" && delta.thinking) {
              const last = blocks[blocks.length - 1];
              if (last && last.type === "thinking") {
                last.text = (last.text || "") + (delta.thinking as string);
              } else {
                blocks.push({
                  id: nextId(),
                  type: "thinking",
                  text: delta.thinking as string,
                });
              }
              upsertTurn();
            }
          } else if (
            (event as Record<string, unknown>).type === "content_block_stop"
          ) {
            // Finalize tool input if it was streamed as JSON string
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
            upsertTurn();
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
            upsertTurn();
          } else if (
            event.type === "result" ||
            (event as Record<string, unknown>).type === "message_stop"
          ) {
            upsertTurn(true);
          }
        }
      } catch (e) {
        if (!(e instanceof DOMException && e.name === "AbortError")) {
          blocks.push({
            id: nextId(),
            type: "error",
            text: e instanceof Error ? e.message : "Stream failed",
          });
          upsertTurn(true);
        }
      } finally {
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
