import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  class MockApiError extends Error {
    constructor(
      public status: number,
      message: string,
    ) {
      super(message);
      this.name = "ApiError";
    }
  }

  return {
    apiGet: vi.fn(),
    ApiError: MockApiError,
  };
});

vi.mock("../client", () => ({
  ApiError: mocks.ApiError,
  api: {
    get: mocks.apiGet,
  },
}));

type PiCommandFixture = {
  kind: string;
  name: string;
  description: string;
  command_name: string;
};

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

async function loadCacheModule(): Promise<typeof import("../piCommandsCache")> {
  vi.resetModules();
  return import("../piCommandsCache");
}

describe("piCommandsCache", () => {
  beforeEach(() => {
    mocks.apiGet.mockReset();
  });

  it("caches successful command responses", async () => {
    const cache = await loadCacheModule();
    mocks.apiGet.mockResolvedValueOnce({
      commands: [
        {
          kind: "skill",
          name: "debug",
          description: "Debug code",
          command_name: "skill:debug",
        },
      ],
    });

    const firstCommands = await cache.getCachedPiCommands();
    const secondCommands = await cache.getCachedPiCommands();

    expect(mocks.apiGet).toHaveBeenCalledTimes(1);
    expect(firstCommands).toEqual([
      {
        name: "skill:debug",
        description: "Debug code",
        source: "pi",
      },
    ]);
    expect(secondCommands).toBe(firstCommands);
  });

  it("caches a missing Pi config as an empty command list", async () => {
    const cache = await loadCacheModule();
    mocks.apiGet.mockRejectedValueOnce(new mocks.ApiError(404, "missing"));

    await expect(cache.getCachedPiCommands()).resolves.toEqual([]);
    await expect(cache.getCachedPiCommands()).resolves.toEqual([]);

    expect(mocks.apiGet).toHaveBeenCalledTimes(1);
  });

  it("does not let stale in-flight failures clear newer cached commands", async () => {
    const cache = await loadCacheModule();
    const firstResponse = deferred<{ commands: PiCommandFixture[] }>();
    const secondResponse = deferred<{ commands: PiCommandFixture[] }>();
    mocks.apiGet
      .mockReturnValueOnce(firstResponse.promise)
      .mockReturnValueOnce(secondResponse.promise);

    const firstRequest = cache.getCachedPiCommands().catch((error: unknown) => error);
    cache.invalidatePiCommands();
    const secondRequest = cache.getCachedPiCommands();

    secondResponse.resolve({
      commands: [
        {
          kind: "skill",
          name: "review",
          description: "Review code",
          command_name: "skill:review",
        },
      ],
    });
    await expect(secondRequest).resolves.toEqual([
      {
        name: "skill:review",
        description: "Review code",
        source: "pi",
      },
    ]);

    firstResponse.reject(new Error("old request failed"));
    await expect(firstRequest).resolves.toBeInstanceOf(Error);
    await cache.getCachedPiCommands();

    expect(mocks.apiGet).toHaveBeenCalledTimes(2);
  });

  it("does not cache transient sidecar failures as an empty command list", async () => {
    const cache = await loadCacheModule();
    mocks.apiGet
      .mockRejectedValueOnce(new mocks.ApiError(503, "warming up"))
      .mockResolvedValueOnce({
        commands: [
          {
            kind: "skill",
            name: "plan",
            description: "Plan work",
            command_name: "skill:plan",
          },
        ],
      });

    await expect(cache.getCachedPiCommands()).rejects.toThrow("warming up");
    await expect(cache.getCachedPiCommands()).resolves.toEqual([
      {
        name: "skill:plan",
        description: "Plan work",
        source: "pi",
      },
    ]);

    expect(mocks.apiGet).toHaveBeenCalledTimes(2);
  });
});
