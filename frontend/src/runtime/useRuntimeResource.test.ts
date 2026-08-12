import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resolveRuntimeRef } from "./resolveRuntime";
import { useRuntimeResource } from "./useRuntimeResource";

vi.mock("./resolveRuntime", () => ({
  resolveRuntimeRef: vi.fn(),
}));

const resourceId = "a".repeat(32);

describe("runtime resource resolution", () => {
  beforeEach(() => {
    vi.mocked(resolveRuntimeRef).mockReset();
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
