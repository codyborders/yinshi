import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "./client";

afterEach(() => {
  delete (window as { yinshiDesktop?: YinshiDesktopBridge }).yinshiDesktop;
  vi.unstubAllGlobals();
});

describe("desktop hosted API bridge", () => {
  it("routes only hosted runner settings through Electron IPC", async () => {
    const hostedRequest = vi.fn().mockResolvedValue({
      status: 200,
      body: { id: "runner-1", status: "online" },
    });
    (window as { yinshiDesktop?: YinshiDesktopBridge }).yinshiDesktop = {
      hostedRequest,
      importLocalRepository: vi.fn(),
      signOut: vi.fn(),
    };
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);

    await expect(api.get("/api/settings/runner")).resolves.toEqual({
      id: "runner-1",
      status: "online",
    });
    expect(hostedRequest).toHaveBeenCalledWith({
      method: "GET",
      path: "/api/settings/runner",
    });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("converts hosted errors without exposing the desktop token", async () => {
    const hostedRequest = vi.fn().mockResolvedValue({
      status: 409,
      body: { detail: "Runner Noise key must be confirmed" },
    });
    (window as { yinshiDesktop?: YinshiDesktopBridge }).yinshiDesktop = {
      hostedRequest,
      importLocalRepository: vi.fn(),
      signOut: vi.fn(),
    };

    const error = await api
      .post("/api/settings/runner/capabilities", {
        initiator_public_key: "client-key",
        scopes: ["worker.health"],
        max_session_bytes: 65_536,
      })
      .catch((caughtError: unknown) => caughtError);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toEqual(
      expect.objectContaining({
        status: 409,
        message: "Runner Noise key must be confirmed",
      }),
    );
  });
});
