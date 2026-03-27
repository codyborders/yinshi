import { describe, expect, it } from "vitest";

import {
  describeSessionModel,
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
});
