import type { RumErrorEvent, RumEventDomainContext } from "@datadog/browser-rum";
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

  it("drops automatic error events because their messages can contain user input", () => {
    const configuration = createRumConfiguration("audit-test-version");
    const errorEvent = {
      type: "error",
      error: { message: "CANARY_PRIVATE_PROMPT", source: "source" },
    } as RumErrorEvent;

    expect(configuration.beforeSend).toBeTypeOf("function");
    expect(configuration.beforeSend?.(errorEvent, {} as RumEventDomainContext)).toBe(false);
  });

  it("drops resource events because their URLs can contain user input", () => {
    const configuration = createRumConfiguration("audit-test-version");
    const resourceEvent = {
      type: "resource",
      resource: { url: "https://example.test/CANARY_PRIVATE_PATH?token=secret" },
    } as Parameters<NonNullable<typeof configuration.beforeSend>>[0];

    expect(configuration.beforeSend?.(resourceEvent, {} as RumEventDomainContext)).toBe(false);
  });
});
