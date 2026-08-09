// Covers helper readiness parsing with strict version, port, nonce, and shape validation.

import { describe, expect, it } from "vitest";

import { HELPER_PROTOCOL_VERSION, parseHelperReadyLine } from "./helperProtocol.js";

describe("parseHelperReadyLine", () => {
  it("accepts only the exact supported readiness contract", () => {
    const line = JSON.stringify({
      type: "ready",
      protocolVersion: HELPER_PROTOCOL_VERSION,
      port: 43123,
      instanceNonce: "abcdefghijklmnopqrstuvwxyz_1234567890-ABCD",
    });

    expect(parseHelperReadyLine(line)).toEqual({
      port: 43123,
      instanceNonce: "abcdefghijklmnopqrstuvwxyz_1234567890-ABCD",
    });
  });

  it.each([
    { line: "not-json", error: "valid JSON" },
    { line: "[]", error: "JSON object" },
    {
      line: JSON.stringify({
        type: "started",
        protocolVersion: HELPER_PROTOCOL_VERSION,
        port: 43123,
        instanceNonce: "abcdefghijklmnopqrstuvwxyz_1234567890-ABCD",
      }),
      error: "type must be ready",
    },
    {
      line: JSON.stringify({
        type: "ready",
        protocolVersion: 99,
        port: 43123,
        instanceNonce: "abcdefghijklmnopqrstuvwxyz_1234567890-ABCD",
      }),
      error: "protocol version is unsupported",
    },
    {
      line: JSON.stringify({
        type: "ready",
        protocolVersion: HELPER_PROTOCOL_VERSION,
        port: 70000,
        instanceNonce: "abcdefghijklmnopqrstuvwxyz_1234567890-ABCD",
      }),
      error: "port must be an integer",
    },
    {
      line: JSON.stringify({
        type: "ready",
        protocolVersion: HELPER_PROTOCOL_VERSION,
        port: 43123,
        instanceNonce: "short",
      }),
      error: "instance nonce is invalid",
    },
    {
      line: JSON.stringify({
        type: "ready",
        protocolVersion: HELPER_PROTOCOL_VERSION,
        port: 43123,
        instanceNonce: "abcdefghijklmnopqrstuvwxyz_1234567890-ABCD",
        userInput: "CANARY_PRIVATE_PATH",
      }),
      error: "unexpected fields",
    },
  ])("rejects invalid helper input: $error", ({ line, error }) => {
    expect(() => parseHelperReadyLine(line)).toThrow(error);
  });
});
