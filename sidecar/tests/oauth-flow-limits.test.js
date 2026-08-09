// Covers OAuth flow expiry and per-process admission limits without provider network calls.

import assert from "node:assert/strict";
import test from "node:test";

import { YinshiSidecar } from "../src/sidecar.js";

function writableMessages() {
  const messages = [];
  return {
    messages,
    socket: {
      writable: true,
      write(value) {
        messages.push(JSON.parse(String(value).trim()));
        return true;
      },
    },
  };
}

test("expired OAuth flows are removed and pending input is rejected", () => {
  const sidecar = new YinshiSidecar();
  let rejectionMessage = null;
  sidecar.activeOAuthFlows.set("expired", {
    createdAtMs: 1,
    manualInputReject(error) {
      rejectionMessage = error.message;
    },
    manualInputResolve: () => undefined,
  });
  sidecar.activeOAuthFlows.set("current", {
    createdAtMs: 2_000_000,
    manualInputReject: null,
    manualInputResolve: null,
  });

  sidecar._pruneExpiredOAuthFlows(2_000_000);

  assert.equal(sidecar.activeOAuthFlows.has("expired"), false);
  assert.equal(sidecar.activeOAuthFlows.has("current"), true);
  assert.equal(rejectionMessage, "OAuth flow expired");
});

test("OAuth flow admission stops before a ninth provider login", async () => {
  const sidecar = new YinshiSidecar();
  for (let index = 0; index < 8; index += 1) {
    sidecar.activeOAuthFlows.set(`flow-${index}`, {
      createdAtMs: Date.now(),
      manualInputReject: null,
      manualInputResolve: null,
    });
  }
  const { messages, socket } = writableMessages();

  await sidecar.startOAuthFlow("request-1", socket, "openai-codex");

  assert.equal(sidecar.activeOAuthFlows.size, 8);
  assert.equal(messages.length, 1);
  assert.deepEqual(messages[0], {
    id: "request-1",
    type: "error",
    error: "Too many active OAuth flows",
  });
});
