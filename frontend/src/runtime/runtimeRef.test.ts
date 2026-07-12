import { describe, expect, it } from "vitest";

import {
  defaultRuntimeRef,
  parseRuntimeResourceId,
  runtimeResourceId,
} from "./runtimeRef";

const resourceId = "a".repeat(32);

describe("location-qualified runtime resource IDs", () => {
  it("keeps hosted browser IDs backward compatible", () => {
    expect(runtimeResourceId({ location: "hosted" }, resourceId)).toBe(resourceId);
    expect(
      runtimeResourceId({ location: "hosted" }, resourceId, { desktop: true }),
    ).toBe(`hosted.${resourceId}`);
    expect(parseRuntimeResourceId(resourceId, { desktop: false })).toEqual({
      runtime: { location: "hosted" },
      resourceId,
    });
  });

  it("qualifies local desktop IDs", () => {
    const encoded = runtimeResourceId({ location: "local" }, resourceId);

    expect(encoded).toBe(`local.${resourceId}`);
    expect(parseRuntimeResourceId(encoded, { desktop: true })).toEqual({
      runtime: { location: "local" },
      resourceId,
    });
  });

  it("qualifies BYOC IDs without putting keys or bearer authority in URLs", () => {
    const runtime = {
      location: "byoc" as const,
      runnerId: "runner/account-1",
      runnerPublicKey: "public-key-must-not-enter-route",
    };
    const encoded = runtimeResourceId(runtime, resourceId);

    expect(encoded).not.toContain(runtime.runnerPublicKey);
    expect(parseRuntimeResourceId(encoded, { desktop: false })).toEqual({
      runtime: {
        location: "byoc",
        runnerId: runtime.runnerId,
        runnerPublicKey: null,
      },
      resourceId,
    });
  });

  it("selects local legacy IDs in desktop and rejects malformed qualifiers", () => {
    expect(defaultRuntimeRef({ desktop: true })).toEqual({ location: "local" });
    expect(parseRuntimeResourceId(resourceId, { desktop: true }).runtime).toEqual({
      location: "local",
    });
    expect(() => parseRuntimeResourceId("byoc.bad", { desktop: false })).toThrow(
      "invalid",
    );
  });
});
