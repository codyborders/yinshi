import { describe, expect, it } from "vitest";

import {
  isChunkLoadError,
  shouldReloadForChunkError,
} from "../ChunkErrorBoundary";

function createStorageStub() {
  const values = new Map<string, string>();
  return {
    getItem(key: string): string | null {
      if (!values.has(key)) {
        return null;
      }
      return values.get(key) ?? null;
    },
    setItem(key: string, value: string): void {
      values.set(key, value);
    },
  };
}

describe("ChunkErrorBoundary helpers", () => {
  it("recognizes common chunk load failures", () => {
    expect(
      isChunkLoadError(
        new TypeError("Failed to fetch dynamically imported module"),
      ),
    ).toBe(true);
    expect(
      isChunkLoadError(
        new Error("Loading chunk 123 failed."),
      ),
    ).toBe(true);
    expect(isChunkLoadError(new Error("Cannot read properties of undefined"))).toBe(false);
  });

  it("reloads only once per route and entry script signature", () => {
    const storage = createStorageStub();

    expect(
      shouldReloadForChunkError(storage, "/app/session/abc123", "/assets/index-old.js"),
    ).toBe(true);
    expect(
      shouldReloadForChunkError(storage, "/app/session/abc123", "/assets/index-old.js"),
    ).toBe(false);
    expect(
      shouldReloadForChunkError(storage, "/app/session/abc123", "/assets/index-new.js"),
    ).toBe(true);
  });
});
