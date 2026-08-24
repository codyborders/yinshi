import { describe, expect, it, vi } from "vitest";

import { provisionRuntimeRef, resolveRuntimeRef } from "./resolveRuntime";

const runnerKey = "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I";

describe("runtime resolution", () => {
  it("resolves a ready browser hosted runtime to its managed runner key", async () => {
    const getRuntime = vi.fn().mockResolvedValue({
      provider: "fly_sprites",
      status: "ready",
      artifact_version: "release-1",
      last_error: null,
      runner_public_key: runnerKey,
      history_bundle_supported: true,
    });
    const provisionRuntime = vi.fn();

    await expect(
      resolveRuntimeRef(
        { location: "hosted" },
        { desktop: false, getRuntime, provisionRuntime },
      ),
    ).resolves.toEqual({
      location: "managed",
      runnerPublicKey: runnerKey,
      historyBundleSupported: true,
    });
    expect(getRuntime).toHaveBeenCalledTimes(1);
    expect(provisionRuntime).not.toHaveBeenCalled();
  });

  it("retains a false managed history feature advertisement", async () => {
    const getRuntime = vi.fn().mockResolvedValue({
      provider: "fly_sprites",
      status: "ready",
      runner_public_key: runnerKey,
      history_bundle_supported: false,
    });

    await expect(
      resolveRuntimeRef(
        { location: "hosted" },
        { desktop: false, getRuntime, provisionRuntime: vi.fn() },
      ),
    ).resolves.toEqual({
      location: "managed",
      runnerPublicKey: runnerKey,
      historyBundleSupported: false,
    });
  });

  it.each([
    ["absent", "Managed runtime is not provisioned"],
    ["failed", "Managed runtime setup failed"],
  ])(
    "does not provision a %s Fly runtime while resolving",
    async (status, message) => {
      const getRuntime = vi.fn().mockResolvedValue({
        provider: "fly_sprites",
        status,
        artifact_version: null,
        last_error: null,
        runner_public_key: null,
      });
      const provisionRuntime = vi.fn();

      await expect(
        resolveRuntimeRef(
          { location: "hosted" },
          { desktop: false, getRuntime, provisionRuntime },
        ),
      ).rejects.toThrow(message);
      expect(getRuntime).toHaveBeenCalledTimes(1);
      expect(provisionRuntime).not.toHaveBeenCalled();
    },
  );

  it("inspects absent managed state without provider mutation", async () => {
    const provisionRuntime = vi.fn();

    await expect(
      resolveRuntimeRef(
        { location: "hosted" },
        {
          desktop: false,
          getRuntime: vi.fn().mockResolvedValue({
            provider: "fly_sprites",
            status: "absent",
            runner_public_key: null,
          }),
          provisionRuntime,
        },
      ),
    ).rejects.toThrow("Managed runtime is not provisioned");
    expect(provisionRuntime).not.toHaveBeenCalled();
  });

  it("provisions managed state only through the explicit command", async () => {
    const getRuntime = vi.fn().mockResolvedValue({
      provider: "fly_sprites",
      status: "ready",
      runner_public_key: runnerKey,
    });
    const provisionRuntime = vi.fn().mockResolvedValue({
      provider: "fly_sprites",
      status: "provisioning",
      runner_public_key: null,
    });

    await expect(
      provisionRuntimeRef(
        { location: "hosted" },
        {
          desktop: false,
          getRuntime,
          provisionRuntime,
          sleep: vi.fn().mockResolvedValue(undefined),
          maxPollAttempts: 1,
        },
      ),
    ).resolves.toEqual({
      location: "managed",
      runnerPublicKey: runnerKey,
    });
    expect(provisionRuntime).toHaveBeenCalledTimes(1);
    expect(getRuntime).toHaveBeenCalledTimes(1);
  });

  it("reports a safe allowlisted failure after explicit provisioning starts", async () => {
    const getRuntime = vi.fn().mockResolvedValue({
      provider: "fly_sprites",
      status: "failed",
      last_error: "bootstrap_failed",
      runner_public_key: null,
    });
    const provisionRuntime = vi.fn().mockResolvedValue({
      provider: "fly_sprites",
      status: "provisioning",
      runner_public_key: null,
    });
    const sleep = vi.fn().mockResolvedValue(undefined);

    await expect(
      provisionRuntimeRef(
        { location: "hosted" },
        {
          desktop: false,
          getRuntime,
          provisionRuntime,
          sleep,
          maxPollAttempts: 2,
        },
      ),
    ).rejects.toThrow("Managed runtime setup failed");
    expect(provisionRuntime).toHaveBeenCalledTimes(1);
    expect(getRuntime).toHaveBeenCalledTimes(1);
    expect(sleep).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["artifact_invalid", "Managed runtime artifact is invalid"],
    ["provider_unavailable", "Managed runtime provider is unavailable"],
    ["network_policy_failed", "Managed runtime network policy setup failed"],
    ["runner_registration_failed", "Managed runtime registration failed"],
    ["runner_identity_changed", "Managed runtime identity changed"],
    ["wake_timeout", "Managed runtime startup timed out"],
    ["checkpoint_failed", "Managed runtime checkpoint failed"],
    ["delete_failed", "Managed runtime deletion failed"],
  ])(
    "maps allowlisted failure %s to a fixed safe message",
    async (code, message) => {
      await expect(
        provisionRuntimeRef(
          { location: "hosted" },
          {
            desktop: false,
            provisionRuntime: vi.fn().mockResolvedValue({
              provider: "fly_sprites",
              status: "failed",
              last_error: code,
              runner_public_key: null,
            }),
          },
        ),
      ).rejects.toThrow(message);
    },
  );

  it("polls an already provisioning runtime without provisioning it again", async () => {
    const getRuntime = vi
      .fn()
      .mockResolvedValueOnce({
        provider: "fly_sprites",
        status: "provisioning",
        runner_public_key: null,
      })
      .mockResolvedValueOnce({
        provider: "fly_sprites",
        status: "ready",
        runner_public_key: runnerKey,
      });
    const provisionRuntime = vi.fn();
    const sleep = vi.fn().mockResolvedValue(undefined);

    await expect(
      resolveRuntimeRef(
        { location: "hosted" },
        {
          desktop: false,
          getRuntime,
          provisionRuntime,
          sleep,
          maxPollAttempts: 2,
        },
      ),
    ).resolves.toEqual({
      location: "managed",
      runnerPublicKey: runnerKey,
    });
    expect(getRuntime).toHaveBeenCalledTimes(2);
    expect(provisionRuntime).not.toHaveBeenCalled();
    expect(sleep).toHaveBeenCalledTimes(1);
  });

  it("stops polling before another request when aborted during a wait", async () => {
    const controller = new AbortController();
    const getRuntime = vi
      .fn()
      .mockResolvedValueOnce({
        provider: "fly_sprites",
        status: "provisioning",
        runner_public_key: null,
      })
      .mockResolvedValueOnce({
        provider: "fly_sprites",
        status: "ready",
        runner_public_key: runnerKey,
      });
    const sleep = vi.fn().mockImplementation(async () => {
      controller.abort();
    });

    await expect(
      resolveRuntimeRef(
        { location: "hosted" },
        {
          desktop: false,
          getRuntime,
          provisionRuntime: vi.fn(),
          sleep,
          maxPollAttempts: 2,
        },
        controller.signal,
      ),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(getRuntime).toHaveBeenCalledTimes(1);
  });

  it("rejects a deleting runtime with a fixed local error", async () => {
    await expect(
      resolveRuntimeRef(
        { location: "hosted" },
        {
          desktop: false,
          getRuntime: vi.fn().mockResolvedValue({
            provider: "fly_sprites",
            status: "deleting",
            runner_public_key: null,
          }),
          provisionRuntime: vi.fn(),
        },
      ),
    ).rejects.toThrow("Managed runtime is deleting");
  });

  it.each([null, "not-a-canonical-key"])(
    "rejects an invalid managed runner key with a fixed local error",
    async (runnerPublicKey) => {
      await expect(
        resolveRuntimeRef(
          { location: "hosted" },
          {
            desktop: false,
            getRuntime: vi.fn().mockResolvedValue({
              provider: "fly_sprites",
              status: "ready",
              runner_public_key: runnerPublicKey,
            }),
            provisionRuntime: vi.fn(),
          },
        ),
      ).rejects.toThrow("Managed runtime runner public key is invalid");
    },
  );

  it("rejects an unknown provider with a fixed local error", async () => {
    await expect(
      resolveRuntimeRef(
        { location: "hosted" },
        {
          desktop: false,
          getRuntime: vi.fn().mockResolvedValue({
            provider: "other",
            status: "ready",
            runner_public_key: runnerKey,
          }),
          provisionRuntime: vi.fn(),
        },
      ),
    ).rejects.toThrow("Managed runtime provider is unsupported");
  });

  it.each([
    null,
    { provider: "fly_sprites", status: "unknown" },
    {
      provider: "fly_sprites",
      status: "ready",
      runner_public_key: runnerKey,
      history_bundle_supported: "true",
    },
  ])(
    "rejects a malformed runtime response with a fixed local error",
    async (response) => {
      await expect(
        resolveRuntimeRef(
          { location: "hosted" },
          {
            desktop: false,
            getRuntime: vi.fn().mockResolvedValue(response),
            provisionRuntime: vi.fn(),
          },
        ),
      ).rejects.toThrow("Managed runtime response is invalid");
    },
  );

  it("keeps local providers and desktop hosted bridges on hosted transport", async () => {
    const getRuntime = vi.fn().mockResolvedValue({
      provider: "local",
      status: "ready",
      artifact_version: null,
      last_error: null,
      runner_public_key: null,
    });
    const provisionRuntime = vi.fn();

    await expect(
      resolveRuntimeRef(
        { location: "hosted" },
        { desktop: false, getRuntime, provisionRuntime },
      ),
    ).resolves.toEqual({ location: "hosted" });
    Object.defineProperty(window, "yinshiDesktop", {
      configurable: true,
      value: {},
    });
    try {
      await expect(
        resolveRuntimeRef(
          { location: "hosted" },
          { getRuntime, provisionRuntime },
        ),
      ).resolves.toEqual({ location: "hosted" });
    } finally {
      Reflect.deleteProperty(window, "yinshiDesktop");
    }
    expect(getRuntime).toHaveBeenCalledTimes(1);
    expect(provisionRuntime).not.toHaveBeenCalled();
  });

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
