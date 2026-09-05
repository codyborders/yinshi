import { Type } from "typebox";

// Maximum length for the optional echo message, shared by the schema and the
// runtime validation so neither can drift from the other.
export const THREAD_BRIDGE_MESSAGE_MAX = 256;

const ALLOWED_PARAM_KEYS = new Set(["message"]);

const pingSchema = Type.Object(
  {
    message: Type.Optional(
      Type.String({
        description: "Optional short message echoed back by the backend.",
        maxLength: THREAD_BRIDGE_MESSAGE_MAX,
      }),
    ),
  },
  { additionalProperties: false },
);

/**
 * Validate tool parameters strictly. No silent coercion: wrong types, unknown
 * keys, and oversized values are rejected with a fixed, model-safe error.
 */
function validatePingParams(params) {
  if (params === null || typeof params !== "object" || Array.isArray(params)) {
    throw new Error("Invalid bridge arguments: expected an object.");
  }
  for (const key of Object.keys(params)) {
    if (!ALLOWED_PARAM_KEYS.has(key)) {
      throw new Error("Invalid bridge arguments: unknown parameter.");
    }
  }
  const message = params.message;
  if (message !== undefined) {
    if (typeof message !== "string") {
      throw new Error("Invalid bridge arguments: message must be a string.");
    }
    if (message.length > THREAD_BRIDGE_MESSAGE_MAX) {
      throw new Error(
        "Invalid bridge arguments: message must be at most 256 characters.",
      );
    }
  }
  return message === undefined ? {} : { message };
}

export function createThreadBridgePingTool({ rpcForCall }) {
  if (typeof rpcForCall !== "function") {
    throw new TypeError("rpcForCall must be a function");
  }
  return {
    name: "thread_bridge_ping",
    label: "Thread bridge ping",
    description:
      "Harmless round-trip check of the Yinshi thread orchestration bridge.",
    parameters: pingSchema,
    async execute(_toolCallId, params, signal, _onUpdate, _ctx) {
      const args = validatePingParams(params);
      const rpc = rpcForCall();
      if (!rpc) {
        throw new Error(
          "Thread bridge ping failed: the orchestration channel is not active.",
        );
      }
      // Forward the caller's cancellation signal so an aborted prompt
      // settles the pending backend request immediately.
      const result = await rpc.request("ping_thread_bridge", args, { signal });
      return {
        content: [{ type: "text", text: JSON.stringify(result) }],
        details: undefined,
      };
    },
  };
}
