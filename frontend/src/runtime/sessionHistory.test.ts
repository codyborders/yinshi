import { describe, expect, it, vi } from "vitest";
import type { RuntimeTransport } from "./runtimeTransport";
import { loadSessionHistory } from "./sessionHistory";

const SESSION_ID = "a".repeat(32);

function transportWithGet(get: ReturnType<typeof vi.fn>): RuntimeTransport {
  return { get } as unknown as RuntimeTransport;
}

describe("loadSessionHistory", () => {
  it("hydrates no more than four independent fields before starting a fifth", async () => {
    const messageIds = Array.from({ length: 5 }, (_, index) =>
      (index + 1).toString(16).padStart(32, "0"),
    );
    const started: string[] = [];
    const releases = new Map<string, () => void>();
    const get = vi.fn((path: string) => {
      if (path.endsWith("/messages/page")) {
        return Promise.resolve({
          messages: messageIds.map((id, index) => ({
            id,
            created_at: `2026-08-23T00:00:0${index}Z`,
            session_id: SESSION_ID,
            role: "user",
            content_length: 1,
            full_message_length: null,
            turn_id: null,
            turn_status: null,
          })),
          next_cursor: null,
        });
      }
      const messageId = messageIds.find((id) => path.includes(id));
      if (!messageId) throw new Error(`Unexpected request: ${path}`);
      started.push(messageId);
      return new Promise((resolve) => {
        releases.set(messageId, () =>
          resolve({ value: "x", offset: 0, next_offset: null }),
        );
      });
    });

    const loading = loadSessionHistory(transportWithGet(get), SESSION_ID);
    await Promise.resolve();
    await Promise.resolve();

    expect(started).toHaveLength(4);
    expect(new Set(started).size).toBe(4);

    releases.get(started[0])?.();
    await Promise.resolve();
    await Promise.resolve();
    expect(started).toHaveLength(5);

    for (const release of releases.values()) release();
    await expect(loading).resolves.toHaveLength(5);
  });

  it("preserves metadata order when independent fields resolve out of order", async () => {
    const messageIds = ["1".repeat(32), "2".repeat(32), "3".repeat(32)];
    const releases = new Map<string, () => void>();
    const get = vi.fn((path: string) => {
      if (path.endsWith("/messages/page")) {
        return Promise.resolve({
          messages: messageIds.map((id, index) => ({
            id,
            created_at: `2026-08-23T00:00:0${index}Z`,
            session_id: SESSION_ID,
            role: "user",
            content_length: 1,
            full_message_length: null,
            turn_id: null,
            turn_status: null,
          })),
          next_cursor: null,
        });
      }
      const messageId = messageIds.find((id) => path.includes(id));
      if (!messageId) throw new Error(`Unexpected request: ${path}`);
      return new Promise((resolve) => {
        releases.set(messageId, () =>
          resolve({ value: messageId[0], offset: 0, next_offset: null }),
        );
      });
    });

    const loading = loadSessionHistory(transportWithGet(get), SESSION_ID);
    await Promise.resolve();
    await Promise.resolve();
    releases.get(messageIds[2])?.();
    releases.get(messageIds[0])?.();
    releases.get(messageIds[1])?.();

    const messages = await loading;
    expect(messages.map((message) => message.id)).toEqual(messageIds);
    expect(messages.map((message) => message.content)).toEqual(["1", "2", "3"]);
  });

  it("keeps chunks within one field sequential", async () => {
    const messageId = "6".repeat(32);
    let releaseFirstChunk: (() => void) | undefined;
    const fieldRequests: string[] = [];
    const get = vi.fn((path: string) => {
      if (path.endsWith("/messages/page")) {
        return Promise.resolve({
          messages: [
            {
              id: messageId,
              created_at: "2026-08-23T00:00:00Z",
              session_id: SESSION_ID,
              role: "assistant",
              content_length: 2,
              full_message_length: null,
              turn_id: null,
              turn_status: null,
            },
          ],
          next_cursor: null,
        });
      }
      fieldRequests.push(path);
      if (path.endsWith("offset=0")) {
        return new Promise((resolve) => {
          releaseFirstChunk = () =>
            resolve({ value: "a", offset: 0, next_offset: 1 });
        });
      }
      return Promise.resolve({ value: "b", offset: 1, next_offset: null });
    });

    const loading = loadSessionHistory(transportWithGet(get), SESSION_ID);
    await Promise.resolve();
    await Promise.resolve();
    expect(fieldRequests).toHaveLength(1);

    releaseFirstChunk?.();
    await expect(loading).resolves.toMatchObject([{ content: "ab" }]);
    expect(fieldRequests).toHaveLength(2);
    expect(fieldRequests[1]).toContain("offset=1");
  });

  it("stops scheduling fields after cancellation and drains started workers", async () => {
    const messageIds = Array.from({ length: 5 }, (_, index) =>
      (index + 8).toString(16).padStart(32, "0"),
    );
    const releases = new Map<string, () => void>();
    const started: string[] = [];
    let cancelled = false;
    let settled = false;
    const get = vi.fn((path: string) => {
      if (path.endsWith("/messages/page")) {
        return Promise.resolve({
          messages: messageIds.map((id, index) => ({
            id,
            created_at: `2026-08-23T00:00:0${index}Z`,
            session_id: SESSION_ID,
            role: "user",
            content_length: 1,
            full_message_length: null,
            turn_id: null,
            turn_status: null,
          })),
          next_cursor: null,
        });
      }
      const messageId = messageIds.find((id) => path.includes(id));
      if (!messageId) throw new Error(`Unexpected request: ${path}`);
      started.push(messageId);
      return new Promise((resolve) => {
        releases.set(messageId, () =>
          resolve({ value: "x", offset: 0, next_offset: null }),
        );
      });
    });

    const loading = loadSessionHistory(transportWithGet(get), SESSION_ID, {
      isCancelled: () => cancelled,
    }).finally(() => {
      settled = true;
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(started).toHaveLength(4);

    cancelled = true;
    for (const messageId of started.slice(0, 3)) releases.get(messageId)?.();
    await Promise.resolve();
    await Promise.resolve();
    expect(settled).toBe(false);
    expect(started).toHaveLength(4);

    releases.get(started[3])?.();
    await expect(loading).rejects.toThrow("cancelled");
    expect(started).toHaveLength(4);
  });

  it("rejects with the first observed worker failure after draining started workers", async () => {
    const messageIds = ["c".repeat(32), "d".repeat(32)];
    const rejectFields = new Map<string, (error: Error) => void>();
    const started: string[] = [];
    let settled = false;
    const get = vi.fn((path: string) => {
      if (path.endsWith("/messages/page")) {
        return Promise.resolve({
          messages: messageIds.map((id, index) => ({
            id,
            created_at: `2026-08-23T00:00:0${index}Z`,
            session_id: SESSION_ID,
            role: "user",
            content_length: 1,
            full_message_length: null,
            turn_id: null,
            turn_status: null,
          })),
          next_cursor: null,
        });
      }
      const messageId = messageIds.find((id) => path.includes(id));
      if (!messageId) throw new Error(`Unexpected request: ${path}`);
      started.push(messageId);
      return new Promise((_resolve, reject) => {
        rejectFields.set(messageId, reject);
      });
    });

    const loading = loadSessionHistory(
      transportWithGet(get),
      SESSION_ID,
    ).finally(() => {
      settled = true;
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(started).toEqual(messageIds);

    rejectFields.get(messageIds[1])?.(new Error("worker 1 failed first"));
    await Promise.resolve();
    await Promise.resolve();
    expect(settled).toBe(false);

    rejectFields.get(messageIds[0])?.(new Error("worker 0 failed later"));
    await expect(loading).rejects.toThrow("worker 1 failed first");
  });

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
      if (
        path.includes(`messages/${secondId}/field`) &&
        path.endsWith("offset=0")
      ) {
        return { value: "A界", offset: 0, next_offset: 2 };
      }
      if (
        path.includes(`messages/${secondId}/field`) &&
        path.endsWith("offset=2")
      ) {
        return { value: "é", offset: 2, next_offset: null };
      }
      throw new Error(`Unexpected request: ${path}`);
    });

    await expect(
      loadSessionHistory(transportWithGet(get), SESSION_ID),
    ).resolves.toEqual([
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
