import { beforeEach, describe, expect, it, vi } from "vitest";

const preloadEncryptedRunnerCryptoMock = vi.fn();

vi.mock("./runner/encryptedRunnerClient", () => ({
  preloadEncryptedRunnerCrypto: () => preloadEncryptedRunnerCryptoMock(),
}));

vi.mock("./main", () => ({}));

describe("early managed runner crypto preload", () => {
  beforeEach(() => {
    vi.resetModules();
    preloadEncryptedRunnerCryptoMock.mockReset();
  });

  it.each(["/app", "/app/session/test"])(
    "preloads runner crypto on %s",
    async (pathname) => {
      window.history.replaceState(null, "", pathname);
      preloadEncryptedRunnerCryptoMock.mockResolvedValue(undefined);

      await import("./preload");

      await vi.waitFor(() => {
        expect(preloadEncryptedRunnerCryptoMock).toHaveBeenCalledOnce();
      });
    },
  );

  it("does not preload runner crypto outside app paths", async () => {
    window.history.replaceState(null, "", "/login");

    await import("./preload");

    expect(preloadEncryptedRunnerCryptoMock).not.toHaveBeenCalled();
  });

  it("handles a rejected crypto preload", async () => {
    window.history.replaceState(null, "", "/app/session/test");
    preloadEncryptedRunnerCryptoMock.mockRejectedValue(
      new Error("preload unavailable"),
    );
    const unhandledRejection = vi.fn();
    window.addEventListener("unhandledrejection", unhandledRejection);

    await import("./preload");
    await vi.waitFor(() => {
      expect(preloadEncryptedRunnerCryptoMock).toHaveBeenCalledOnce();
    });
    await Promise.resolve();

    expect(unhandledRejection).not.toHaveBeenCalled();
    window.removeEventListener("unhandledrejection", unhandledRejection);
  });
});
