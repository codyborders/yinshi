export const HELPER_PROTOCOL_VERSION = 1;

export interface HelperReadyMessage {
  port: number;
  instanceNonce: string;
}

const READY_FIELDS = new Set(["type", "protocolVersion", "port", "instanceNonce"]);
const INSTANCE_NONCE_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;

export function parseHelperReadyLine(line: string): HelperReadyMessage {
  if (typeof line !== "string" || line.length === 0) {
    throw new TypeError("helper readiness must be valid JSON");
  }

  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch {
    throw new TypeError("helper readiness must be valid JSON");
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("helper readiness must be a JSON object");
  }

  const record = value as Record<string, unknown>;
  const fields = Object.keys(record);
  if (fields.length !== READY_FIELDS.size || fields.some((field) => !READY_FIELDS.has(field))) {
    throw new TypeError("helper readiness contains unexpected fields");
  }
  if (record.type !== "ready") {
    throw new TypeError("helper readiness type must be ready");
  }
  if (record.protocolVersion !== HELPER_PROTOCOL_VERSION) {
    throw new TypeError("helper readiness protocol version is unsupported");
  }
  if (!Number.isInteger(record.port) || (record.port as number) < 1 || (record.port as number) > 65535) {
    throw new TypeError("helper readiness port must be an integer between 1 and 65535");
  }
  if (typeof record.instanceNonce !== "string" || !INSTANCE_NONCE_PATTERN.test(record.instanceNonce)) {
    throw new TypeError("helper readiness instance nonce is invalid");
  }

  return {
    port: record.port as number,
    instanceNonce: record.instanceNonce,
  };
}
