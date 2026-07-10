import { describe, expect, it } from "vitest";

import { createRumConfiguration } from "./rum";

describe("createRumConfiguration", () => {
  it("does not capture source, prompts, or tool output", () => {
    const configuration = createRumConfiguration("audit-test-version");

    expect(configuration.sessionReplaySampleRate).toBe(0);
    expect(configuration.defaultPrivacyLevel).toBe("mask");
    expect(configuration.trackUserInteractions).toBe(false);
    expect(configuration.sessionSampleRate).toBeLessThanOrEqual(10);
  });
});
