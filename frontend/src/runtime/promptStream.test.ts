import { describe, expect, it, vi } from "vitest";

import type { RuntimeTransport } from "./runtimeTransport";
import { startRuntimePrompt } from "./promptStream";

const sessionId = "a".repeat(32);
const runId = "b".repeat(32);

function transport(): RuntimeTransport {
  return {
    runtime: { location: "hosted" },
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    upload: vi.fn(),
  };
}

describe("runtime prompt stream", () => {
  it("reconnects from durable event sequence until terminal status", async () => {
    const runtimeTransport = transport();
    vi.mocked(runtimeTransport.post).mockResolvedValue({
      id: runId,
      session_id: sessionId,
      status: "starting",
    });
    vi.mocked(runtimeTransport.get)
      .mockResolvedValueOnce({
        run_id: runId,
        status: "running",
        events: [{ type: "status", status: "started" }],
        next_sequence: 1,
      })
      .mockResolvedValueOnce({
        run_id: runId,
        status: "completed",
        events: [{ type: "result" }],
        next_sequence: 2,
      });

    const handle = await startRuntimePrompt(runtimeTransport, sessionId, {
      prompt: "hello",
      idempotencyKey: "22222222-2222-4222-8222-222222222222",
      pollDelayMs: 0,
    });
    const events = [];
    for await (const event of handle.events()) {
      events.push(event);
    }

    expect(events.map((event) => event.type)).toEqual(["status", "result"]);
    expect(runtimeTransport.get).toHaveBeenNthCalledWith(
      1,
      `/api/sessions/${sessionId}/runs/${runId}/events/0`,
    );
    expect(runtimeTransport.get).toHaveBeenNthCalledWith(
      2,
      `/api/sessions/${sessionId}/runs/${runId}/events/1`,
    );
  });

  it("cancels the exact run idempotently", async () => {
    const runtimeTransport = transport();
    vi.mocked(runtimeTransport.post)
      .mockResolvedValueOnce({ id: runId, session_id: sessionId, status: "starting" })
      .mockResolvedValueOnce({ id: runId, session_id: sessionId, status: "cancelled" });
    const handle = await startRuntimePrompt(runtimeTransport, sessionId, {
      prompt: "hello",
      idempotencyKey: "22222222-2222-4222-8222-222222222222",
      pollDelayMs: 0,
    });

    await expect(handle.cancel()).resolves.toBe("cancelled");
    expect(runtimeTransport.post).toHaveBeenLastCalledWith(
      `/api/sessions/${sessionId}/runs/${runId}/cancel`,
    );
  });

  it("rejects a non-contiguous server cursor", async () => {
    const runtimeTransport = transport();
    vi.mocked(runtimeTransport.post).mockResolvedValue({
      id: runId,
      session_id: sessionId,
      status: "starting",
    });
    vi.mocked(runtimeTransport.get).mockResolvedValue({
      run_id: runId,
      status: "running",
      events: [{ type: "status", status: "started" }],
      next_sequence: 9,
    });
    const handle = await startRuntimePrompt(runtimeTransport, sessionId, {
      prompt: "hello",
      idempotencyKey: "22222222-2222-4222-8222-222222222222",
      pollDelayMs: 0,
    });

    await expect(async () => {
      for await (const _event of handle.events()) {
        // Consume the generator to trigger validation.
      }
    }).rejects.toThrow("sequence");
  });
});
