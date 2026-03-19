import { describe, expect, it } from "vitest";

import {
  describeSessionModel,
  getSessionModelLabel,
  resolveSessionModelKey,
} from "../sessionModels";

describe("sessionModels", () => {
  it("normalizes the legacy minimax alias to the current default", () => {
    expect(resolveSessionModelKey("minimax")).toBe("minimax-m2.7");
  });

  it("normalizes the highspeed identifier regardless of case", () => {
    expect(resolveSessionModelKey("MiniMax-M2.7-highspeed")).toBe(
      "minimax-m2.7-highspeed",
    );
  });

  it("describes known models with both label and key", () => {
    expect(getSessionModelLabel("minimax-m2.7")).toBe("MiniMax M2.7");
    expect(describeSessionModel("minimax-m2.7-highspeed")).toContain(
      "minimax-m2.7-highspeed",
    );
  });
});
