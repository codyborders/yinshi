import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { cancelSessionMock, streamPromptMock } = vi.hoisted(() => ({
  cancelSessionMock: vi.fn(),
  streamPromptMock: vi.fn(),
}));

vi.mock("../../api/client", () => ({
  cancelSession: cancelSessionMock,
  streamPrompt: streamPromptMock,
}));

import { useAgentStream } from "../useAgentStream";

function createDeferredPromise(): { promise: Promise<void>; resolve: () => void } {
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
      result.current.messages.filter((message) => message.role === "user").map((message) => message.content),
    ).toEqual(["first prompt", "second prompt"]);

    await act(async () => {
      if (firstPromptPromise === null) {
        throw new Error("First prompt promise should be initialized");
      }
      await firstPromptPromise;
    });
  });
});
