import { describe, expect, it, vi } from "vitest";

import { RunnerRelayConnectionError } from "../runner/encryptedRunnerClient";
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
    close: vi.fn(),
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

  it("resumes from the durable sequence after a relay disconnect", async () => {
    vi.useFakeTimers();
    try {
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
        .mockRejectedValueOnce(
          new RunnerRelayConnectionError(
            "Runner relay closed before responding",
          ),
        )
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
      const events: string[] = [];
      const collect = (async () => {
        for await (const event of handle.events()) {
          events.push(event.type);
        }
      })();

      await vi.runAllTimersAsync();
      await collect;

      expect(events).toEqual(["status", "result"]);
      expect(runtimeTransport.get).toHaveBeenNthCalledWith(
        1,
        `/api/sessions/${sessionId}/runs/${runId}/events/0`,
      );
      expect(runtimeTransport.get).toHaveBeenNthCalledWith(
        2,
        `/api/sessions/${sessionId}/runs/${runId}/events/1`,
      );
      expect(runtimeTransport.get).toHaveBeenNthCalledWith(
        3,
        `/api/sessions/${sessionId}/runs/${runId}/events/1`,
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("uses the remote polling interval for a managed runtime", async () => {
    vi.useFakeTimers();
    try {
      const runtimeTransport: RuntimeTransport = {
        ...transport(),
        runtime: {
          location: "managed",
          runnerPublicKey: "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
        },
      };
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: runId,
        session_id: sessionId,
        status: "starting",
      });
      vi.mocked(runtimeTransport.get)
        .mockResolvedValueOnce({
          run_id: runId,
          status: "running",
          events: [],
          next_sequence: 0,
        })
        .mockResolvedValueOnce({
          run_id: runId,
          status: "completed",
          events: [{ type: "result" }],
          next_sequence: 1,
        });
      const handle = await startRuntimePrompt(runtimeTransport, sessionId, {
        prompt: "hello",
        idempotencyKey: "22222222-2222-4222-8222-222222222222",
      });

      const nextEvent = handle.events().next();
      await vi.advanceTimersByTimeAsync(749);
      expect(runtimeTransport.get).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1);

      await expect(nextEvent).resolves.toMatchObject({
        done: false,
        value: { type: "result" },
      });
      expect(runtimeTransport.get).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("removes the abort listener after a poll delay resolves", async () => {
    vi.useFakeTimers();
    try {
      const runtimeTransport = transport();
      const controller = new AbortController();
      const addListener = vi.spyOn(controller.signal, "addEventListener");
      const removeListener = vi.spyOn(controller.signal, "removeEventListener");
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: runId,
        session_id: sessionId,
        status: "starting",
      });
      vi.mocked(runtimeTransport.get)
        .mockResolvedValueOnce({
          run_id: runId,
          status: "running",
          events: [],
          next_sequence: 0,
        })
        .mockResolvedValueOnce({
          run_id: runId,
          status: "completed",
          events: [{ type: "result" }],
          next_sequence: 1,
        });
      const handle = await startRuntimePrompt(runtimeTransport, sessionId, {
        prompt: "hello",
        idempotencyKey: "22222222-2222-4222-8222-222222222222",
        pollDelayMs: 25,
        signal: controller.signal,
      });

      const nextEvent = handle.events().next();
      await vi.advanceTimersByTimeAsync(25);
      await expect(nextEvent).resolves.toMatchObject({
        done: false,
        value: { type: "result" },
      });
      const abortListener = addListener.mock.calls.find(
        ([eventName]) => eventName === "abort",
      )?.[1];

      expect(abortListener).toBeTypeOf("function");
      expect(removeListener).toHaveBeenCalledWith("abort", abortListener);
    } finally {
      vi.useRealTimers();
    }
  });

  it("clears a pending poll timer and rejects once when aborted", async () => {
    vi.useFakeTimers();
    try {
      const runtimeTransport = transport();
      const controller = new AbortController();
      const clearTimeout = vi.spyOn(window, "clearTimeout");
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: runId,
        session_id: sessionId,
        status: "starting",
      });
      vi.mocked(runtimeTransport.get).mockResolvedValue({
        run_id: runId,
        status: "running",
        events: [],
        next_sequence: 0,
      });
      const handle = await startRuntimePrompt(runtimeTransport, sessionId, {
        prompt: "hello",
        idempotencyKey: "22222222-2222-4222-8222-222222222222",
        pollDelayMs: 25,
        signal: controller.signal,
      });

      const nextEvent = handle.events().next();
      await vi.advanceTimersByTimeAsync(0);
      expect(vi.getTimerCount()).toBe(1);
      const rejection = expect(nextEvent).rejects.toMatchObject({
        name: "AbortError",
      });

      controller.abort();
      controller.abort();

      await rejection;
      expect(clearTimeout).toHaveBeenCalledOnce();
      expect(runtimeTransport.get).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it("surfaces the last relay error at the injected retry limit", async () => {
    vi.useFakeTimers();
    try {
      const runtimeTransport = transport();
      const firstError = new RunnerRelayConnectionError(
        "Runner relay closed before responding",
      );
      const lastError = new RunnerRelayConnectionError(
        "Runner relay response timed out",
      );
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: runId,
        session_id: sessionId,
        status: "starting",
      });
      vi.mocked(runtimeTransport.get)
        .mockRejectedValueOnce(firstError)
        .mockRejectedValueOnce(lastError);
      const handle = await startRuntimePrompt(runtimeTransport, sessionId, {
        prompt: "hello",
        idempotencyKey: "22222222-2222-4222-8222-222222222222",
        pollDelayMs: 0,
        pollRetryLimit: 2,
      });

      const nextEvent = handle.events().next();
      const rejection = expect(nextEvent).rejects.toBe(lastError);
      await vi.runAllTimersAsync();

      await rejection;
      expect(runtimeTransport.get).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("surfaces the last transient polling error at the injected retry limit", async () => {
    vi.useFakeTimers();
    try {
      const runtimeTransport = transport();
      const firstError = Object.assign(new Error("busy"), { status: 503 });
      const lastError = Object.assign(new Error("still busy"), { status: 503 });
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: runId,
        session_id: sessionId,
        status: "starting",
      });
      vi.mocked(runtimeTransport.get)
        .mockRejectedValueOnce(firstError)
        .mockRejectedValueOnce(lastError);
      const handle = await startRuntimePrompt(runtimeTransport, sessionId, {
        prompt: "hello",
        idempotencyKey: "22222222-2222-4222-8222-222222222222",
        pollDelayMs: 0,
        pollRetryLimit: 2,
      });

      const nextEvent = handle.events().next();
      const rejection = expect(nextEvent).rejects.toBe(lastError);
      await vi.runAllTimersAsync();

      await rejection;
      expect(runtimeTransport.get).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ["HTTP 429", Object.assign(new Error("busy"), { status: 429 })],
    ["HTTP 500", Object.assign(new Error("unavailable"), { status: 500 })],
    ["runner 599", Object.assign(new Error("runner unavailable"), { status: 599 })],
    ["fetch TypeError", new TypeError("fetch failed")],
  ])("retries transient %s polling errors", async (_label, pollingError) => {
    vi.useFakeTimers();
    try {
      const runtimeTransport = transport();
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: runId,
        session_id: sessionId,
        status: "starting",
      });
      vi.mocked(runtimeTransport.get)
        .mockRejectedValueOnce(pollingError)
        .mockResolvedValueOnce({
          run_id: runId,
          status: "completed",
          events: [{ type: "result" }],
          next_sequence: 1,
        });
      const handle = await startRuntimePrompt(runtimeTransport, sessionId, {
        prompt: "hello",
        idempotencyKey: "22222222-2222-4222-8222-222222222222",
        pollDelayMs: 0,
        pollRetryLimit: 2,
      });

      const nextEvent = handle.events().next();
      await vi.runAllTimersAsync();

      await expect(nextEvent).resolves.toMatchObject({
        done: false,
        value: { type: "result" },
      });
      expect(runtimeTransport.get).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("caps managed retry delays and defaults to five transient failures", async () => {
    vi.useFakeTimers();
    try {
      const runtimeTransport: RuntimeTransport = {
        ...transport(),
        runtime: {
          location: "managed",
          runnerPublicKey: "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
        },
      };
      const lastError = Object.assign(new Error("last failure"), { status: 503 });
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: runId,
        session_id: sessionId,
        status: "starting",
      });
      vi.mocked(runtimeTransport.get)
        .mockRejectedValueOnce(Object.assign(new Error("failure 1"), { status: 503 }))
        .mockRejectedValueOnce(Object.assign(new Error("failure 2"), { status: 503 }))
        .mockRejectedValueOnce(Object.assign(new Error("failure 3"), { status: 503 }))
        .mockRejectedValueOnce(Object.assign(new Error("failure 4"), { status: 503 }))
        .mockRejectedValueOnce(lastError);
      const handle = await startRuntimePrompt(runtimeTransport, sessionId, {
        prompt: "hello",
        idempotencyKey: "22222222-2222-4222-8222-222222222222",
      });

      const nextEvent = handle.events().next();
      const rejection = expect(nextEvent).rejects.toBe(lastError);
      await vi.advanceTimersByTimeAsync(1_499);
      expect(runtimeTransport.get).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(runtimeTransport.get).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(3_000);
      expect(runtimeTransport.get).toHaveBeenCalledTimes(3);
      await vi.advanceTimersByTimeAsync(4_999);
      expect(runtimeTransport.get).toHaveBeenCalledTimes(3);
      await vi.advanceTimersByTimeAsync(1);
      expect(runtimeTransport.get).toHaveBeenCalledTimes(4);
      await vi.advanceTimersByTimeAsync(5_000);

      await rejection;
      expect(runtimeTransport.get).toHaveBeenCalledTimes(5);
    } finally {
      vi.useRealTimers();
    }
  });

  it("resets transient failure count after a valid poll response", async () => {
    vi.useFakeTimers();
    try {
      const runtimeTransport = transport();
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: runId,
        session_id: sessionId,
        status: "starting",
      });
      vi.mocked(runtimeTransport.get)
        .mockRejectedValueOnce(new TypeError("first disconnect"))
        .mockResolvedValueOnce({
          run_id: runId,
          status: "running",
          events: [],
          next_sequence: 0,
        })
        .mockRejectedValueOnce(new TypeError("second disconnect"))
        .mockResolvedValueOnce({
          run_id: runId,
          status: "completed",
          events: [{ type: "result" }],
          next_sequence: 1,
        });
      const handle = await startRuntimePrompt(runtimeTransport, sessionId, {
        prompt: "hello",
        idempotencyKey: "22222222-2222-4222-8222-222222222222",
        pollDelayMs: 0,
        pollRetryLimit: 2,
      });

      const nextEvent = handle.events().next();
      await vi.runAllTimersAsync();

      await expect(nextEvent).resolves.toMatchObject({
        done: false,
        value: { type: "result" },
      });
      expect(runtimeTransport.get).toHaveBeenCalledTimes(4);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ["ordinary client status", Object.assign(new Error("not found"), { status: 404 })],
    ["abort", new DOMException("aborted", "AbortError")],
    ["generic transport error", new Error("identity changed")],
    [
      "untyped relay wording",
      new Error("Runner relay closed before responding"),
    ],
  ])("does not retry %s polling errors", async (_label, pollingError) => {
    const runtimeTransport = transport();
    vi.mocked(runtimeTransport.post).mockResolvedValue({
      id: runId,
      session_id: sessionId,
      status: "starting",
    });
    vi.mocked(runtimeTransport.get).mockRejectedValue(pollingError);
    const handle = await startRuntimePrompt(runtimeTransport, sessionId, {
      prompt: "hello",
      idempotencyKey: "22222222-2222-4222-8222-222222222222",
      pollDelayMs: 0,
    });

    await expect(handle.events().next()).rejects.toBe(pollingError);
    expect(runtimeTransport.get).toHaveBeenCalledTimes(1);
  });

  it.each([0, 6])("rejects an out-of-bounds retry limit of %s", async (pollRetryLimit) => {
    const runtimeTransport = transport();

    await expect(
      startRuntimePrompt(runtimeTransport, sessionId, {
        prompt: "hello",
        idempotencyKey: "22222222-2222-4222-8222-222222222222",
        pollRetryLimit,
      }),
    ).rejects.toThrow("retry limit");
    expect(runtimeTransport.post).not.toHaveBeenCalled();
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
    expect(runtimeTransport.get).toHaveBeenCalledTimes(1);
  });
});
