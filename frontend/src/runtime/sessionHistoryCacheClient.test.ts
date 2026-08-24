import { describe, expect, it, vi } from "vitest";

import { SessionHistoryCacheClient } from "./sessionHistoryCacheClient";

const USER = "user";
const SESSION = "0123456789abcdef0123456789abcdef";

class FakePort {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onmessageerror: (() => void) | null = null;
  posted: unknown[] = [];
  start = vi.fn();
  close = vi.fn();
  postMessage = vi.fn((message: unknown) => this.posted.push(message));
  reply(value: unknown) {
    this.onmessage?.({ data: value } as MessageEvent);
  }
}

class FakeWorker {
  onerror: ((event: ErrorEvent) => void) | null = null;
  constructor(readonly port: FakePort) {}
  fail() {
    this.onerror?.(new ErrorEvent("error"));
  }
}

function response(port: FakePort, extra: Record<string, unknown>) {
  const request = port.posted[port.posted.length - 1] as { requestId: string };
  return { version: 1, requestId: request.requestId, ...extra };
}

describe("SessionHistoryCacheClient", () => {
  it("returns a hit from a strict response", async () => {
    const port = new FakePort();
    const worker = new FakeWorker(port);
    const client = new SessionHistoryCacheClient(() => worker as never);
    const pending = client.get(USER, SESSION);
    port.reply(
      response(port, { ok: true, hit: true, envelopes: [{ data: "x" }] }),
    );
    await expect(pending).resolves.toEqual([{ data: "x" }]);
  });

  it("treats construction failure, timeout, malformed and late responses as misses", async () => {
    const broken = new SessionHistoryCacheClient(() => {
      throw new Error("blocked");
    });
    await expect(broken.get(USER, SESSION)).resolves.toBeNull();

    vi.useFakeTimers();
    const port = new FakePort();
    const client = new SessionHistoryCacheClient(() => ({ port }) as never);
    const pending = client.get(USER, SESSION);
    await vi.advanceTimersByTimeAsync(251);
    await expect(pending).resolves.toBeNull();
    port.reply(response(port, { ok: true, hit: true, envelopes: [] }));
    const malformed = client.get(USER, SESSION);
    port.reply(
      response(port, { ok: true, hit: true, envelopes: [], extra: true }),
    );
    await expect(malformed).resolves.toBeNull();
    vi.useRealTimers();
  });

  it("marks the retained worker unavailable after worker errors", async () => {
    const port = new FakePort();
    const worker = new FakeWorker(port);
    const factory = vi.fn(() => worker as never);
    const client = new SessionHistoryCacheClient(factory);
    const first = client.get(USER, SESSION);
    worker.fail();
    await expect(first).resolves.toBeNull();
    expect(port.close).toHaveBeenCalled();
    await expect(client.get(USER, SESSION)).resolves.toBeNull();
    expect(factory).toHaveBeenCalledTimes(1);
  });

  it("resolves pending requests on port errors and disposal", async () => {
    const port = new FakePort();
    const client = new SessionHistoryCacheClient(
      () => new FakeWorker(port) as never,
    );
    const first = client.get(USER, SESSION);
    port.onmessageerror?.();
    await expect(first).resolves.toBeNull();
    const second = client.get(USER, SESSION);
    client.dispose();
    await expect(second).resolves.toBeNull();
    expect(port.close).toHaveBeenCalled();
  });

  it("bounds pending requests and ignores failed writes", async () => {
    const port = new FakePort();
    const client = new SessionHistoryCacheClient(() => ({ port }) as never);
    const pending = Array.from({ length: 32 }, () => client.get(USER, SESSION));
    await expect(client.get(USER, SESSION)).resolves.toBeNull();
    port.onmessageerror?.();
    await Promise.all(pending);
    await expect(client.put(USER, SESSION, [])).resolves.toBeUndefined();
  });
});
