// Covers durable, user-scoped model choices through the browser storage boundary.
import { beforeEach, describe, expect, it } from "vitest";
import {
  preferredSessionModel,
  rememberSessionModel,
} from "../sessionModelPreference";
import { DEFAULT_SESSION_MODEL } from "../sessionModels";

describe("session model preference", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("falls back to the existing default when no preference exists", () => {
    expect(preferredSessionModel("user-1")).toBe(DEFAULT_SESSION_MODEL);
  });

  it("keeps each authenticated user's preference separate", () => {
    rememberSessionModel("user-1", "openai-codex/gpt-5.6-sol");
    rememberSessionModel("user-2", "anthropic/claude-sonnet-4-20250514");

    expect(preferredSessionModel("user-1")).toBe("openai-codex/gpt-5.6-sol");
    expect(preferredSessionModel("user-2")).toBe(
      "anthropic/claude-sonnet-4-20250514",
    );
  });

  it("ignores an invalid stored model reference", () => {
    localStorage.setItem("yinshi:last-session-model:user-1", "not-a-model");

    expect(preferredSessionModel("user-1")).toBe(DEFAULT_SESSION_MODEL);
  });
});
