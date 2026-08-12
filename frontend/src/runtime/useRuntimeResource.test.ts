import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resolveRuntimeRef } from "./resolveRuntime";
import { createRuntimeTransport } from "./runtimeTransport";
import { useRuntimeResource } from "./useRuntimeResource";

vi.mock("./resolveRuntime", () => ({
  resolveRuntimeRef: vi.fn(),
}));
vi.mock("./runtimeTransport", () => ({
  createRuntimeTransport: vi.fn(),
}));

const resourceId = "a".repeat(32);

describe("runtime resource resolution", () => {
  beforeEach(() => {
    vi.mocked(resolveRuntimeRef).mockReset();
    vi.mocked(createRuntimeTransport).mockReset();
  });

  it("closes its runtime transport when the hook unmounts", async () => {
    const close = vi.fn();
    vi.mocked(resolveRuntimeRef).mockResolvedValue({ location: "hosted" });
    vi.mocked(createRuntimeTransport).mockReturnValue({
      runtime: { location: "hosted" },
      get: vi.fn(),
      post: vi.fn(),
      patch: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
      upload: vi.fn(),
      close,
    });

    const { unmount } = renderHook(() => useRuntimeResource(resourceId));
    await waitFor(() => expect(createRuntimeTransport).toHaveBeenCalledTimes(1));

    unmount();

    expect(close).toHaveBeenCalledTimes(1);
  });

  it("aborts pending runtime resolution when the hook unmounts", async () => {
    vi.mocked(resolveRuntimeRef).mockReturnValue(new Promise(() => undefined));

    const { unmount } = renderHook(() => useRuntimeResource(resourceId));

    await waitFor(() => expect(resolveRuntimeRef).toHaveBeenCalledTimes(1));
    const signal = vi.mocked(resolveRuntimeRef).mock.calls[0]?.[2];
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(signal?.aborted).toBe(false);

    unmount();

    expect(signal?.aborted).toBe(true);
  });
});
