import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { cancelSessionMock, streamPromptMock } = vi.hoisted(() => ({
  cancelSessionMock: vi.fn(),
  streamPromptMock: vi.fn(),
}));

vi.mock("../../api/client", () => ({
  cancelSession: cancelSessionMock,
  normalizeEvent: (event: unknown) => event,
  streamPrompt: streamPromptMock,
}));

import type { RuntimeTransport } from "../../runtime/runtimeTransport";
import { useAgentStream } from "../useAgentStream";

function runtimeTransport(): RuntimeTransport {
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

function createDeferredPromise(): {
  promise: Promise<void>;
  resolve: () => void;
} {
  let resolvePromise!: () => void;
  const promise = new Promise<void>((resolve) => {
    resolvePromise = resolve;
  });
  return { promise, resolve: resolvePromise };
}

describe("useAgentStream", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cancelSessionMock.mockResolvedValue(undefined);
  });

  it("replays an active durable run without starting a duplicate", async () => {
    const sessionId = "a".repeat(32);
    const runId = "b".repeat(32);
    const transport = runtimeTransport();
    vi.mocked(transport.get).mockResolvedValueOnce({
      run_id: runId,
      status: "completed",
      events: [
        { type: "tool_use", id: "tool-1", name: "read", input: {} },
        { type: "tool_result", tool_use_id: "tool-1", content: "restored" },
        { type: "result" },
      ],
      next_sequence: 3,
    });
    const { result } = renderHook(() => useAgentStream(sessionId, transport));

    act(() => {
      result.current.bootstrapSession(
        [
          {
            id: "user-1",
            role: "user",
            content: "inspect",
            blocks: [],
            timestamp: 1,
          },
        ],
        runId,
      );
    });

    await waitFor(() => expect(result.current.runState).toBe("idle"));
    const assistant = result.current.messages.find(
      (message) => message.id === runId,
    );
    expect(assistant?.blocks).toEqual([
      expect.objectContaining({
        type: "tool_use",
        toolName: "read",
        toolOutput: "restored",
      }),
    ]);
    expect(transport.post).not.toHaveBeenCalled();
    expect(transport.get).toHaveBeenCalledWith(
      `/api/sessions/${sessionId}/runs/${runId}/events/0`,
    );
  });

  it("cancels a resumed run and drains one queued steering prompt", async () => {
    const sessionId = "a".repeat(32);
    const resumedRunId = "b".repeat(32);
    const queuedRunId = "c".repeat(32);
    const transport = runtimeTransport();
    let releaseResumedRun!: (value: unknown) => void;
    vi.mocked(transport.get)
      .mockReturnValueOnce(
        new Promise((resolve) => {
          releaseResumedRun = resolve;
        }),
      )
      .mockResolvedValueOnce({
        run_id: queuedRunId,
        status: "completed",
        events: [
          {
            type: "assistant",
            message: { content: [{ type: "text", text: "queued complete" }] },
          },
          { type: "result" },
        ],
        next_sequence: 2,
      });
    vi.mocked(transport.post).mockImplementation(async (path) => {
      if (path.endsWith(`/${resumedRunId}/cancel`)) {
        return {
          id: resumedRunId,
          session_id: sessionId,
          status: "stopping",
        };
      }
      if (path === `/api/sessions/${sessionId}/runs`) {
        return {
          id: queuedRunId,
          session_id: sessionId,
          status: "starting",
        };
      }
      throw new Error(`Unexpected POST ${path}`);
    });
    const { result } = renderHook(() => useAgentStream(sessionId, transport));

    act(() => {
      result.current.bootstrapSession([], resumedRunId);
    });
    await waitFor(() => expect(result.current.runState).toBe("running"));
    await act(async () => {
      await result.current.sendPrompt("steer next");
    });
    act(() => {
      releaseResumedRun({
        run_id: resumedRunId,
        status: "cancelled",
        events: [{ type: "cancelled" }],
        next_sequence: 1,
      });
    });

    await waitFor(() => {
      expect(transport.post).toHaveBeenCalledWith(
        `/api/sessions/${sessionId}/runs`,
        expect.objectContaining({ prompt: "steer next" }),
      );
    });
    await waitFor(() => expect(result.current.runState).toBe("idle"));
    expect(transport.post).toHaveBeenCalledTimes(2);
    expect(transport.post).toHaveBeenNthCalledWith(
      1,
      `/api/sessions/${sessionId}/runs/${resumedRunId}/cancel`,
    );
    expect(
      result.current.messages.filter(
        (message) => message.content === "steer next",
      ),
    ).toHaveLength(1);
  });

  it("ignores late events from the previously selected session", async () => {
    const sessionA = "a".repeat(32);
    const sessionB = "c".repeat(32);
    const runA = "b".repeat(32);
    const transportA = runtimeTransport();
    const transportB = runtimeTransport();
    let releaseRunA!: (value: unknown) => void;
    vi.mocked(transportA.get).mockReturnValueOnce(
      new Promise((resolve) => {
        releaseRunA = resolve;
      }),
    );
    const { result, rerender } = renderHook(
      ({ sessionId, transport }) => useAgentStream(sessionId, transport),
      { initialProps: { sessionId: sessionA, transport: transportA } },
    );

    act(() => {
      result.current.bootstrapSession([], runA);
    });
    rerender({ sessionId: sessionB, transport: transportB });
    act(() => {
      result.current.bootstrapSession(
        [
          {
            id: "user-b",
            role: "user",
            content: "session B",
            blocks: [],
            timestamp: 2,
          },
        ],
        null,
      );
      releaseRunA({
        run_id: runA,
        status: "completed",
        events: [
          {
            type: "assistant",
            message: { content: [{ type: "text", text: "stale A" }] },
          },
          { type: "result" },
        ],
        next_sequence: 2,
      });
    });

    await waitFor(() => {
      expect(result.current.messages.map((message) => message.content)).toEqual(
        ["session B"],
      );
    });
    expect(transportA.post).not.toHaveBeenCalled();
  });

  it("restores a local run after cancellation fails and reports one safe error", async () => {
    const currentTurnFinished = createDeferredPromise();
    cancelSessionMock.mockRejectedValueOnce(
      new Error("provider request failed for secret session path"),
    );
    streamPromptMock.mockImplementationOnce(async function* () {
      yield {
        type: "assistant",
        message: { content: [{ type: "text", text: "working" }] },
      };
      await currentTurnFinished.promise;
      yield { type: "result" };
    });

    const { result } = renderHook(() => useAgentStream("sess-1"));

    let promptPromise: Promise<void> | null = null;
    await act(async () => {
      promptPromise = result.current.sendPrompt("keep working");
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(result.current.runState).toBe("running");
    });

    await act(async () => {
      await result.current.cancel();
    });

    expect(result.current.runState).toBe("running");
    expect(
      result.current.messages
        .filter((message) => message.role === "error")
        .map((message) => message.content),
    ).toEqual(["Could not stop the current response. Try again."]);
    expect(streamPromptMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await result.current.cancel();
    });

    expect(cancelSessionMock).toHaveBeenCalledTimes(2);
    expect(result.current.runState).toBe("stopping");
    expect(
      result.current.messages.filter((message) => message.role === "error"),
    ).toHaveLength(1);
    expect(streamPromptMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      currentTurnFinished.resolve();
      if (promptPromise === null) {
        throw new Error("Prompt promise should be initialized");
      }
      await promptPromise;
    });
    expect(result.current.runState).toBe("idle");
  });

  it("replays a queued steering prompt after the current run completes", async () => {
    const firstTurnFinished = createDeferredPromise();

    streamPromptMock
      .mockImplementationOnce(async function* () {
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "first reply" }] },
        };
        await firstTurnFinished.promise;
        yield { type: "result" };
      })
      .mockImplementationOnce(async function* () {
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "second reply" }] },
        };
        yield { type: "result" };
      });

    const { result } = renderHook(() => useAgentStream("sess-1"));

    let firstPromptPromise: Promise<void> | null = null;
    await act(async () => {
      firstPromptPromise = result.current.sendPrompt("first prompt");
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(result.current.runState).toBe("running");
    });

    await act(async () => {
      await result.current.sendPrompt("second prompt");
    });

    expect(cancelSessionMock).toHaveBeenCalledWith("sess-1");
    expect(result.current.runState).toBe("stopping");

    await act(async () => {
      firstTurnFinished.resolve();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(streamPromptMock).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(result.current.runState).toBe("idle");
    });

    expect(streamPromptMock.mock.calls[0]?.[1]).toBe("first prompt");
    expect(streamPromptMock.mock.calls[1]?.[1]).toBe("second prompt");
    expect(
      result.current.messages
        .filter((message) => message.role === "user")
        .map((message) => message.content),
    ).toEqual(["first prompt", "second prompt"]);

    await act(async () => {
      if (firstPromptPromise === null) {
        throw new Error("First prompt promise should be initialized");
      }
      await firstPromptPromise;
    });
  });
});
