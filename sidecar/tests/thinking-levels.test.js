import assert from "node:assert/strict";
import test from "node:test";

import { ModelRegistry, ModelRuntime } from "@earendil-works/pi-coding-agent";
import { InMemoryCredentialStore } from "@earendil-works/pi-ai";

import { getThinkingLevels } from "../src/sidecar.js";

test("catalog thinking levels preserve model-specific gaps and max", () => {
  assert.deepEqual(
    getThinkingLevels({
      reasoning: true,
      thinkingLevelMap: {
        off: null,
        minimal: null,
        low: "low",
        medium: null,
        high: "high",
        xhigh: "xhigh",
        max: "max",
      },
    }),
    ["low", "high", "xhigh", "max"],
  );
});

test("catalog thinking levels report off for non-reasoning models", () => {
  assert.deepEqual(getThinkingLevels({ reasoning: false }), ["off"]);
});


test("pi catalog includes GPT-6 Astra with exact thinking levels", async () => {
  const runtime = await ModelRuntime.create({
    credentials: new InMemoryCredentialStore(),
    modelsPath: null,
    allowModelNetwork: false,
    refreshOnCreate: false,
  });
  const registry = new ModelRegistry(runtime);
  const model = registry
    .getAll()
    .find(candidate => candidate.provider === "openai" && candidate.id === "gpt-6-astra");

  assert.ok(model, "GPT-6 Astra must exist in the OpenAI catalog");
  assert.deepEqual(
    getThinkingLevels(model),
    ["low", "medium", "high", "xhigh", "max"],
  );
});
