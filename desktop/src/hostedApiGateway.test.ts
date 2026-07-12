// Covers the renderer-to-hosted API boundary with exact route and response limits.

import { describe, expect, it, vi } from "vitest";

import { HostedApiGateway } from "./hostedApiGateway.js";

const runner = {
  id: "runner-1",
  noise_key_confirmed: false,
};

describe("HostedApiGateway", () => {
  it("adds the memory-only bearer token for an allowlisted runner request", async () => {
    const fetch = vi.fn(async (_input: string | URL, _init?: RequestInit) =>
      new Response(JSON.stringify(runner), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const gateway = new HostedApiGateway({
      apiBaseUrl: "https://yinshi.example/",
      fetch,
      getAccessToken: async () => "memory-only-access-token-value-123456789",
    });

    const response = await gateway.request({
      method: "GET",
      path: "/api/settings/runner",
    });

    expect(response).toEqual({ status: 200, body: runner });
    expect(fetch).toHaveBeenCalledOnce();
    const [url, init] = fetch.mock.calls[0]!;
    expect(url.toString()).toBe("https://yinshi.example/api/settings/runner");
    expect(init).toEqual(
      expect.objectContaining({
        method: "GET",
        redirect: "error",
        headers: {
          Accept: "application/json",
          Authorization: "Bearer memory-only-access-token-value-123456789",
        },
      }),
    );
  });

  it("adds CSRF proof to an allowlisted hosted mutation", async () => {
    const fetch = vi.fn(async (_input: string | URL, _init?: RequestInit) =>
      new Response(JSON.stringify({ capability: "signed" }), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const gateway = new HostedApiGateway({
      apiBaseUrl: "https://yinshi.example",
      fetch,
      getAccessToken: async () => "memory-only-access-token-value-123456789",
    });

    await gateway.request({
      method: "POST",
      path: "/api/settings/runner/capabilities",
      body: { scopes: ["worker.health"] },
    });

    expect(fetch.mock.calls[0]?.[1]?.headers).toEqual({
      Accept: "application/json",
      Authorization: "Bearer memory-only-access-token-value-123456789",
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest",
    });
  });

  it("rejects route confusion, invalid bodies, and oversized responses", async () => {
    const fetch = vi.fn(async (_input: string | URL, _init?: RequestInit) =>
      new Response("x".repeat(1_048_577), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const gateway = new HostedApiGateway({
      apiBaseUrl: "https://yinshi.example",
      fetch,
      getAccessToken: async () => "memory-only-access-token-value-123456789",
    });

    await expect(
      gateway.request({ method: "GET", path: "/api/settings/runner/../devices" }),
    ).rejects.toThrow("Hosted API route is not allowed");
    await expect(
      gateway.request({
        method: "GET",
        path: "/api/settings/runner",
        body: { unexpected: true },
      }),
    ).rejects.toThrow("GET hosted requests cannot include a body");
    await expect(
      gateway.request({ method: "GET", path: "/api/settings/runner" }),
    ).rejects.toThrow("Hosted API response exceeded the size limit");
  });
});
