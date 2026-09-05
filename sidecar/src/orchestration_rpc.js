import { randomUUID } from "node:crypto";

export const ORCHESTRATION_REQUEST_TYPE = "orchestration_request";

// The backend may only be asked operations on this allowlist. Anything else
// is rejected locally and never put on the wire.
export const ORCHESTRATION_OPERATIONS = new Set(["ping_thread_bridge"]);

// Hard bound for one serialized outbound request frame (64 KiB).
export const ORCHESTRATION_FRAME_BYTES_MAX = 64 * 1024;

// Hard cap on in-flight requests per channel.
export const ORCHESTRATION_PENDING_MAX = 16;

// Hard bound for an inbound result payload (256 KiB).
export const ORCHESTRATION_RESULT_BYTES_MAX = 256 * 1024;

// The only keys an orchestration_response frame may carry.
const RESPONSE_ALLOWED_KEYS = new Set([
  "type",
  "id",
  "request_id",
  "ok",
  "result",
  "error",
]);

function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// Fixed error codes; remote error text is never surfaced.
export const ORCHESTRATION_ERROR_CODES = Object.freeze({
  handlerFailed: "handler_failed",
});

export function createOrchestrationRpc({
  sessionId,
  capability,
  send,
  timeoutMs = 60_000,
}) {
  /** Pending map: request_id -> entry. */
  const pending = new Map();
  let closed = false;

  /**
   * Settle exactly once. Every exit path (response, timeout, abort, dispose,
   * send failure) routes through here so cancellation can never double
   * settle, and timers plus abort listeners are always released.
   */
  function settle(entry, error, value) {
    if (entry.settled) {
      return;
    }
    entry.settled = true;
    if (pending.get(entry.requestId) === entry) pending.delete(entry.requestId);
    if (entry.timer) {
      clearTimeout(entry.timer);
      entry.timer = null;
    }
    if (entry.signal && entry.onAbort) {
      entry.signal.removeEventListener("abort", entry.onAbort);
    }
    entry.signal = null;
    entry.onAbort = null;
    if (error) {
      entry.reject(error);
    } else {
      entry.resolve(value);
    }
  }

  function request(operation, args, { signal } = {}) {
    return new Promise((resolve, reject) => {
      // A disposed channel is closed: nothing is created and nothing is sent.
      if (closed) {
        reject(orchestrationError("orchestration_channel_closed"));
        return;
      }
      if (signal !== undefined && !(signal instanceof AbortSignal)) {
        reject(new TypeError("signal must be an AbortSignal"));
        return;
      }
      // An already-aborted signal rejects immediately, before any send.
      if (signal?.aborted) {
        reject(orchestrationError("orchestration_cancelled"));
        return;
      }
      // Strict operation allowlist: unknown operations are refused locally
      // before anything is created or sent.
      if (
        typeof operation !== "string"
        || !ORCHESTRATION_OPERATIONS.has(operation)
      ) {
        reject(orchestrationError("orchestration_operation_not_allowed"));
        return;
      }
      // Arguments must be a plain object: no coercion, no containers.
      if (!isPlainObject(args)) {
        reject(orchestrationError("orchestration_invalid_arguments"));
        return;
      }
      // Hard cap on concurrent pending calls: excess requests are refused
      // locally, never queued and never sent.
      if (pending.size >= ORCHESTRATION_PENDING_MAX) {
        reject(orchestrationError("orchestration_too_many_requests"));
        return;
      }
      const requestId = randomUUID();
      const frame = {
        type: ORCHESTRATION_REQUEST_TYPE,
        id: sessionId,
        request_id: requestId,
        capability,
        operation,
        arguments: args ?? {},
      };
      // Hard frame bound with safe serialization: unserializable arguments
      // and oversized frames are refused locally, never put on the wire.
      let frameBytes;
      try {
        frameBytes = Buffer.byteLength(JSON.stringify(frame)) + 1;
      } catch {
        reject(orchestrationError("orchestration_invalid_arguments"));
        return;
      }
      if (frameBytes > ORCHESTRATION_FRAME_BYTES_MAX) {
        reject(orchestrationError("orchestration_frame_too_large"));
        return;
      }

      const entry = {
        requestId,
        resolve,
        reject,
        timer: null,
        settled: false,
        signal: signal ?? null,
        onAbort: null,
      };
      if (signal) {
        entry.onAbort = () => {
          settle(entry, orchestrationError("orchestration_cancelled"));
        };
        signal.addEventListener("abort", entry.onAbort, { once: true });
      }

      pending.set(requestId, entry);
      entry.timer = setTimeout(() => {
        entry.timer = null;
        settle(entry, orchestrationError("orchestration_timeout"));
      }, timeoutMs);

      try {
        send(frame);
      } catch {
        // Raw send error text (socket internals, tokens, paths) must never
        // surface to callers or models.
        settle(entry, orchestrationError("orchestration_send_failed"));
      }
    });
  }

  /** Route one inbound frame. Returns true when the frame was consumed. */
  function handleFrame(message) {
    if (!message || typeof message !== "object") {
      return false;
    }
    if (message.type !== "orchestration_response") {
      return false;
    }
    // Defense-in-depth: a frame addressed to another session is never
    // consumed by this channel.
    if (message.id !== sessionId) {
      return false;
    }
    if (typeof message.request_id !== "string") {
      return true;
    }
    const entry = pending.get(message.request_id);
    if (!entry) {
      // Unknown, late, or duplicate response. Consumed and dropped.
      return true;
    }
    try {
      if (Buffer.byteLength(JSON.stringify(message)) + 1 > ORCHESTRATION_RESULT_BYTES_MAX) {
        settle(entry, orchestrationError("orchestration_result_too_large"));
        return true;
      }
    } catch {
      settle(entry, orchestrationError("orchestration_bad_response"));
      return true;
    }
    // Strict frame shape: unexpected top-level keys are a protocol
    // violation and fail the pending request closed.
    for (const key of Object.keys(message)) {
      if (!RESPONSE_ALLOWED_KEYS.has(key)) {
        settle(entry, orchestrationError("orchestration_bad_response"));
        return true;
      }
    }
    const hasResult = Object.hasOwn(message, "result");
    const hasError = Object.hasOwn(message, "error");
    if (message.ok === true ? (!hasResult || hasError) : (!hasError || hasResult)) {
      settle(entry, orchestrationError("orchestration_bad_response"));
      return true;
    }
    if (message.ok === true) {
      const result = message.result;
      if (result !== undefined
        && (typeof result !== "object" || result === null || Array.isArray(result))) {
        settle(entry, orchestrationError("orchestration_bad_response"));
        return true;
      }
      settle(entry, null, result ?? {});
    } else if (message.ok === false) {
      // Remote error codes and messages are never trusted or surfaced; the
      // caller always gets one fixed, safe error.
      const error = new Error("The orchestration operation failed.");
      error.code = ORCHESTRATION_ERROR_CODES.handlerFailed;
      settle(entry, error);
    } else {
      // ok must be a strict boolean; anything else is a protocol violation.
      settle(entry, orchestrationError("orchestration_bad_response"));
    }
    return true;
  }

  /** Reject every pending request (query teardown or disconnect). */
  function dispose() {
    // Disposal is idempotent and permanently closes the channel.
    if (closed) {
      return;
    }
    closed = true;
    for (const entry of pending.values()) {
      settle(entry, orchestrationError("orchestration_disconnected"));
    }
    pending.clear();
  }

  return {
    request,
    handleFrame,
    dispose,
    get pendingCount() {
      return pending.size;
    },
  };
}

function orchestrationError(code) {
  let message = "The orchestration operation failed.";
  if (code === "orchestration_bad_response") {
    message = "The orchestration backend returned an invalid response.";
  } else if (code === "orchestration_result_too_large") {
    message = "The orchestration response exceeded the allowed size.";
  } else if (code === "orchestration_operation_not_allowed") {
    message = "The orchestration operation is not allowed.";
  } else if (code === "orchestration_frame_too_large") {
    message = "The orchestration request exceeded the allowed size.";
  } else if (code === "orchestration_invalid_arguments") {
    message = "The orchestration arguments are invalid.";
  } else if (code === "orchestration_channel_closed") {
    message = "The orchestration channel is closed.";
  } else if (code === "orchestration_too_many_requests") {
    message = "Too many orchestration requests are already in flight.";
  } else if (code === "orchestration_cancelled") {
    message = "The orchestration request was cancelled.";
  } else if (code === "orchestration_send_failed") {
    message = "The orchestration request could not be sent.";
  }
  const error = new Error(message);
  error.code = code;
  return error;
}
