import { describe, expect, it, vi } from "vitest";

import { HistoryCacheStore } from "./sessionHistoryCacheCore";
import {
  authenticateSessionHistoryCacheUser,
  bindHistoryCachePort,
} from "./sessionHistoryCachePort";

const SESSION = "0123456789abcdef0123456789abcdef";

class FakePort {
  onmessage: ((event: MessageEvent) => void) | null = null;
  posted: unknown[] = [];
  start = vi.fn();
  close = vi.fn();
  postMessage = vi.fn((value: unknown) => this.posted.push(value));
  request(value: unknown) {
    this.onmessage?.({ data: value } as MessageEvent);
  }
}

function request(type: "get" | "put", userId: string, requestId: string) {
  return {
    version: 1,
    type,
    requestId,
    userId,
    sessionId: SESSION,
    ...(type === "put" ? { envelopes: [{ data: userId }] } : {}),
  };
}

describe("bindHistoryCachePort", () => {
  it("authenticates from same-origin auth state with included credentials", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ authenticated: true, user_id: "user-1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(authenticateSessionHistoryCacheUser(fetcher)).resolves.toBe(
      "user-1",
    );
    expect(fetcher).toHaveBeenCalledWith("/auth/me", {
      cache: "no-store",
      credentials: "include",
      headers: { Accept: "application/json" },
    });
  });

  it("binds each port to its authenticated identity across account switches", async () => {
    const store = new HistoryCacheStore();
    const first = new FakePort();
    await bindHistoryCachePort(first as never, store, async () => "user-1");
    first.request(request("put", "user-1", "put-1"));
    expect(first.posted).toHaveLength(1);

    const second = new FakePort();
    await bindHistoryCachePort(second as never, store, async () => "user-2");
    second.request(request("get", "user-1", "cross-account"));
    expect(second.posted).toHaveLength(0);
    second.request(request("get", "user-2", "get-2"));
    expect(second.posted).toEqual([
      expect.objectContaining({ requestId: "get-2", hit: false }),
    ]);

    first.request(request("get", "user-1", "get-1"));
    expect(first.posted[first.posted.length - 1]).toEqual(
      expect.objectContaining({ requestId: "get-1", hit: true }),
    );
  });

  it("closes ports when authentication fails or returns invalid identity", async () => {
    for (const authenticate of [
      async () => null,
      async () => " bad ",
      async () => "x".repeat(257),
      async () => {
        throw new Error("offline");
      },
    ]) {
      const port = new FakePort();
      await bindHistoryCachePort(
        port as never,
        new HistoryCacheStore(),
        authenticate,
      );
      expect(port.close).toHaveBeenCalled();
      expect(port.onmessage).toBeNull();
    }
  });
});
