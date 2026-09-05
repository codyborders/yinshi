import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { RuntimeTransport } from "../../runtime/runtimeTransport";

const sessionId = "a".repeat(32);

describe("useThreadTree", () => {
  it("loads all thread resources concurrently and treats missing result as empty", async () => {
    const { useThreadTree } = await import("../useThreadTree");
    const runtime = {
      runtime: { location: "hosted" },
      get: vi.fn((path: string) =>
        path.endsWith("/tree")
          ? Promise.resolve({ root: {}, nodes: [], placeholders: [], thread_count: 0, active_descendant_count: 0, tree_depth: 0 })
          : path.endsWith("/children")
            ? Promise.resolve([])
            : path.endsWith("/limits")
              ? Promise.resolve({})
              : Promise.reject({ status: 404 }),
      ),
      post: vi.fn(),
      patch: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
      upload: vi.fn(),
      close: vi.fn(),
    } as unknown as RuntimeTransport;
    const { result } = renderHook(() => useThreadTree(sessionId, runtime));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(runtime.get).toHaveBeenCalledTimes(4);
    expect(result.current.result).toBeNull();
    expect(result.current.error).toBeNull();
  });
});
