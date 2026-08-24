import { describe, expect, it, vi } from "vitest";
import type { RuntimeTransport } from "./runtimeTransport";
import { loadSessionHistory } from "./sessionHistory";

const SESSION_ID = "a".repeat(32);

function transportWithGet(get: ReturnType<typeof vi.fn>): RuntimeTransport {
  return { get } as unknown as RuntimeTransport;
}

describe("loadSessionHistory", () => {
  it("preserves pagination, Unicode, null fields, and empty fields", async () => {
    const firstId = "1".repeat(32);
    const secondId = "2".repeat(32);
    const get = vi.fn(async (path: string) => {
      if (path.endsWith("/messages/page")) {
        return {
          messages: [
            {
              id: firstId,
              created_at: "2026-08-23T00:00:00Z",
              session_id: SESSION_ID,
              role: "user",
              content_length: null,
              full_message_length: 0,
              turn_id: null,
              turn_status: null,
            },
          ],
          next_cursor: "cursor-1",
        };
      }
      if (path.includes("cursor=cursor-1")) {
        return {
          messages: [
            {
              id: secondId,
              created_at: "2026-08-23T00:00:01Z",
              session_id: SESSION_ID,
              role: "assistant",
              content_length: 4,
              full_message_length: null,
              turn_id: "turn-2",
              turn_status: "completed",
            },
          ],
          next_cursor: null,
        };
      }
      if (path.includes(`messages/${secondId}/field`) && path.endsWith("offset=0")) {
        return { value: "A界", offset: 0, next_offset: 2 };
      }
      if (path.includes(`messages/${secondId}/field`) && path.endsWith("offset=2")) {
        return { value: "é", offset: 2, next_offset: null };
      }
      throw new Error(`Unexpected request: ${path}`);
    });

    await expect(loadSessionHistory(transportWithGet(get), SESSION_ID)).resolves.toEqual([
      {
        id: firstId,
        created_at: "2026-08-23T00:00:00Z",
        session_id: SESSION_ID,
        role: "user",
        content: null,
        full_message: "",
        turn_id: null,
        turn_status: null,
      },
      {
        id: secondId,
        created_at: "2026-08-23T00:00:01Z",
        session_id: SESSION_ID,
        role: "assistant",
        content: "A界é",
        full_message: null,
        turn_id: "turn-2",
        turn_status: "completed",
      },
    ]);
  });

  it("rejects a repeated metadata cursor", async () => {
    const get = vi
      .fn()
      .mockResolvedValueOnce({
        messages: [
          {
            id: "1".repeat(32),
            created_at: "2026-08-23T00:00:00Z",
            session_id: SESSION_ID,
            role: "user",
            content_length: null,
            full_message_length: null,
            turn_id: null,
            turn_status: null,
          },
        ],
        next_cursor: "repeat",
      })
      .mockResolvedValueOnce({
        messages: [
          {
            id: "2".repeat(32),
            created_at: "2026-08-23T00:00:01Z",
            session_id: SESSION_ID,
            role: "user",
            content_length: null,
            full_message_length: null,
            turn_id: null,
            turn_status: null,
          },
        ],
        next_cursor: "repeat",
      });

    await expect(
      loadSessionHistory(transportWithGet(get), SESSION_ID),
    ).rejects.toThrow("cursor did not advance");
  });

  it.each([
    [
      "a repeated next offset",
      { value: "x", offset: 0, next_offset: 0 },
      "invalid next offset",
    ],
    [
      "a decreasing response offset",
      { value: "x", offset: 1, next_offset: 2 },
      "offset changed unexpectedly",
    ],
    [
      "a chunk beyond the advertised length",
      { value: "xyz", offset: 0, next_offset: null },
      "did not advance",
    ],
    [
      "early field termination",
      { value: "x", offset: 0, next_offset: null },
      "ended before",
    ],
  ])("rejects %s", async (_name, fieldResponse, error) => {
    const messageId = "3".repeat(32);
    const get = vi
      .fn()
      .mockResolvedValueOnce({
        messages: [
          {
            id: messageId,
            created_at: "2026-08-23T00:00:00Z",
            session_id: SESSION_ID,
            role: "user",
            content_length: 2,
            full_message_length: null,
            turn_id: null,
            turn_status: null,
          },
        ],
        next_cursor: null,
      })
      .mockResolvedValueOnce(fieldResponse);

    await expect(
      loadSessionHistory(transportWithGet(get), SESSION_ID),
    ).rejects.toThrow(error);
  });

  it("stops requesting chunks after session navigation cancels loading", async () => {
    const messageId = "4".repeat(32);
    let cancelled = false;
    const get = vi
      .fn()
      .mockResolvedValueOnce({
        messages: [
          {
            id: messageId,
            created_at: "2026-08-23T00:00:00Z",
            session_id: SESSION_ID,
            role: "user",
            content_length: 40_000,
            full_message_length: null,
            turn_id: null,
            turn_status: null,
          },
        ],
        next_cursor: null,
      })
      .mockImplementationOnce(async () => {
        cancelled = true;
        return { value: "x".repeat(32_768), offset: 0, next_offset: 32_768 };
      });

    await expect(
      loadSessionHistory(transportWithGet(get), SESSION_ID, {
        isCancelled: () => cancelled,
      }),
    ).rejects.toThrow("cancelled");
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("rejects excessive metadata page requests", async () => {
    let requestIndex = 0;
    const get = vi.fn(async () => {
      requestIndex += 1;
      const indexText = requestIndex.toString().padStart(12, "0");
      return {
        messages: [
          {
            id: requestIndex.toString(16).padStart(32, "0"),
            created_at: `2026-08-23T${indexText}Z`,
            session_id: SESSION_ID,
            role: "user",
            content_length: null,
            full_message_length: null,
            turn_id: null,
            turn_status: null,
          },
        ],
        next_cursor: `cursor-${requestIndex}`,
      };
    });

    await expect(
      loadSessionHistory(transportWithGet(get), SESSION_ID),
    ).rejects.toThrow("too many page requests");
    expect(get).toHaveBeenCalledTimes(10_000);
  });
});
