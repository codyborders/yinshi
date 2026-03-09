import { useCallback, useRef, useState } from "react";
import { cancelSession, streamPrompt, type SSEEvent } from "../api/client";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "tool_use" | "result" | "error";
  content: string;
  toolName?: string;
  toolInput?: unknown;
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

      // Add user message to state
      setMessages((prev) => [
        ...prev,
        {
          id: nextId(),
          role: "user",
          content: prompt,
          timestamp: Date.now(),
        },
      ]);

      setStreaming(true);
      const controller = new AbortController();
      abortRef.current = controller;

      let assistantBuf = "";
      let assistantMsgId: string | null = null;

      try {
        for await (const event of streamPrompt(
          sessionId,
          prompt,
          model,
          controller.signal,
        )) {
          if (event.type === "error") {
            setMessages((prev) => [
              ...prev,
              {
                id: nextId(),
                role: "error",
                content: event.error,
                timestamp: Date.now(),
              },
            ]);
            break;
          }

          if (event.type === "assistant") {
            const blocks = event.message?.content ?? [];
            let text = "";
            const toolUseBlocks: { name: string; input: unknown }[] = [];

            for (const block of blocks) {
              if (block.type === "text" && block.text) {
                text += block.text;
              } else if (block.type === "tool_use" && block.name) {
                toolUseBlocks.push({ name: block.name, input: block.input });
              }
            }

            if (text) {
              assistantBuf += text;
              const mid: string = assistantMsgId ?? nextId();
              assistantMsgId = mid;

              setMessages((prev) => {
                const updated: ChatMessage = {
                  id: mid,
                  role: "assistant",
                  content: assistantBuf,
                  streaming: true,
                  timestamp: Date.now(),
                };
                const existing = prev.findIndex((m) => m.id === mid);
                if (existing >= 0) {
                  const next = [...prev];
                  next[existing] = updated;
                  return next;
                }
                return [...prev, updated];
              });
            }

            for (const tool of toolUseBlocks) {
              setMessages((prev) => [
                ...prev,
                {
                  id: nextId(),
                  role: "tool_use",
                  content: "",
                  toolName: tool.name,
                  toolInput: tool.input,
                  timestamp: Date.now(),
                },
              ]);
            }
          } else if (event.type === "tool_use") {
            setMessages((prev) => [
              ...prev,
              {
                id: nextId(),
                role: "tool_use",
                content: "",
                toolName: event.name || event.tool_name || "unknown",
                toolInput: event.input,
                timestamp: Date.now(),
              },
            ]);
          } else if (event.type === "result") {
            // Finalize the assistant message as non-streaming
            if (assistantMsgId) {
              const mid = assistantMsgId;
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === mid ? { ...m, streaming: false } : m,
                ),
              );
            }
          }
        }
      } catch (e) {
        if (!(e instanceof DOMException && e.name === "AbortError")) {
          setMessages((prev) => [
            ...prev,
            {
              id: nextId(),
              role: "error",
              content: e instanceof Error ? e.message : "Stream failed",
              timestamp: Date.now(),
            },
          ]);
        }
      } finally {
        setStreaming(false);
        abortRef.current = null;
      }
    },
    [sessionId, streaming],
  );

  const cancel = useCallback(async () => {
    // Abort the fetch to stop reading the stream
    abortRef.current?.abort();
    setStreaming(false);
    // Tell the backend to cancel the sidecar
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
