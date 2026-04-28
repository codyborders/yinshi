import { describe, expect, it } from "vitest";

import {
  describeSessionModel,
  formatSessionModelOptionLabel,
  getSessionModelLabel,
  resolveSessionModelKey,
} from "../sessionModels";

const catalogModels = [
  {
    ref: "minimax/MiniMax-M2.7",
    provider: "minimax",
    id: "MiniMax-M2.7",
    label: "MiniMax M2.7",
    api: "openai-completions",
    reasoning: false,
    inputs: ["text"],
    context_window: 200000,
    max_tokens: 16384,
  },
  {
    ref: "minimax/MiniMax-M2.7-highspeed",
    provider: "minimax",
    id: "MiniMax-M2.7-highspeed",
    label: "MiniMax M2.7 Highspeed",
    api: "openai-completions",
    reasoning: false,
    inputs: ["text"],
    context_window: 200000,
    max_tokens: 16384,
  },
  {
    ref: "openai/gpt-5.4-pro",
    provider: "openai",
    id: "gpt-5.4-pro",
    label: "GPT-5.4 Pro",
    api: "openai-responses",
    reasoning: true,
    inputs: ["text"],
    context_window: 400000,
    max_tokens: 16384,
  },
  {
    ref: "openrouter/openai/gpt-5.4-pro",
    provider: "openrouter",
    id: "openai/gpt-5.4-pro",
    label: "OpenAI: GPT-5.4 Pro",
    api: "openai-completions",
    reasoning: true,
    inputs: ["text"],
    context_window: 400000,
    max_tokens: 16384,
  },
  {
    ref: "opencode/gpt-5.4-pro",
    provider: "opencode",
    id: "gpt-5.4-pro",
    label: "GPT-5.4 Pro",
    api: "openai-responses",
    reasoning: true,
    inputs: ["text"],
    context_window: 400000,
    max_tokens: 16384,
  },
];

describe("sessionModels", () => {
  it("normalizes the legacy minimax alias to the canonical ref", () => {
    expect(resolveSessionModelKey("minimax", [...catalogModels])).toBe(
      "minimax/MiniMax-M2.7",
    );
  });

  it("normalizes the highspeed identifier regardless of case", () => {
    expect(
      resolveSessionModelKey("MiniMax-M2.7-highspeed", [...catalogModels]),
    ).toBe("minimax/MiniMax-M2.7-highspeed");
  });

  it("describes known models with both label and canonical ref", () => {
    expect(getSessionModelLabel("minimax/MiniMax-M2.7", [...catalogModels])).toBe(
      "MiniMax M2.7",
    );
    expect(
      describeSessionModel("minimax/MiniMax-M2.7-highspeed", [...catalogModels]),
    ).toContain("minimax/MiniMax-M2.7-highspeed");
  });

  it("prefers the connected provider when an id is ambiguous", () => {
    expect(
      resolveSessionModelKey("gpt-5.4-pro", [...catalogModels], ["openai"]),
    ).toBe("openai/gpt-5.4-pro");
    expect(
      resolveSessionModelKey("gpt-5.4-pro", [...catalogModels]),
    ).toBeNull();
  });

  it("formats option labels with the provider identity and connection state", () => {
    expect(
      formatSessionModelOptionLabel(catalogModels[2]!, "OpenAI", true),
    ).toBe("OpenAI - GPT-5.4 Pro");
    expect(
      formatSessionModelOptionLabel(catalogModels[3]!, "OpenRouter", false),
    ).toBe("OpenRouter - OpenAI: GPT-5.4 Pro (not connected)");
  });
});
