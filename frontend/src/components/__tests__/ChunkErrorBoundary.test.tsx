import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  default as ChunkErrorBoundary,
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

describe("ChunkErrorBoundary", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows reload UI when sessionStorage is unavailable during chunk recovery", async () => {
    const ThrowChunkError = () => {
      throw new TypeError("Failed to fetch dynamically imported module");
    };

    vi.spyOn(console, "error").mockImplementation(() => {});
    const originalSessionStorage = window.sessionStorage;
    Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      value: {
        getItem(): string | null {
          return null;
        },
        setItem(): void {
          throw new DOMException("storage unavailable", "QuotaExceededError");
        },
      },
    });
    try {
      render(
        <ChunkErrorBoundary>
          <ThrowChunkError />
        </ChunkErrorBoundary>,
      );

      await waitFor(() => {
        expect(
          screen.getByText("This page needs a refresh after the latest deploy."),
        ).toBeTruthy();
      });
      expect(screen.getByRole("button", { name: "Reload page" })).toBeTruthy();
    } finally {
      Object.defineProperty(window, "sessionStorage", {
        configurable: true,
        value: originalSessionStorage,
      });
    }
  });
});
