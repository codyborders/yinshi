import { useCallback, useEffect, useRef, useState } from "react";
import { AgentSocket, type WSEvent } from "../api/client";

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

export function useWebSocket(sessionId: string | undefined) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [connected, setConnected] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const socketRef = useRef<AgentSocket | null>(null);
  const assistantBuf = useRef<string>("");
  const assistantMsgId = useRef<string | null>(null);

  useEffect(() => {
    if (!sessionId) return;

    const socket = new AgentSocket(sessionId);
    socketRef.current = socket;

    const unsub = socket.on((event: WSEvent) => {
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
        setStreaming(false);
        return;
      }

      if (event.type === "message") {
        const data = event.data;

        if (data.type === "assistant") {
          const blocks = data.message?.content ?? [];
          let text = "";
          for (const block of blocks) {
            if (block.type === "text" && block.text) {
              text += block.text;
            }
          }

          if (text) {
            assistantBuf.current += text;
            const mid = assistantMsgId.current ?? nextId();
            assistantMsgId.current = mid;

            setMessages((prev) => {
              const existing = prev.findIndex((m) => m.id === mid);
              const updated: ChatMessage = {
                id: mid,
                role: "assistant",
                content: assistantBuf.current,
                streaming: true,
                timestamp: Date.now(),
              };
              if (existing >= 0) {
                const next = [...prev];
                next[existing] = updated;
                return next;
              }
              return [...prev, updated];
            });
          }
        } else if (data.type === "tool_use") {
          const toolData = data as { type: string; tool_name: string; input: unknown };
          setMessages((prev) => [
            ...prev,
            {
              id: nextId(),
              role: "tool_use",
              content: "",
              toolName: toolData.tool_name,
              toolInput: toolData.input,
              timestamp: Date.now(),
            },
          ]);
        } else if (data.type === "result") {
          // Finalize the assistant message as non-streaming
          if (assistantMsgId.current) {
            const mid = assistantMsgId.current;
            setMessages((prev) =>
              prev.map((m) =>
                m.id === mid ? { ...m, streaming: false } : m,
              ),
            );
          }
          assistantBuf.current = "";
          assistantMsgId.current = null;
          setStreaming(false);
        }

        // Update connected state from connect notification
        if ("connected" in data && data.connected) {
          setConnected(true);
        }
      }
    });

    socket.connect();

    return () => {
      unsub();
      socket.disconnect();
      socketRef.current = null;
    };
  }, [sessionId]);

  useEffect(() => {
    if (!socketRef.current) return;
    setConnected(socketRef.current.connected);
  }, [sessionId]);

  const sendPrompt = useCallback(
    (prompt: string, model?: string) => {
      if (!socketRef.current) return;
      // Add user message
      setMessages((prev) => [
        ...prev,
        {
          id: nextId(),
          role: "user",
          content: prompt,
          timestamp: Date.now(),
        },
      ]);
      assistantBuf.current = "";
      assistantMsgId.current = null;
      setStreaming(true);
      socketRef.current.sendPrompt(prompt, model);
    },
    [],
  );

  const cancel = useCallback(() => {
    socketRef.current?.cancel();
    setStreaming(false);
  }, []);

  return { messages, sendPrompt, cancel, connected, streaming, setMessages };
}
