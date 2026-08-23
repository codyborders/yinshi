import {
  normalizeEvent,
  type SSEEvent,
  type ThinkingLevel,
} from "../api/client";
import { RunnerRelayConnectionError } from "../runner/encryptedRunnerClient";
import type { RuntimeTransport } from "./runtimeTransport";

const RESOURCE_ID_PATTERN = /^[0-9a-f]{32}$/;
const TERMINAL_STATUSES = new Set([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
]);
const RUN_STATUSES = new Set([
  "starting",
  "running",
  "stopping",
  ...TERMINAL_STATUSES,
]);

type PromptRunStatus =
  | "starting"
  | "running"
  | "stopping"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

interface PromptRunResponse {
  readonly id: string;
  readonly session_id: string;
  readonly status: PromptRunStatus;
}

interface PromptEventBatchResponse {
  readonly run_id: string;
  readonly status: PromptRunStatus;
  readonly events: unknown[];
  readonly next_sequence: number;
}

export interface RuntimePromptOptions {
  readonly prompt: string;
  readonly model?: string;
  readonly thinking?: ThinkingLevel;
  readonly idempotencyKey?: string;
  readonly signal?: AbortSignal;
  readonly pollDelayMs?: number;
  readonly pollRetryLimit?: number;
}

export interface RuntimePromptHandle {
  readonly runId: string;
  events(): AsyncGenerator<SSEEvent>;
  cancel(): Promise<PromptRunStatus>;
}

function validateResourceId(value: unknown, field: string): string {
  if (typeof value !== "string" || !RESOURCE_ID_PATTERN.test(value)) {
    throw new Error(`Prompt ${field} is invalid`);
  }
  return value;
}

function validateStatus(value: unknown): PromptRunStatus {
  if (typeof value !== "string" || !RUN_STATUSES.has(value)) {
    throw new Error("Prompt run status is invalid");
  }
  return value as PromptRunStatus;
}

function validateRun(value: unknown, sessionId: string): PromptRunResponse {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Prompt run response is invalid");
  }
  const run = value as Record<string, unknown>;
  if (Object.keys(run).length !== 3 || run.session_id !== sessionId) {
    throw new Error("Prompt run response did not match the session");
  }
  return {
    id: validateResourceId(run.id, "run ID"),
    session_id: sessionId,
    status: validateStatus(run.status),
  };
}

function validateEvent(value: unknown): SSEEvent {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Prompt journal event is invalid");
  }
  const event = value as Record<string, unknown>;
  if (typeof event.type !== "string" || !event.type) {
    throw new Error("Prompt journal event type is invalid");
  }
  return normalizeEvent(event);
}

function validateBatch(
  value: unknown,
  runId: string,
  expectedSequence: number,
): { status: PromptRunStatus; events: SSEEvent[]; nextSequence: number } {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Prompt event batch is invalid");
  }
  const batch = value as Record<string, unknown>;
  if (
    Object.keys(batch).length !== 4 ||
    batch.run_id !== runId ||
    !Array.isArray(batch.events) ||
    !Number.isSafeInteger(batch.next_sequence)
  ) {
    throw new Error("Prompt event batch did not match the run");
  }
  const events = batch.events.map(validateEvent);
  const nextSequence = batch.next_sequence as number;
  if (nextSequence !== expectedSequence + events.length) {
    throw new Error("Prompt event sequence is not contiguous");
  }
  return {
    status: validateStatus(batch.status),
    events,
    nextSequence,
  };
}

function abortError(): DOMException {
  return new DOMException("Prompt polling aborted", "AbortError");
}

async function delay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) throw abortError();
  if (milliseconds === 0) return;
  await new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    const abort = (): void => {
      window.clearTimeout(timer);
      reject(abortError());
    };
    signal?.addEventListener("abort", abort, { once: true });
  });
}

function shouldRetry(error: unknown): boolean {
  if (error instanceof DOMException && error.name === "AbortError") return false;
  if (error instanceof TypeError || error instanceof RunnerRelayConnectionError) {
    return true;
  }
  if (error !== null && typeof error === "object" && "status" in error) {
    const status = (error as { status?: unknown }).status;
    return (
      typeof status === "number" &&
      (status === 429 || (status >= 500 && status < 600))
    );
  }
  return false;
}

export async function startRuntimePrompt(
  transport: RuntimeTransport,
  sessionIdValue: string,
  options: RuntimePromptOptions,
): Promise<RuntimePromptHandle> {
  if (!transport || typeof transport.post !== "function" || typeof transport.get !== "function") {
    throw new TypeError("Prompt runtime transport is invalid");
  }
  const sessionId = validateResourceId(sessionIdValue, "session ID");
  const prompt = options.prompt.trim();
  if (!prompt || prompt.length > 100_000) {
    throw new Error("Prompt content has an invalid length");
  }
  const idempotencyKey = options.idempotencyKey ?? crypto.randomUUID();
  if (typeof idempotencyKey !== "string" || idempotencyKey.length !== 36) {
    throw new Error("Prompt idempotency key is invalid");
  }
  const remoteRuntime =
    transport.runtime.location === "byoc" || transport.runtime.location === "managed";
  const pollDelayMs = options.pollDelayMs ?? (remoteRuntime ? 750 : 250);
  if (!Number.isSafeInteger(pollDelayMs) || pollDelayMs < 0 || pollDelayMs > 5_000) {
    throw new Error("Prompt poll delay is invalid");
  }
  const pollRetryLimit = options.pollRetryLimit ?? 5;
  if (
    !Number.isSafeInteger(pollRetryLimit) ||
    pollRetryLimit < 1 ||
    pollRetryLimit > 5
  ) {
    throw new Error("Prompt poll retry limit is invalid");
  }
  const started = validateRun(
    await transport.post<unknown>(`/api/sessions/${sessionId}/runs`, {
      prompt,
      model: options.model ?? null,
      thinking: options.thinking ?? null,
      idempotency_key: idempotencyKey,
    }),
    sessionId,
  );
  let consumed = false;

  return {
    runId: started.id,
    async *events(): AsyncGenerator<SSEEvent> {
      if (consumed) {
        throw new Error("Prompt event stream can only be consumed once");
      }
      consumed = true;
      let nextSequence = 0;
      let retryDelayMs = pollDelayMs;
      let consecutiveTransientFailures = 0;
      let emittedTerminalEvent = false;
      while (true) {
        if (options.signal?.aborted) throw abortError();
        let rawBatch: unknown;
        try {
          rawBatch = await transport.get<unknown>(
            `/api/sessions/${sessionId}/runs/${started.id}/events/${nextSequence}`,
          );
        } catch (error) {
          if (!shouldRetry(error)) throw error;
          consecutiveTransientFailures += 1;
          if (consecutiveTransientFailures >= pollRetryLimit) throw error;
          retryDelayMs = Math.min(Math.max(retryDelayMs * 2, 250), 5_000);
          await delay(retryDelayMs, options.signal);
          continue;
        }
        const batch = validateBatch(rawBatch, started.id, nextSequence);
        consecutiveTransientFailures = 0;
        retryDelayMs = pollDelayMs;
        nextSequence = batch.nextSequence;
        for (const event of batch.events) {
          if (event.type === "error" || event.type === "cancelled" || event.type === "result") {
            emittedTerminalEvent = true;
          }
          yield event;
        }
        if (TERMINAL_STATUSES.has(batch.status)) {
          if (emittedTerminalEvent) return;
          if (batch.events.length > 0) continue;
          if (batch.status !== "completed") {
            yield {
              type: "error",
              error:
                batch.status === "interrupted"
                  ? "Prompt run was interrupted"
                  : `Prompt run ended with status ${batch.status}`,
            };
          }
          return;
        }
        await delay(pollDelayMs, options.signal);
      }
    },
    async cancel(): Promise<PromptRunStatus> {
      const cancelled = validateRun(
        await transport.post<unknown>(
          `/api/sessions/${sessionId}/runs/${started.id}/cancel`,
        ),
        sessionId,
      );
      if (cancelled.id !== started.id) {
        throw new Error("Prompt cancellation response did not match the run");
      }
      return cancelled.status;
    },
  };
}
