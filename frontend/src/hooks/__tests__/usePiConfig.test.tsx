import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeTransport } from "../../runtime/runtimeTransport";

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

const transport = {
  runtime: { location: "hosted" },
  ...apiMock,
  put: vi.fn(),
} as unknown as RuntimeTransport;

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

  it("does not restore config when sync finishes after removal", async () => {
    let resolveSync: ((value: typeof READY_CONFIG) => void) | null = null;
    apiMock.post.mockReturnValueOnce(
      new Promise((resolve: (value: typeof READY_CONFIG) => void) => {
        resolveSync = resolve;
      }),
    );
    apiMock.delete.mockResolvedValueOnce(undefined);

    const { result } = renderHook(() => usePiConfig(transport));
    await waitFor(() => {
      expect(result.current.config?.id).toBe("cfg-1");
    });

    let syncPromise: Promise<boolean> | null = null;
    await act(async () => {
      syncPromise = result.current.syncConfig();
    });
    await waitFor(() => expect(result.current.syncing).toBe(true));

    await act(async () => {
      expect(await result.current.removeConfig()).toBe(true);
    });
    expect(result.current.config).toBeNull();
    expect(result.current.syncing).toBe(false);
    expect(result.current.busy).toBe(false);

    await act(async () => {
      if (!resolveSync || !syncPromise) {
        throw new Error("Sync promise was not initialized");
      }
      resolveSync(READY_CONFIG);
      await syncPromise;
    });

    expect(result.current.config).toBeNull();
  });

  it("does not let an older load overwrite a completed sync", async () => {
    let resolveInitial: ((value: typeof READY_CONFIG) => void) | null = null;
    apiMock.get.mockReturnValueOnce(
      new Promise((resolve: (value: typeof READY_CONFIG) => void) => {
        resolveInitial = resolve;
      }),
    );
    const syncedConfig = { ...READY_CONFIG, id: "cfg-sync" };
    apiMock.post.mockResolvedValueOnce(syncedConfig);

    const { result } = renderHook(() => usePiConfig(transport));
    await act(async () => {
      expect(await result.current.syncConfig()).toBe(true);
    });
    expect(result.current.config?.id).toBe("cfg-sync");
    expect(result.current.loading).toBe(false);

    await act(async () => {
      if (!resolveInitial) throw new Error("Initial load was not initialized");
      resolveInitial(READY_CONFIG);
    });

    expect(result.current.config?.id).toBe("cfg-sync");
    expect(result.current.loading).toBe(false);
  });

  it("does not let an older category update overwrite sync", async () => {
    let resolvePatch: ((value: typeof READY_CONFIG) => void) | null = null;
    apiMock.patch.mockReturnValueOnce(
      new Promise((resolve: (value: typeof READY_CONFIG) => void) => {
        resolvePatch = resolve;
      }),
    );
    const syncedConfig = { ...READY_CONFIG, id: "cfg-sync" };
    apiMock.post.mockResolvedValueOnce(syncedConfig);

    const { result } = renderHook(() => usePiConfig(transport));
    await waitFor(() => expect(result.current.config?.id).toBe("cfg-1"));
    let categoryPromise: Promise<boolean> | null = null;
    await act(async () => {
      categoryPromise = result.current.toggleCategory("models", false);
    });
    await act(async () => {
      expect(await result.current.syncConfig()).toBe(true);
    });
    expect(result.current.config?.id).toBe("cfg-sync");

    await act(async () => {
      if (!resolvePatch || !categoryPromise) {
        throw new Error("Category promise was not initialized");
      }
      resolvePatch(READY_CONFIG);
      await categoryPromise;
    });

    expect(result.current.config?.id).toBe("cfg-sync");
  });

  it("serializes category toggle requests", async () => {
    let resolvePatch: ((value: typeof READY_CONFIG) => void) | null = null;
    apiMock.patch.mockReturnValueOnce(
      new Promise((resolve: (value: typeof READY_CONFIG) => void) => {
        resolvePatch = resolve;
      }),
    );

    const { result } = renderHook(() => usePiConfig(transport));

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

    const { result } = renderHook(() => usePiConfig(transport));

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
