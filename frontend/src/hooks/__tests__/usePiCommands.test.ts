import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SlashCommand } from "../../components/SlashCommandMenu";
import { usePiCommands } from "../usePiCommands";

const mocks = vi.hoisted(() => ({
  getCachedPiCommands: vi.fn(),
  subscribePiCommands: vi.fn(),
}));

vi.mock("../../api/piCommandsCache", () => ({
  getCachedPiCommands: mocks.getCachedPiCommands,
  subscribePiCommands: mocks.subscribePiCommands,
}));

describe("usePiCommands", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.getCachedPiCommands.mockReset();
    mocks.subscribePiCommands.mockReset();
    mocks.subscribePiCommands.mockReturnValue(() => undefined);
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("retries transient command loading failures while mounted", async () => {
    const importedCommands: SlashCommand[] = [
      {
        name: "skill:debug",
        description: "Debug code",
        source: "pi",
      },
    ];
    mocks.getCachedPiCommands
      .mockRejectedValueOnce(new Error("sidecar warming up"))
      .mockResolvedValueOnce(importedCommands);

    const { result, unmount } = renderHook(() => usePiCommands());

    await act(async () => {
      await Promise.resolve();
    });
    expect(mocks.getCachedPiCommands).toHaveBeenCalledTimes(1);
    expect(result.current).toEqual([]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current).toEqual(importedCommands);
    expect(mocks.getCachedPiCommands).toHaveBeenCalledTimes(2);

    unmount();
  });
});
