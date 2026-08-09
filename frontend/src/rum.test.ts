import type {
  RumErrorEvent,
  RumEventDomainContext,
  RumViewEvent,
} from "@datadog/browser-rum";
import { describe, expect, it } from "vitest";

import { createRumConfiguration } from "./rum";

describe("createRumConfiguration", () => {
  it("does not capture source, prompts, or tool output", () => {
    const configuration = createRumConfiguration("audit-test-version");

    expect(configuration.sessionReplaySampleRate).toBe(0);
    expect(configuration.defaultPrivacyLevel).toBe("mask");
    expect(configuration.trackUserInteractions).toBe(false);
    expect(configuration.trackResources).toBe(false);
    expect(configuration.trackLongTasks).toBe(false);
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

  it.each(["action", "long_task", "transition", "vital"] as const)(
    "drops %s events because only view-performance metadata is allowlisted",
    (type) => {
      const configuration = createRumConfiguration("audit-test-version");
      const event = { type } as Parameters<
        NonNullable<typeof configuration.beforeSend>
      >[0];

      expect(configuration.beforeSend?.(event, {} as RumEventDomainContext)).toBe(false);
    },
  );

  it("replaces user-derived view fields with an allowlisted route", () => {
    const configuration = createRumConfiguration("audit-test-version");
    const viewEvent = {
      type: "view",
      view: {
        url: "https://yinshi.example/app/session/CANARY_SESSION?prompt=CANARY_PROMPT",
        name: "CANARY_REPOSITORY",
        referrer: "https://search.example/?q=CANARY_QUERY",
      },
    } as RumViewEvent;

    expect(configuration.beforeSend?.(viewEvent, {} as RumEventDomainContext)).toBe(true);
    expect(viewEvent.view.url).toBe("/app/session/:sessionId");
    expect(viewEvent.view.name).toBe("/app/session/:sessionId");
    expect(viewEvent.view.referrer).toBe("");
  });

  it.each([
    { field: "user", extra: { usr: { email: "CANARY_EMAIL" } } },
    { field: "account", extra: { account: { id: "id", name: "CANARY_ACCOUNT" } } },
    { field: "custom context", extra: { context: { prompt: "CANARY_PROMPT" } } },
  ])("drops view events carrying $field data", ({ extra }) => {
    const configuration = createRumConfiguration("audit-test-version");
    const viewEvent = {
      type: "view",
      view: { url: "https://yinshi.example/app" },
      ...extra,
    } as RumViewEvent;

    expect(configuration.beforeSend?.(viewEvent, {} as RumEventDomainContext)).toBe(false);
  });
});
