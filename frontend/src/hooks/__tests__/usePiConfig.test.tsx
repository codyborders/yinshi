import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
    upload: vi.fn(),
  },
}));

vi.mock("../../api/client", () => {
  class ApiError extends Error {
    status: number;

    constructor(status: number, message: string) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }

  return {
    ApiError,
    api: apiMock,
  };
});

import { usePiConfig } from "../usePiConfig";

const READY_CONFIG = {
  id: "cfg-1",
  created_at: "2026-03-20T12:00:00Z",
  updated_at: "2026-03-20T12:00:00Z",
  source_type: "github" as const,
  source_label: "example/repo",
  last_synced_at: "2026-03-20T12:00:00Z",
  status: "ready" as const,
  error_message: null,
  available_categories: ["settings", "models"],
  enabled_categories: ["settings", "models"],
};

describe("usePiConfig", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.get.mockResolvedValue(READY_CONFIG);
  });

  it("serializes category toggle requests", async () => {
    let resolvePatch: ((value: typeof READY_CONFIG) => void) | null = null;
    apiMock.patch.mockReturnValueOnce(
      new Promise((resolve: (value: typeof READY_CONFIG) => void) => {
        resolvePatch = resolve;
      }),
    );

    const { result } = renderHook(() => usePiConfig());

    await waitFor(() => {
      expect(result.current.config?.enabled_categories).toEqual(["settings", "models"]);
    });

    let firstTogglePromise: Promise<boolean> | null = null;
    await act(async () => {
      firstTogglePromise = result.current.toggleCategory("settings", false);
    });

    await waitFor(() => {
      expect(result.current.updatingCategories).toBe(true);
      expect(result.current.config?.enabled_categories).toEqual(["models"]);
    });

    let secondToggleResult = true;
    await act(async () => {
      secondToggleResult = await result.current.toggleCategory("models", false);
    });

    expect(secondToggleResult).toBe(false);
    expect(apiMock.patch).toHaveBeenCalledTimes(1);

    await act(async () => {
      if (!resolvePatch || !firstTogglePromise) {
        throw new Error("Toggle promise was not initialized");
      }
      resolvePatch({
        ...READY_CONFIG,
        enabled_categories: ["models"],
      });
      await firstTogglePromise;
    });

    expect(result.current.updatingCategories).toBe(false);
    expect(result.current.config?.enabled_categories).toEqual(["models"]);
  });

  it("rolls back the optimistic toggle on failure", async () => {
    apiMock.patch.mockRejectedValueOnce(new Error("Patch failed"));

    const { result } = renderHook(() => usePiConfig());

    await waitFor(() => {
      expect(result.current.config?.enabled_categories).toEqual(["settings", "models"]);
    });

    let toggleResult = true;
    await act(async () => {
      toggleResult = await result.current.toggleCategory("settings", false);
    });

    expect(toggleResult).toBe(false);
    expect(result.current.updatingCategories).toBe(false);
    expect(result.current.config?.enabled_categories).toEqual(["settings", "models"]);
    expect(result.current.error).toBe("Patch failed");
  });
});
