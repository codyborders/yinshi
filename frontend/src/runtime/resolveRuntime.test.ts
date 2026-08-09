import { describe, expect, it, vi } from "vitest";

import { resolveRuntimeRef } from "./resolveRuntime";

const runnerKey = "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I";

describe("runtime resolution", () => {
  it("resolves an explicitly paired BYOC runner without storing its key in routes", async () => {
    const getRunner = vi.fn().mockResolvedValue({
      id: "runner-1",
      status: "online",
      noise_public_key: runnerKey,
      noise_key_confirmed: true,
    });

    await expect(
      resolveRuntimeRef(
        { location: "byoc", runnerId: "runner-1", runnerPublicKey: null },
        { getRunner },
      ),
    ).resolves.toEqual({
      location: "byoc",
      runnerId: "runner-1",
      runnerPublicKey: runnerKey,
    });
    expect(getRunner).toHaveBeenCalledTimes(1);
  });

  it("fails closed when the runner key is absent, changed, or belongs to another runner", async () => {
    const getRunner = vi.fn().mockResolvedValue({
      id: "runner-2",
      status: "online",
      noise_public_key: runnerKey,
      noise_key_confirmed: true,
    });

    await expect(
      resolveRuntimeRef(
        { location: "byoc", runnerId: "runner-1", runnerPublicKey: null },
        { getRunner },
      ),
    ).rejects.toThrow("unavailable or is not paired");
  });
});
