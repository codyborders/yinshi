import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { RuntimeTransport } from "./runtimeTransport";
import { loadSessionHistory } from "./sessionHistory";

const SESSION_ID = "a".repeat(32);

function transportWithGet(
  get: ReturnType<typeof vi.fn>,
  location: RuntimeTransport["runtime"]["location"] = "local",
  historyBundleSupported = true,
): RuntimeTransport {
  const runtime =
    location === "managed"
      ? { location, runnerPublicKey: "x", historyBundleSupported }
      : location === "byoc"
        ? { location, runnerId: "runner", runnerPublicKey: "x" }
        : { location };
  return { get, runtime } as unknown as RuntimeTransport;
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/gu, "-")
    .replace(/\//gu, "_")
    .replace(/=+$/u, "");
}

function historyCursor(createdAt: string, id: string): string {
  const timestamp = new TextEncoder().encode(createdAt);
  const raw = new Uint8Array(2 + timestamp.length + 16);
  raw[0] = 1;
  raw[1] = timestamp.length;
  raw.set(timestamp, 2);
  for (let index = 0; index < 16; index += 1) {
    raw[2 + timestamp.length + index] = Number.parseInt(
      id.slice(index * 2, index * 2 + 2),
      16,
    );
  }
  return base64Url(raw);
}

async function bundleEnvelope(
  messages: unknown[],
  options: {
    cursor?: string | null;
    nextCursor?: string | null;
    through?: string | null;
    snapshot?: number;
    snapshotCount?: number;
    snapshotTail?: string | null;
    activeRunId?: string | null;
  } = {},
): Promise<Record<string, unknown>> {
  const raw = new TextEncoder().encode(JSON.stringify(messages));
  const source = new ReadableStream<BufferSource>({
    start(controller) {
      const input = new ArrayBuffer(raw.byteLength);
      new Uint8Array(input).set(raw);
      controller.enqueue(input);
      controller.close();
    },
  });
  const compressed = new Uint8Array(
    await new Response(
      source.pipeThrough(new CompressionStream("gzip")),
    ).arrayBuffer(),
  );
  const lastMessage = messages[messages.length - 1];
  const inferredSnapshotTail =
    typeof lastMessage === "object" &&
    lastMessage !== null &&
    "created_at" in lastMessage &&
    typeof lastMessage.created_at === "string" &&
    "id" in lastMessage &&
    typeof lastMessage.id === "string"
      ? historyCursor(lastMessage.created_at, lastMessage.id)
      : null;
  return {
    version: 1,
    encoding: "gzip+base64url",
    raw_bytes: raw.length,
    message_count: messages.length,
    cursor: options.cursor ?? null,
    next_cursor: options.nextCursor ?? null,
    through: options.through ?? null,
    snapshot: options.snapshot ?? (messages.length > 0 ? 123 : 0),
    snapshot_count: options.snapshotCount ?? messages.length,
    snapshot_tail: options.snapshotTail ?? inferredSnapshotTail,
    active_run_id: options.activeRunId ?? null,
    data: base64Url(compressed),
  };
}

describe("loadSessionHistory", () => {
  it("loads one complete managed tool trace from one compressed bundle", async () => {
    const messageId = "1".repeat(32);
    const createdAt = "2026-08-23T00:00:00Z";
    const fullMessage = JSON.stringify({
      schema: "yinshi.assistant_turn.v1",
      events: [{ type: "tool_use", toolCallId: "tool-1", toolName: "read" }],
    });
    const messages = [
      {
        id: messageId,
        created_at: createdAt,
        session_id: SESSION_ID,
        role: "assistant",
        content: "done",
        full_message: fullMessage,
        turn_id: "turn-1",
        turn_status: "completed",
      },
    ];
    const get = vi.fn().mockResolvedValue(
      await bundleEnvelope(messages, {
        through: historyCursor(createdAt, messageId),
      }),
    );

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).resolves.toEqual(messages);
    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith(
      `/api/sessions/${SESSION_ID}/messages/bundle`,
    );
  });

  it("reports the bundled active run only after the complete snapshot succeeds", async () => {
    const activeRunId = "b".repeat(32);
    const onBundledActiveRun = vi.fn();
    const first = {
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:00Z",
      session_id: SESSION_ID,
      role: "user",
      content: "first",
      full_message: null,
      turn_id: activeRunId,
      turn_status: null,
    };
    const second = {
      ...first,
      id: "2".repeat(32),
      created_at: "2026-08-23T00:00:01Z",
      content: "second",
    };
    const cursor = historyCursor(first.created_at, first.id);
    const through = historyCursor(second.created_at, second.id);
    const get = vi
      .fn()
      .mockResolvedValueOnce(
        await bundleEnvelope([first], {
          activeRunId,
          nextCursor: cursor,
          through,
          snapshotCount: 2,
          snapshotTail: through,
        }),
      )
      .mockImplementationOnce(async () => {
        expect(onBundledActiveRun).not.toHaveBeenCalled();
        return bundleEnvelope([second], {
          activeRunId,
          cursor,
          through,
          snapshotCount: 2,
          snapshotTail: through,
        });
      });

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID, {
        onBundledActiveRun,
      }),
    ).resolves.toEqual([first, second]);
    expect(onBundledActiveRun).toHaveBeenCalledOnce();
    expect(onBundledActiveRun).toHaveBeenCalledWith(activeRunId);
    expect(get).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining(`active_run_id=${activeRunId}`),
    );
  });

  it("rejects a malformed bundled active run without a callback", async () => {
    const onBundledActiveRun = vi.fn();
    const envelope = await bundleEnvelope([]);
    envelope.active_run_id = "not-a-run-id";
    const get = vi.fn().mockResolvedValue(envelope);

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID, {
        onBundledActiveRun,
      }),
    ).rejects.toThrow("Invalid message history bundle");
    expect(onBundledActiveRun).not.toHaveBeenCalled();
  });

  it("reports a null bundled active run after a complete snapshot", async () => {
    const onBundledActiveRun = vi.fn();
    const get = vi.fn().mockResolvedValue(await bundleEnvelope([]));

    await loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID, {
      onBundledActiveRun,
    });

    expect(onBundledActiveRun).toHaveBeenCalledOnce();
    expect(onBundledActiveRun).toHaveBeenCalledWith(null);
  });

  it("rejects an active run change between bundle pages without a callback", async () => {
    const onBundledActiveRun = vi.fn();
    const first = {
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:00Z",
      session_id: SESSION_ID,
      role: "user",
      content: "first",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    const second = {
      ...first,
      id: "2".repeat(32),
      created_at: "2026-08-23T00:00:01Z",
    };
    const cursor = historyCursor(first.created_at, first.id);
    const through = historyCursor(second.created_at, second.id);
    const get = vi
      .fn()
      .mockResolvedValueOnce(
        await bundleEnvelope([first], {
          activeRunId: "b".repeat(32),
          nextCursor: cursor,
          through,
          snapshotCount: 2,
          snapshotTail: through,
        }),
      )
      .mockResolvedValueOnce(
        await bundleEnvelope([second], {
          activeRunId: "c".repeat(32),
          cursor,
          through,
          snapshotCount: 2,
          snapshotTail: through,
        }),
      );

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID, {
        onBundledActiveRun,
      }),
    ).rejects.toThrow("snapshot changed unexpectedly");
    expect(onBundledActiveRun).not.toHaveBeenCalled();
  });

  it("loads bundle pages through one stable snapshot cursor", async () => {
    const first = {
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:00Z",
      session_id: SESSION_ID,
      role: "user",
      content: "first",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    const second = {
      ...first,
      id: "2".repeat(32),
      created_at: "2026-08-23T00:00:01Z",
      content: "second",
    };
    const cursor = historyCursor(first.created_at, first.id);
    const through = historyCursor(second.created_at, second.id);
    const get = vi
      .fn()
      .mockResolvedValueOnce(
        await bundleEnvelope([first], {
          nextCursor: cursor,
          through,
          snapshotCount: 2,
          snapshotTail: through,
        }),
      )
      .mockResolvedValueOnce(
        await bundleEnvelope([second], {
          cursor,
          through,
          snapshotCount: 2,
          snapshotTail: through,
        }),
      );

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).resolves.toEqual([first, second]);
    expect(get).toHaveBeenNthCalledWith(
      2,
      `/api/sessions/${SESSION_ID}/messages/bundle?cursor=${encodeURIComponent(
        cursor,
      )}&through=${encodeURIComponent(
        through,
      )}&snapshot=123&snapshot_count=2&snapshot_tail=${encodeURIComponent(through)}&active_run_id=none`,
    );
  });

  it("rejects a changed snapshot watermark on continuation", async () => {
    const first = {
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:00Z",
      session_id: SESSION_ID,
      role: "user",
      content: "first",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    const second = {
      ...first,
      id: "2".repeat(32),
      created_at: "2026-08-23T00:00:01Z",
      content: "second",
    };
    const cursor = historyCursor(first.created_at, first.id);
    const through = historyCursor(second.created_at, second.id);
    const get = vi
      .fn()
      .mockResolvedValueOnce(
        await bundleEnvelope([first], {
          nextCursor: cursor,
          through,
          snapshot: 123,
          snapshotCount: 2,
          snapshotTail: through,
        }),
      )
      .mockResolvedValueOnce(
        await bundleEnvelope([second], {
          cursor,
          through,
          snapshot: 124,
          snapshotCount: 2,
          snapshotTail: through,
        }),
      );

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).rejects.toThrow("snapshot changed");
  });

  it("rejects a changed snapshot count on continuation", async () => {
    const first = {
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:00Z",
      session_id: SESSION_ID,
      role: "user",
      content: "first",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    const second = {
      ...first,
      id: "2".repeat(32),
      created_at: "2026-08-23T00:00:01Z",
      content: "second",
    };
    const cursor = historyCursor(first.created_at, first.id);
    const through = historyCursor(second.created_at, second.id);
    const get = vi
      .fn()
      .mockResolvedValueOnce(
        await bundleEnvelope([first], {
          nextCursor: cursor,
          through,
          snapshotCount: 2,
          snapshotTail: through,
        }),
      )
      .mockResolvedValueOnce(
        await bundleEnvelope([second], {
          cursor,
          through,
          snapshotCount: 3,
          snapshotTail: through,
        }),
      );

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).rejects.toThrow("snapshot changed");
  });

  it("rejects a changed snapshot tail on continuation", async () => {
    const first = {
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:00Z",
      session_id: SESSION_ID,
      role: "user",
      content: "first",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    const second = {
      ...first,
      id: "2".repeat(32),
      created_at: "2026-08-23T00:00:01Z",
      content: "second",
    };
    const cursor = historyCursor(first.created_at, first.id);
    const through = historyCursor(second.created_at, second.id);
    const get = vi
      .fn()
      .mockResolvedValueOnce(
        await bundleEnvelope([first], {
          nextCursor: cursor,
          through,
          snapshotCount: 2,
          snapshotTail: cursor,
        }),
      )
      .mockResolvedValueOnce(
        await bundleEnvelope([second], {
          cursor,
          through,
          snapshotCount: 2,
          snapshotTail: through,
        }),
      );

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).rejects.toThrow("snapshot changed");
  });

  it("rejects an empty continuation page", async () => {
    const message = {
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:00Z",
      session_id: SESSION_ID,
      role: "user",
      content: "first",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    const cursor = historyCursor(message.created_at, message.id);
    const through = historyCursor("2026-08-23T00:00:01Z", "2".repeat(32));
    const get = vi
      .fn()
      .mockResolvedValueOnce(
        await bundleEnvelope([message], {
          nextCursor: cursor,
          through,
          snapshotCount: 2,
          snapshotTail: cursor,
        }),
      )
      .mockResolvedValueOnce(
        await bundleEnvelope([], {
          cursor,
          through,
          snapshot: 123,
          snapshotCount: 2,
          snapshotTail: cursor,
        }),
      );

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).rejects.toThrow("Invalid message history bundle snapshot");
  });

  it("rejects a next cursor that skips the final decoded page message", async () => {
    const first = {
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:00Z",
      session_id: SESSION_ID,
      role: "user",
      content: "first",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    const second = {
      ...first,
      id: "2".repeat(32),
      created_at: "2026-08-23T00:00:01Z",
      content: "second",
    };
    const through = historyCursor(second.created_at, second.id);
    const get = vi.fn().mockResolvedValue(
      await bundleEnvelope([first, second], {
        nextCursor: historyCursor(first.created_at, first.id),
        through,
      }),
    );

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).rejects.toThrow("final page message");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("rejects a final through cursor that does not identify the final message", async () => {
    const message = {
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:00Z",
      session_id: SESSION_ID,
      role: "user",
      content: "only",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    const get = vi.fn().mockResolvedValue(
      await bundleEnvelope([message], {
        through: historyCursor("2026-08-23T00:00:01Z", "2".repeat(32)),
      }),
    );

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).rejects.toThrow("snapshot end");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("rejects a snapshot tail absent from final decoded history", async () => {
    const message = {
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:00Z",
      session_id: SESSION_ID,
      role: "user",
      content: "only",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    const get = vi.fn().mockResolvedValue(
      await bundleEnvelope([message], {
        through: historyCursor(message.created_at, message.id),
        snapshotTail: historyCursor("2026-08-23T00:00:01Z", "2".repeat(32)),
      }),
    );

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).rejects.toThrow("snapshot tail");
  });

  it.each([
    [new ApiError(413, "large", { code: "history_bundle_message_too_large" })],
  ])("falls back only for an oversized bundle response", async (error) => {
    const onBundledActiveRun = vi.fn();
    const get = vi
      .fn()
      .mockRejectedValueOnce(error)
      .mockResolvedValueOnce({ messages: [], next_cursor: null });

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID, {
        onBundledActiveRun,
      }),
    ).resolves.toEqual([]);
    expect(onBundledActiveRun).not.toHaveBeenCalled();
    expect(get).toHaveBeenNthCalledWith(
      2,
      `/api/sessions/${SESSION_ID}/messages/page`,
    );
  });

  it.each([
    [new ApiError(401, "Unauthorized")],
    [new ApiError(403, "Forbidden")],
    [
      new ApiError(409, "changed", {
        code: "history_bundle_snapshot_changed",
      }),
    ],
    [new ApiError(404, "Not Found")],
    [new Error("Runner RPC method or path is not allowed")],
    [new Error("Runner relay closed before responding")],
    [new Error("timed out")],
  ])(
    "does not hide bundle transport or authorization failures",
    async (error) => {
      const get = vi.fn().mockRejectedValue(error);

      await expect(
        loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
      ).rejects.toBe(error);
      expect(get).toHaveBeenCalledTimes(1);
    },
  );

  it("rejects malformed bundle counts without falling back", async () => {
    const message = {
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:00Z",
      session_id: SESSION_ID,
      role: "user",
      content: "x",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    const envelope = await bundleEnvelope([message], {
      through: historyCursor(message.created_at, message.id),
    });
    envelope.message_count = 2;
    envelope.snapshot_count = 2;
    const get = vi.fn().mockResolvedValue(envelope);

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).rejects.toThrow("count did not match");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("accepts the shared maximum safe snapshot numeric range", async () => {
    const message = {
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:00Z",
      session_id: SESSION_ID,
      role: "user",
      content: "safe",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    const envelope = await bundleEnvelope([message], {
      through: historyCursor(message.created_at, message.id),
      snapshot: Number.MAX_SAFE_INTEGER,
      snapshotCount: Number.MAX_SAFE_INTEGER,
    });
    const get = vi.fn().mockResolvedValue(envelope);

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).resolves.toEqual([message]);
  });

  it("rejects an unsafe bundle snapshot watermark", async () => {
    const envelope = await bundleEnvelope([]);
    envelope.snapshot = Number.MAX_SAFE_INTEGER + 1;
    const get = vi.fn().mockResolvedValue(envelope);

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).rejects.toThrow("Invalid message history bundle");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("rejects an unsafe bundle snapshot count", async () => {
    const envelope = await bundleEnvelope([]);
    envelope.snapshot_count = Number.MAX_SAFE_INTEGER + 1;
    const get = vi.fn().mockResolvedValue(envelope);

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).rejects.toThrow("Invalid message history bundle");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("rejects a nonzero snapshot for an empty first history", async () => {
    const envelope = await bundleEnvelope([], { snapshot: 1 });
    const get = vi.fn().mockResolvedValue(envelope);

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).rejects.toThrow("Invalid message history bundle snapshot");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("aborts bundle decompression above four MiB", async () => {
    const oversized = "x".repeat(4 * 1_024 * 1_024 + 1);
    const envelope = await bundleEnvelope([oversized]);
    envelope.raw_bytes = 4 * 1_024 * 1_024;
    envelope.message_count = 1;
    envelope.through = historyCursor("2026-08-23T00:00:00Z", "1".repeat(32));
    envelope.snapshot_tail = envelope.through;
    const get = vi.fn().mockResolvedValue(envelope);

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).rejects.toThrow("decoded size limit");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("falls back when browser gzip streaming is unavailable", async () => {
    const get = vi.fn().mockResolvedValue({ messages: [], next_cursor: null });
    vi.stubGlobal("DecompressionStream", undefined);
    try {
      await expect(
        loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
      ).resolves.toEqual([]);
    } finally {
      vi.unstubAllGlobals();
    }
    expect(get).toHaveBeenCalledWith(
      `/api/sessions/${SESSION_ID}/messages/page`,
    );
  });

  it("stops a managed bundle after navigation cancellation", async () => {
    let release: ((value: unknown) => void) | undefined;
    let cancelled = false;
    const message = {
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:00Z",
      session_id: SESSION_ID,
      role: "user",
      content: "x",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    const envelope = await bundleEnvelope([message], {
      through: historyCursor(message.created_at, message.id),
    });
    const get = vi.fn().mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    const loading = loadSessionHistory(
      transportWithGet(get, "managed"),
      SESSION_ID,
      { isCancelled: () => cancelled },
    );
    await Promise.resolve();
    cancelled = true;
    release?.(envelope);

    await expect(loading).rejects.toThrow("cancelled");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("rejects bundle messages outside canonical history order", async () => {
    const later = {
      id: "2".repeat(32),
      created_at: "2026-08-23T00:00:01.000002Z",
      session_id: SESSION_ID,
      role: "user",
      content: "later",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    const earlier = {
      ...later,
      id: "1".repeat(32),
      created_at: "2026-08-23T00:00:01.000001Z",
      content: "earlier",
    };
    const get = vi.fn().mockResolvedValue(
      await bundleEnvelope([later, earlier], {
        through: historyCursor(later.created_at, later.id),
      }),
    );

    await expect(
      loadSessionHistory(transportWithGet(get, "managed"), SESSION_ID),
    ).rejects.toThrow("order is invalid");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("uses legacy loading when managed feature advertisement is absent", async () => {
    const get = vi.fn().mockResolvedValue({ messages: [], next_cursor: null });
    const transport = {
      get,
      runtime: { location: "managed", runnerPublicKey: "x" },
    } as unknown as RuntimeTransport;

    await expect(loadSessionHistory(transport, SESSION_ID)).resolves.toEqual(
      [],
    );
    expect(get).toHaveBeenCalledWith(
      `/api/sessions/${SESSION_ID}/messages/page`,
    );
  });

  it("does not try bundles outside managed runtimes", async () => {
    for (const location of ["local", "hosted", "byoc"] as const) {
      const get = vi
        .fn()
        .mockResolvedValue({ messages: [], next_cursor: null });
      await expect(
        loadSessionHistory(transportWithGet(get, location), SESSION_ID),
      ).resolves.toEqual([]);
      expect(get).toHaveBeenCalledWith(
        `/api/sessions/${SESSION_ID}/messages/page`,
      );
    }
  });

  it("hydrates no more than eight independent fields before starting a ninth", async () => {
    const messageIds = Array.from({ length: 9 }, (_, index) =>
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

    expect(started).toHaveLength(8);
    expect(new Set(started).size).toBe(8);

    releases.get(started[0])?.();
    await Promise.resolve();
    await Promise.resolve();
    expect(started).toHaveLength(9);

    for (const release of releases.values()) release();
    await expect(loading).resolves.toHaveLength(9);
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
    const messageIds = Array.from({ length: 9 }, (_, index) =>
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
    expect(started).toHaveLength(8);

    cancelled = true;
    for (const messageId of started.slice(0, 7)) releases.get(messageId)?.();
    await Promise.resolve();
    await Promise.resolve();
    expect(settled).toBe(false);
    expect(started).toHaveLength(8);

    releases.get(started[7])?.();
    await expect(loading).rejects.toThrow("cancelled");
    expect(started).toHaveLength(8);
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
