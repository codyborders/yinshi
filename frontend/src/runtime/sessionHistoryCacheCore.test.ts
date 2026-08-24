import { describe, expect, it } from "vitest";

import {
  HistoryCacheStore,
  processHistoryCacheRequest,
} from "./sessionHistoryCacheCore";

const USER = "user-1";
const SESSION = "0123456789abcdef0123456789abcdef";
const OTHER_SESSION = "fedcba9876543210fedcba9876543210";
const envelope = [{ version: 1, data: "abc" }];

function request(
  type: "get" | "put" | "delete",
  overrides: Record<string, unknown> = {},
) {
  return {
    version: 1,
    type,
    requestId: `request-${type}`,
    userId: USER,
    sessionId: SESSION,
    ...(type === "put" ? { envelopes: envelope } : {}),
    ...overrides,
  };
}

describe("HistoryCacheStore", () => {
  it("isolates users and sessions and expires entries", () => {
    let now = 1_000;
    const store = new HistoryCacheStore({ now: () => now });
    expect(processHistoryCacheRequest(store, request("put"))).toMatchObject({
      ok: true,
    });
    expect(processHistoryCacheRequest(store, request("get"))).toMatchObject({
      hit: true,
      envelopes: envelope,
    });
    expect(
      processHistoryCacheRequest(store, request("get", { userId: "user-2" })),
    ).toMatchObject({ hit: false });
    expect(
      processHistoryCacheRequest(
        store,
        request("get", { sessionId: OTHER_SESSION }),
      ),
    ).toMatchObject({ hit: false });
    now += 600_001;
    expect(processHistoryCacheRequest(store, request("get"))).toMatchObject({
      hit: false,
    });
  });

  it("uses LRU eviction and correct replacement byte accounting", () => {
    const store = new HistoryCacheStore({
      maxEntries: 2,
      maxTotalBytes: 120,
      maxEntryBytes: 100,
    });
    processHistoryCacheRequest(
      store,
      request("put", { sessionId: SESSION, envelopes: [{ data: "a" }] }),
    );
    processHistoryCacheRequest(
      store,
      request("put", { sessionId: OTHER_SESSION, envelopes: [{ data: "b" }] }),
    );
    processHistoryCacheRequest(store, request("get", { sessionId: SESSION }));
    processHistoryCacheRequest(
      store,
      request("put", {
        sessionId: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        envelopes: [{ data: "c" }],
      }),
    );
    expect(
      processHistoryCacheRequest(
        store,
        request("get", { sessionId: OTHER_SESSION }),
      ),
    ).toMatchObject({ hit: false });
    expect(
      processHistoryCacheRequest(store, request("get", { sessionId: SESSION })),
    ).toMatchObject({ hit: true });
    processHistoryCacheRequest(
      store,
      request("put", {
        sessionId: SESSION,
        envelopes: [{ data: "replacement" }],
      }),
    );
    expect(store.totalBytes).toBe(
      JSON.stringify([{ data: "replacement" }]).length +
        JSON.stringify([{ data: "c" }]).length,
    );
  });

  it("evicts least-recently-used entries when aggregate bytes exceed the bound", () => {
    const store = new HistoryCacheStore({
      maxEntries: 8,
      maxEntryBytes: 100,
      maxTotalBytes: 30,
    });
    processHistoryCacheRequest(
      store,
      request("put", { sessionId: SESSION, envelopes: [{ data: "aaaaa" }] }),
    );
    processHistoryCacheRequest(
      store,
      request("put", {
        sessionId: OTHER_SESSION,
        envelopes: [{ data: "bbbbb" }],
      }),
    );
    expect(
      processHistoryCacheRequest(store, request("get", { sessionId: SESSION })),
    ).toMatchObject({ hit: false });
    expect(
      processHistoryCacheRequest(
        store,
        request("get", { sessionId: OTHER_SESSION }),
      ),
    ).toMatchObject({ hit: true });
  });

  it("strictly rejects malformed messages, keys, IDs, and oversized values", () => {
    const store = new HistoryCacheStore({ maxEntryBytes: 20 });
    for (const malformed of [
      request("get", { extra: true }),
      request("get", { version: 2 }),
      request("get", { requestId: "bad id" }),
      request("get", { userId: "" }),
      request("get", { userId: "x".repeat(257) }),
      request("get", { sessionId: "bad" }),
      request("put", { envelopes: "bad" }),
      request("put", { envelopes: [{ data: "x".repeat(30) }] }),
    ]) {
      expect(processHistoryCacheRequest(store, malformed)).toBeNull();
    }
  });

  it("deletes exact entries", () => {
    const store = new HistoryCacheStore();
    processHistoryCacheRequest(store, request("put"));
    expect(processHistoryCacheRequest(store, request("delete"))).toMatchObject({
      ok: true,
    });
    expect(processHistoryCacheRequest(store, request("get"))).toMatchObject({
      hit: false,
    });
  });
});
