import { Type } from "typebox";
import { Check } from "typebox/value";
import { THREAD_OPERATIONS } from "./orchestration_rpc.js";

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

const threadId = Type.String({ minLength: 1, maxLength: 128 });
const boundedText = maximum => Type.String({ maxLength: maximum });
const strictObject = fields => Type.Object(fields, { additionalProperties: false });
const choices = values => Type.Union(values.map(value => Type.Literal(value)));
const threadSchemas = {
  spawn_thread: strictObject({
    title: Type.String({ minLength: 1, maxLength: 200 }),
    task: Type.String({ minLength: 1, maxLength: 20000 }),
    context: Type.Optional(boundedText(20000)),
    role: Type.Optional(choices(["general", "research", "implementation", "test", "review", "debug"])),
    model: Type.Optional(boundedText(200)),
    thinking: Type.Optional(choices(["off", "minimal", "low", "medium", "high", "xhigh"])),
  }),
  list_children: strictObject({ include_terminal: Type.Optional(Type.Boolean({ default: true })) }),
  get_thread: strictObject({ thread_id: threadId, include_result: Type.Optional(Type.Boolean({ default: true })) }),
  wait_for_threads: strictObject({
    thread_ids: Type.Array(threadId, { minItems: 1, maxItems: 20, uniqueItems: true }),
    timeout_seconds: Type.Optional(Type.Integer({ minimum: 0, maximum: 60, default: 60 })),
  }),
  cancel_thread: strictObject({ thread_id: threadId, cascade: Type.Optional(Type.Boolean({ default: true })) }),
  report_thread_result: strictObject({
    summary: Type.String({ minLength: 1, maxLength: 20000 }),
    tests: Type.Optional(Type.Array(strictObject({
      command: Type.String({ minLength: 1, maxLength: 2000 }),
      status: choices(["passed", "failed", "skipped"]),
      summary: Type.Optional(boundedText(4000)),
    }), { maxItems: 50 })),
    warnings: Type.Optional(Type.Array(boundedText(4000), { maxItems: 20 })),
  }),
};
const threadDescriptions = {
  spawn_thread: "Start one independent child thread in a delegated worktree. The backend selects the parent and runtime.",
  list_children: "List direct children, including provisioning placeholders and current limits.",
  get_thread: "Read an authorized descendant's status and bounded result. Full reports remain available through the UI.",
  wait_for_threads: "Wait up to sixty seconds for selected descendants. Cancelling this wait does not cancel children.",
  cancel_thread: "Cancel an authorized descendant. Cascade cancellation includes its active descendants by default.",
  report_thread_result: "Submit this child thread's final report. Test commands are inert claims, not instructions to execute.",
};

export function createThreadTools({ allowedOperations, rpcForCall }) {
  if (!Array.isArray(allowedOperations) || allowedOperations.some(name => !THREAD_OPERATIONS.includes(name))
    || new Set(allowedOperations).size !== allowedOperations.length) {
    throw new TypeError("Invalid thread tool permissions.");
  }
  if (typeof rpcForCall !== "function") {
    throw new TypeError("rpcForCall must be a function");
  }
  return allowedOperations.map(name => ({
    name,
    label: name.replaceAll("_", " "),
    description: threadDescriptions[name],
    parameters: threadSchemas[name],
    async execute(toolCallId, params, signal) {
      if (!Check(threadSchemas[name], params)) {
        throw new Error("Invalid thread tool arguments.");
      }
      const rpc = rpcForCall();
      if (!rpc || typeof rpc.request !== "function") {
        throw new Error("Thread orchestration channel is not active.");
      }
      const result = await rpc.request(name, params, { signal, toolCallId });
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: undefined };
    },
  }));
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
