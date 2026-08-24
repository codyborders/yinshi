import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelSession,
  streamPrompt,
  type SSEEvent,
  type ThinkingLevel,
} from "../api/client";
import {
  attachRuntimePrompt,
  startRuntimePrompt,
  type RuntimePromptHandle,
} from "../runtime/promptStream";
import type { RuntimeTransport } from "../runtime/runtimeTransport";
import {
  applyTurnEventToBlocks,
  blocksToContent,
  type TurnBlock,
  type TurnStatus,
} from "../utils/turnEvents";

export type { TurnBlock } from "../utils/turnEvents";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "error";
  content: string;
  blocks: TurnBlock[];
  streaming?: boolean;
  turnStatus?: TurnStatus;
  turnId?: string | null;
  timestamp: number;
}

let messageIdCounter = 0;
function nextId(): string {
  return `msg-${Date.now()}-${++messageIdCounter}`;
}

export type RunState = "idle" | "running" | "stopping";

const CANCELLATION_ERROR_MESSAGE =
  "Could not stop the current response. Try again.";

interface QueuedPrompt {
  prompt: string;
  model?: string;
  thinking?: ThinkingLevel;
}

export function useAgentStream(
  sessionId: string | undefined,
  runtimeTransport?: RuntimeTransport,
) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [runState, setRunState] = useState<RunState>("idle");
  const runStateRef = useRef<RunState>("idle");
  const abortRef = useRef<AbortController | null>(null);
  const promptHandleRef = useRef<RuntimePromptHandle | null>(null);
  const promptStartRef = useRef<Promise<RuntimePromptHandle> | null>(null);
  const cancelInFlightRef = useRef(false);
  const queuedPromptRef = useRef<QueuedPrompt | null>(null);
  const generationRef = useRef(0);
  const operationRef = useRef(0);
  const contextRef = useRef({ sessionId, runtimeTransport });
  contextRef.current = { sessionId, runtimeTransport };

  const updateRunState = useCallback((state: RunState) => {
    runStateRef.current = state;
    setRunState(state);
  }, []);

  const isCurrent = useCallback(
    (generation: number, operation: number): boolean =>
      generationRef.current === generation &&
      operationRef.current === operation,
    [],
  );

  useEffect(() => {
    generationRef.current += 1;
    operationRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    promptHandleRef.current = null;
    promptStartRef.current = null;
    queuedPromptRef.current = null;
    cancelInFlightRef.current = false;
    runStateRef.current = "idle";
    setRunState("idle");
    setMessages([]);

    return () => {
      generationRef.current += 1;
      operationRef.current += 1;
      abortRef.current?.abort();
      abortRef.current = null;
      promptHandleRef.current = null;
      promptStartRef.current = null;
      queuedPromptRef.current = null;
      cancelInFlightRef.current = false;
    };
  }, [runtimeTransport, sessionId]);

  const consumeEvents = useCallback(
    async (
      eventSource: AsyncIterable<SSEEvent>,
      turnId: string,
      generation: number,
      operation: number,
    ): Promise<void> => {
      const blocks: TurnBlock[] = [];
      let rafId: number | null = null;
      let turnStatus: TurnStatus = "completed";

      const upsertTurn = (done = false): void => {
        if (!isCurrent(generation, operation)) return;
        const allText = blocksToContent(blocks);
        const snapshot = blocks.map((block) => ({ ...block }));
        setMessages((previous) => {
          if (!isCurrent(generation, operation)) return previous;
          const message: ChatMessage = {
            id: turnId,
            role: "assistant",
            content: allText,
            blocks: snapshot,
            streaming: !done,
            turnStatus: done ? turnStatus : undefined,
            timestamp: Date.now(),
          };
          const index = previous.findIndex((entry) => entry.id === turnId);
          if (index < 0) return [...previous, message];
          const next = [...previous];
          next[index] = message;
          return next;
        });
      };

      const scheduleUpsert = (done = false): void => {
        if (!isCurrent(generation, operation)) return;
        if (done) {
          if (rafId !== null) cancelAnimationFrame(rafId);
          rafId = null;
          upsertTurn(true);
          return;
        }
        if (rafId !== null) return;
        rafId = requestAnimationFrame(() => {
          rafId = null;
          upsertTurn(false);
        });
      };

      try {
        for await (const event of eventSource) {
          if (!isCurrent(generation, operation)) return;
          const applyResult = applyTurnEventToBlocks(blocks, event, nextId);
          if (applyResult.status) {
            turnStatus = applyResult.status;
            scheduleUpsert(true);
          } else if (applyResult.changed) {
            scheduleUpsert();
          }
        }
        scheduleUpsert(true);
      } catch (error) {
        if (!isCurrent(generation, operation)) return;
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          blocks.push({
            id: nextId(),
            type: "error",
            text: error instanceof Error ? error.message : "Stream failed",
          });
          turnStatus = "failed";
          scheduleUpsert(true);
        }
      } finally {
        if (rafId !== null) cancelAnimationFrame(rafId);
      }
    },
    [isCurrent],
  );

  const startPrompt = useCallback(
    async (prompt: string, model?: string, thinking?: ThinkingLevel) => {
      if (!sessionId) return;
      const selectedSessionId = sessionId;
      const selectedTransport = runtimeTransport;
      const normalizedPrompt = prompt.trim();
      if (!normalizedPrompt) return;
      if (
        contextRef.current.sessionId !== selectedSessionId ||
        contextRef.current.runtimeTransport !== selectedTransport
      ) {
        return;
      }

      const generation = generationRef.current;
      const operation = ++operationRef.current;
      setMessages((previous) => [
        ...previous,
        {
          id: nextId(),
          role: "user",
          content: normalizedPrompt,
          blocks: [],
          timestamp: Date.now(),
        },
      ]);
      updateRunState("running");
      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const promptStart = selectedTransport
          ? startRuntimePrompt(selectedTransport, selectedSessionId, {
              prompt: normalizedPrompt,
              model,
              thinking,
              signal: controller.signal,
            })
          : null;
        promptStartRef.current = promptStart;
        const runtimePrompt = promptStart ? await promptStart : null;
        if (!isCurrent(generation, operation)) return;
        promptHandleRef.current = runtimePrompt;
        const eventSource = runtimePrompt
          ? runtimePrompt.events()
          : streamPrompt(
              selectedSessionId,
              normalizedPrompt,
              model,
              thinking,
              controller.signal,
            );
        await consumeEvents(
          eventSource,
          runtimePrompt?.runId ?? nextId(),
          generation,
          operation,
        );
      } catch (error) {
        if (
          isCurrent(generation, operation) &&
          !(error instanceof DOMException && error.name === "AbortError")
        ) {
          const text = error instanceof Error ? error.message : "Stream failed";
          setMessages((previous) => [
            ...previous,
            {
              id: nextId(),
              role: "assistant",
              content: "",
              blocks: [{ id: nextId(), type: "error", text }],
              streaming: false,
              turnStatus: "failed",
              timestamp: Date.now(),
            },
          ]);
        }
      } finally {
        if (!isCurrent(generation, operation)) return;
        abortRef.current = null;
        promptHandleRef.current = null;
        promptStartRef.current = null;
        const queuedPrompt = queuedPromptRef.current;
        queuedPromptRef.current = null;
        updateRunState("idle");
        if (queuedPrompt) {
          void startPrompt(
            queuedPrompt.prompt,
            queuedPrompt.model,
            queuedPrompt.thinking,
          );
        }
      }
    },
    [consumeEvents, isCurrent, runtimeTransport, sessionId, updateRunState],
  );

  const resumePrompt = useCallback(
    async (runId: string): Promise<void> => {
      if (!sessionId || !runtimeTransport) return;
      const selectedSessionId = sessionId;
      const selectedTransport = runtimeTransport;
      if (
        contextRef.current.sessionId !== selectedSessionId ||
        contextRef.current.runtimeTransport !== selectedTransport
      ) {
        return;
      }
      const generation = generationRef.current;
      const operation = ++operationRef.current;
      const controller = new AbortController();
      abortRef.current = controller;
      const runtimePrompt = attachRuntimePrompt(
        selectedTransport,
        selectedSessionId,
        runId,
        { signal: controller.signal },
      );
      promptHandleRef.current = runtimePrompt;
      updateRunState("running");
      try {
        await consumeEvents(
          runtimePrompt.events(),
          runtimePrompt.runId,
          generation,
          operation,
        );
      } finally {
        if (!isCurrent(generation, operation)) return;
        abortRef.current = null;
        promptHandleRef.current = null;
        promptStartRef.current = null;
        const queuedPrompt = queuedPromptRef.current;
        queuedPromptRef.current = null;
        updateRunState("idle");
        if (queuedPrompt) {
          void startPrompt(
            queuedPrompt.prompt,
            queuedPrompt.model,
            queuedPrompt.thinking,
          );
        }
      }
    },
    [
      consumeEvents,
      isCurrent,
      runtimeTransport,
      sessionId,
      startPrompt,
      updateRunState,
    ],
  );

  const bootstrapSession = useCallback(
    (history: ChatMessage[], activeRunId: string | null): void => {
      if (
        contextRef.current.sessionId !== sessionId ||
        contextRef.current.runtimeTransport !== runtimeTransport
      ) {
        return;
      }
      operationRef.current += 1;
      abortRef.current?.abort();
      abortRef.current = null;
      promptHandleRef.current = null;
      promptStartRef.current = null;
      queuedPromptRef.current = null;
      cancelInFlightRef.current = false;
      updateRunState("idle");
      setMessages(history);
      if (activeRunId !== null) void resumePrompt(activeRunId);
    },
    [resumePrompt, runtimeTransport, sessionId, updateRunState],
  );

  const cancel = useCallback(async () => {
    if (runStateRef.current !== "running" || cancelInFlightRef.current) return;
    const generation = generationRef.current;
    const operation = operationRef.current;
    cancelInFlightRef.current = true;
    const activeController = abortRef.current;
    updateRunState("stopping");
    try {
      if (sessionId) {
        if (runtimeTransport) {
          const promptHandle =
            promptHandleRef.current ?? (await promptStartRef.current);
          if (promptHandle) await promptHandle.cancel();
        } else {
          await cancelSession(sessionId);
        }
      }
    } catch {
      if (!isCurrent(generation, operation)) return;
      if (activeController !== null && abortRef.current === activeController) {
        updateRunState("running");
      }
      setMessages((previous) => [
        ...previous,
        {
          id: nextId(),
          role: "error",
          content: CANCELLATION_ERROR_MESSAGE,
          blocks: [],
          timestamp: Date.now(),
        },
      ]);
    } finally {
      if (isCurrent(generation, operation)) cancelInFlightRef.current = false;
    }
  }, [isCurrent, runtimeTransport, sessionId, updateRunState]);

  const sendPrompt = useCallback(
    async (prompt: string, model?: string, thinking?: ThinkingLevel) => {
      if (!sessionId) return;
      const normalizedPrompt = prompt.trim();
      if (!normalizedPrompt) return;

      if (runStateRef.current === "running") {
        queuedPromptRef.current = { prompt: normalizedPrompt, model, thinking };
        await cancel();
        return;
      }
      if (runStateRef.current === "stopping") {
        queuedPromptRef.current = { prompt: normalizedPrompt, model, thinking };
        return;
      }
      await startPrompt(normalizedPrompt, model, thinking);
    },
    [cancel, sessionId, startPrompt],
  );

  return {
    messages,
    sendPrompt,
    cancel,
    runState,
    setMessages,
    bootstrapSession,
    streaming: runState === "running" || runState === "stopping",
  };
}
