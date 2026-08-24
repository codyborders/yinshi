import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiGetMock = vi.fn();
const apiPatchMock = vi.fn();
const cancelMock = vi.fn();
const sendPromptMock = vi.fn();
const setMessagesMock = vi.fn();
const bootstrapSessionMock = vi.fn();
const useCatalogMock = vi.fn();
const useAgentStreamMock = vi.fn();
const historyCacheClient = {
  get: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
};
let runtimeResourceOverride: Record<string, unknown> | null = null;
let historyCacheAvailable = true;

const minimaxProvider = {
  id: "minimax",
  label: "MiniMax",
  auth_strategies: ["api_key"],
  setup_fields: [],
  docs_url: "https://example.com/minimax",
  connected: true,
  model_count: 1,
};

const openaiProvider = {
  id: "openai",
  label: "OpenAI",
  auth_strategies: ["api_key"],
  setup_fields: [],
  docs_url: "https://example.com/openai",
  connected: true,
  model_count: 1,
};

const minimaxModel = {
  ref: "minimax/MiniMax-M2.7",
  provider: "minimax",
  id: "MiniMax-M2.7",
  label: "MiniMax M2.7",
  api: "responses",
  reasoning: true,
  thinking_levels: ["off", "minimal", "low", "medium", "high"],
  inputs: ["text"],
  context_window: 1000,
  max_tokens: 1000,
};

const openaiModel = {
  ref: "openai/gpt-4.1",
  provider: "openai",
  id: "gpt-4.1",
  label: "GPT-4.1",
  api: "responses",
  reasoning: false,
  thinking_levels: ["off"],
  inputs: ["text"],
  context_window: 1000,
  max_tokens: 1000,
};

vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      get: (...args: unknown[]) => apiGetMock(...args),
      patch: (...args: unknown[]) => apiPatchMock(...args),
    },
  };
});

vi.mock("../../components/ChatView", () => ({
  default: ({
    onSend,
    inputDisabledReason,
  }: {
    onSend: (prompt: string) => void | Promise<void>;
    inputDisabledReason?: string | null;
  }) => (
    <div>
      {inputDisabledReason && <div>{inputDisabledReason}</div>}
      <button
        type="button"
        disabled={Boolean(inputDisabledReason)}
        onClick={() => {
          void onSend("Ship it");
        }}
      >
        Send Prompt
      </button>
    </div>
  ),
}));

vi.mock("../../hooks/useAgentStream", () => ({
  useAgentStream: (...args: unknown[]) => useAgentStreamMock(...args),
}));

vi.mock("../../hooks/useAuth", () => ({
  useAuth: () => ({
    status: "authenticated",
    email: "u@t.com",
    userId: "user-1",
    logout: vi.fn(),
  }),
}));

vi.mock("../../hooks/useCatalog", () => ({
  useCatalog: () => useCatalogMock(),
}));

vi.mock("../../hooks/usePiCommands", () => ({
  usePiCommands: () => [],
}));

vi.mock("../../runtime/sessionHistoryCacheClient", () => ({
  getSessionHistoryCacheClient: () => historyCacheClient,
  isSessionHistoryCacheAvailable: () => historyCacheAvailable,
  invalidateSessionHistoryCache: (userId: string, sessionId: string) => {
    void historyCacheClient.delete(userId, sessionId);
  },
}));

vi.mock("../../runtime/useRuntimeResource", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../runtime/useRuntimeResource")>();
  return {
    ...actual,
    useRuntimeResource: (
      ...args: Parameters<typeof actual.useRuntimeResource>
    ) => runtimeResourceOverride ?? actual.useRuntimeResource(...args),
  };
});

import { ApiError } from "../../api/client";
import Session from "../Session";

function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  let reject: (reason?: unknown) => void = () => {};
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/gu, "-")
    .replace(/\//gu, "_")
    .replace(/=+$/u, "");
}

async function historyBundle(
  messages: Record<string, unknown>[],
  activeRunId: string | null,
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
  const finalMessage = messages[messages.length - 1];
  const cursor =
    finalMessage === undefined
      ? null
      : historyCursor(
          finalMessage.created_at as string,
          finalMessage.id as string,
        );
  return {
    version: 1,
    encoding: "gzip+base64url",
    raw_bytes: raw.length,
    message_count: messages.length,
    cursor: null,
    next_cursor: null,
    through: cursor,
    snapshot: messages.length,
    snapshot_count: messages.length,
    snapshot_tail: cursor,
    active_run_id: activeRunId,
    data: base64Url(compressed),
  };
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

function managedRuntimeResource(transportGet: ReturnType<typeof vi.fn>) {
  const runtime = {
    location: "managed" as const,
    runnerPublicKey: "rFvhxP4Hj7ENRRoyt2-DhkltHdiuShm7vob8n0NhzUc",
    historyBundleSupported: true,
  };
  return {
    resource: {
      resourceId: TEST_SESSION_ID,
      runtime,
      transport: {
        runtime,
        get: transportGet,
        post: vi.fn(),
        patch: vi.fn(),
        put: vi.fn(),
        delete: vi.fn(),
        upload: vi.fn(),
        close: vi.fn(),
      },
    },
    loading: false,
    error: null,
  };
}

function mockCatalog({
  defaultModel = minimaxModel.ref,
  providers = [minimaxProvider],
  models = [minimaxModel],
}: {
  defaultModel?: string;
  providers?: (typeof minimaxProvider)[];
  models?: (typeof minimaxModel)[];
} = {}) {
  useCatalogMock.mockReturnValue({
    catalog: {
      default_model: defaultModel,
      providers,
      models,
    },
    loading: false,
  });
}

const TEST_SESSION_ID = "a".repeat(32);

function sessionMetadata(overrides: Record<string, unknown> = {}) {
  return {
    id: TEST_SESSION_ID,
    created_at: "2026-04-26T00:00:00Z",
    updated_at: "2026-04-26T00:00:00Z",
    workspace_id: "workspace-123",
    status: "idle",
    model: minimaxModel.ref,
    pi_context_version: 1,
    ...overrides,
  };
}

function mockSessionApi(
  sessionMetadataValue:
    | Record<string, unknown>
    | Promise<Record<string, unknown>> = sessionMetadata(),
  messages: unknown[] = [],
  activeRun: unknown = null,
) {
  const metadata =
    sessionMetadataValue instanceof Promise
      ? sessionMetadataValue
      : sessionMetadata(sessionMetadataValue);
  apiGetMock.mockImplementation((path: string) => {
    if (path === "/api/runtime") {
      return Promise.resolve({
        provider: "local",
        status: "ready",
        artifact_version: null,
        last_error: null,
        runner_public_key: null,
      });
    }
    const pagePath = `/api/sessions/${TEST_SESSION_ID}/messages/page`;
    if (path === pagePath) {
      return Promise.resolve({
        messages: messages.map((message) => {
          const stored = message as Record<string, unknown>;
          const content = stored.content as string | null;
          const fullMessage = stored.full_message as string | null;
          return {
            id: stored.id,
            created_at: stored.created_at,
            session_id: stored.session_id,
            role: stored.role,
            content_length: content == null ? null : Array.from(content).length,
            full_message_length:
              fullMessage == null ? null : Array.from(fullMessage).length,
            turn_id: stored.turn_id ?? null,
            turn_status: stored.turn_status ?? null,
          };
        }),
        next_cursor: null,
      });
    }
    if (path.startsWith(`/api/sessions/${TEST_SESSION_ID}/messages/`)) {
      const url = new URL(path, "https://yinshi.test");
      const parts = url.pathname.split("/");
      const messageId = parts[parts.length - 2];
      const fieldName = url.searchParams.get("name");
      const offset = Number(url.searchParams.get("offset"));
      const stored = messages.find(
        (message) => (message as Record<string, unknown>).id === messageId,
      ) as Record<string, unknown> | undefined;
      if (
        !stored ||
        (fieldName !== "content" && fieldName !== "full_message")
      ) {
        return Promise.reject(new Error("Unexpected history field request"));
      }
      const fieldValue = stored[fieldName] as string;
      const characters = Array.from(fieldValue);
      const value = characters
        .filter(
          (_character, index) => index >= offset && index < offset + 32_768,
        )
        .join("");
      const nextOffset = offset + Array.from(value).length;
      return Promise.resolve({
        value,
        offset,
        next_offset: nextOffset < characters.length ? nextOffset : null,
      });
    }
    if (path === `/api/sessions/${TEST_SESSION_ID}/runs/active`) {
      return Promise.resolve(activeRun);
    }
    if (path === `/api/sessions/${TEST_SESSION_ID}`) {
      return Promise.resolve(metadata);
    }
    throw new Error(`Unexpected GET path: ${path}`);
  });
}

function renderSession() {
  return render(
    <MemoryRouter initialEntries={[`/app/sessions/${TEST_SESSION_ID}`]}>
      <Routes>
        <Route path="/app/sessions/:id" element={<Session />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Session", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    runtimeResourceOverride = null;
    historyCacheAvailable = true;
    historyCacheClient.get.mockResolvedValue(null);
    historyCacheClient.put.mockResolvedValue(undefined);
    historyCacheClient.delete.mockResolvedValue(undefined);
    localStorage.clear();
    sendPromptMock.mockResolvedValue(undefined);
    cancelMock.mockResolvedValue(undefined);
    bootstrapSessionMock.mockImplementation((history) => {
      setMessagesMock(history);
    });
    useAgentStreamMock.mockReturnValue({
      messages: [],
      sendPrompt: sendPromptMock,
      cancel: cancelMock,
      streaming: false,
      setMessages: setMessagesMock,
      bootstrapSession: bootstrapSessionMock,
    });
    apiPatchMock.mockResolvedValue({ model: minimaxModel.ref });
  });

  it("does not override the persisted model or thinking settings before the user changes them", async () => {
    const pendingSessionPromise = new Promise<{ model: string }>(() => {});
    mockCatalog();
    mockSessionApi(pendingSessionPromise);

    renderSession();

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Send Prompt" }),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Send Prompt" }));

    await waitFor(() => {
      expect(sendPromptMock).toHaveBeenCalledWith(
        "Ship it",
        undefined,
        undefined,
      );
    });
  });

  it("uses a newly selected model for prompts while the save is still pending", async () => {
    let resolvePatch: (value: { model: string }) => void = () => {};
    apiPatchMock.mockReturnValue(
      new Promise<{ model: string }>((resolve) => {
        resolvePatch = resolve;
      }),
    );
    mockCatalog({
      providers: [minimaxProvider, openaiProvider],
      models: [minimaxModel, openaiModel],
    });
    mockSessionApi({ model: minimaxModel.ref });

    renderSession();

    const modelSelect = await screen.findByLabelText("Model");
    fireEvent.change(modelSelect, { target: { value: openaiModel.ref } });
    fireEvent.click(screen.getByRole("button", { name: "Send Prompt" }));

    await waitFor(() => {
      expect(sendPromptMock).toHaveBeenCalledWith(
        "Ship it",
        openaiModel.ref,
        undefined,
      );
    });

    await act(async () => {
      resolvePatch({ model: openaiModel.ref });
    });
  });

  it("initializes a missing preference from the current persisted session", async () => {
    mockCatalog({
      providers: [minimaxProvider, openaiProvider],
      models: [minimaxModel, openaiModel],
    });
    mockSessionApi({ model: openaiModel.ref });

    renderSession();

    await screen.findByLabelText("Model");
    expect(localStorage.getItem("yinshi:last-session-model:user-1")).toBe(
      openaiModel.ref,
    );
  });

  it("does not replace an existing preference when another session loads", async () => {
    localStorage.setItem("yinshi:last-session-model:user-1", minimaxModel.ref);
    mockCatalog({
      providers: [minimaxProvider, openaiProvider],
      models: [minimaxModel, openaiModel],
    });
    mockSessionApi({ model: openaiModel.ref });

    renderSession();

    await screen.findByLabelText("Model");
    expect(localStorage.getItem("yinshi:last-session-model:user-1")).toBe(
      minimaxModel.ref,
    );
  });

  it("remembers a successfully selected model for the authenticated user", async () => {
    apiPatchMock.mockResolvedValue({ model: openaiModel.ref });
    mockCatalog({
      providers: [minimaxProvider, openaiProvider],
      models: [minimaxModel, openaiModel],
    });
    mockSessionApi({ model: minimaxModel.ref });

    renderSession();

    fireEvent.change(await screen.findByLabelText("Model"), {
      target: { value: openaiModel.ref },
    });

    await waitFor(() => {
      expect(localStorage.getItem("yinshi:last-session-model:user-1")).toBe(
        openaiModel.ref,
      );
    });
  });

  it("preserves the remembered model when a later session update fails", async () => {
    localStorage.setItem("yinshi:last-session-model:user-1", minimaxModel.ref);
    apiPatchMock.mockRejectedValue(new Error("save failed"));
    mockCatalog({
      providers: [minimaxProvider, openaiProvider],
      models: [minimaxModel, openaiModel],
    });
    mockSessionApi({ model: minimaxModel.ref });

    renderSession();

    const modelSelect = await screen.findByLabelText("Model");
    setMessagesMock.mockClear();
    fireEvent.change(modelSelect, {
      target: { value: openaiModel.ref },
    });

    await waitFor(() => expect(apiPatchMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(setMessagesMock).toHaveBeenCalledTimes(1));
    expect(localStorage.getItem("yinshi:last-session-model:user-1")).toBe(
      minimaxModel.ref,
    );
  });

  it("disables prompting for legacy transcript-only sessions", async () => {
    mockCatalog();
    useAgentStreamMock.mockReturnValue({
      messages: [
        {
          id: "message-1",
          role: "user",
          content: "Old prompt",
          blocks: [],
          timestamp: Date.now(),
        },
      ],
      sendPrompt: sendPromptMock,
      cancel: cancelMock,
      streaming: false,
      setMessages: setMessagesMock,
      bootstrapSession: bootstrapSessionMock,
    });
    mockSessionApi(sessionMetadata({ pi_context_version: 0 }));

    renderSession();

    const sendButton = await screen.findByRole("button", {
      name: "Send Prompt",
    });
    expect(sendButton).toBeDisabled();
    expect(screen.getByText(/predates durable Pi context/)).toBeInTheDocument();
  });

  it("omits the thinking override for models that do not support reasoning", async () => {
    mockCatalog({
      defaultModel: openaiModel.ref,
      providers: [openaiProvider],
      models: [openaiModel],
    });
    mockSessionApi({ model: openaiModel.ref });

    renderSession();

    const thinkingSelect = await screen.findByLabelText("Thinking");

    await waitFor(() => {
      expect(thinkingSelect).toBeDisabled();
      expect(thinkingSelect).toHaveAttribute(
        "title",
        "This model does not support thinking",
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "Send Prompt" }));

    await waitFor(() => {
      expect(sendPromptMock).toHaveBeenCalledWith(
        "Ship it",
        undefined,
        undefined,
      );
    });
  });

  it("forwards an explicit thinking level for reasoning models", async () => {
    mockCatalog();
    mockSessionApi({ model: minimaxModel.ref });

    renderSession();

    const thinkingSelect = await screen.findByLabelText("Thinking");
    fireEvent.change(thinkingSelect, { target: { value: "high" } });
    fireEvent.click(screen.getByRole("button", { name: "Send Prompt" }));

    await waitFor(() => {
      expect(sendPromptMock).toHaveBeenCalledWith("Ship it", undefined, "high");
    });
  });

  it("shows every thinking level advertised by the selected model", async () => {
    mockCatalog({
      models: [
        {
          ...minimaxModel,
          thinking_levels: ["off", "minimal", "low", "medium", "high", "xhigh"],
        },
      ],
    });
    mockSessionApi({ model: minimaxModel.ref });

    renderSession();

    const thinkingSelect = await screen.findByLabelText("Thinking");

    expect(thinkingSelect).toHaveTextContent("Model default");
    expect(thinkingSelect).toHaveTextContent("Off");
    expect(thinkingSelect).toHaveTextContent("Minimal");
    expect(thinkingSelect).toHaveTextContent("Low");
    expect(thinkingSelect).toHaveTextContent("Medium");
    expect(thinkingSelect).toHaveTextContent("High");
    expect(thinkingSelect).toHaveTextContent("XHigh");
  });

  it("keeps metadata success when bounded history loading fails", async () => {
    mockCatalog();
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/api/runtime") {
        return Promise.resolve({
          provider: "local",
          status: "ready",
          artifact_version: null,
          last_error: null,
          runner_public_key: null,
        });
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/page`) {
        return Promise.reject(new Error("history unavailable"));
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderSession();

    expect(
      await screen.findByText("Failed to load session history."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Failed to load session metadata."),
    ).not.toBeInTheDocument();
    expect(apiGetMock).toHaveBeenCalledWith(`/api/sessions/${TEST_SESSION_ID}`);
  });

  it("skips managed cache when tab storage is unavailable", async () => {
    mockCatalog();
    historyCacheAvailable = false;
    const emptyBundle = await historyBundle([], null);
    const transportGet = vi.fn((path: string) => {
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/bundle`) {
        return Promise.resolve(emptyBundle);
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    runtimeResourceOverride = managedRuntimeResource(transportGet);

    renderSession();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send Prompt" })).toBeEnabled(),
    );
    expect(historyCacheClient.get).not.toHaveBeenCalled();
    expect(historyCacheClient.put).not.toHaveBeenCalled();
  });

  it("marks cached and live history rendering in order", async () => {
    mockCatalog();
    const performanceMarks: string[] = [];
    const clearMarksSpy = vi
      .spyOn(performance, "clearMarks")
      .mockImplementation(() => undefined);
    vi.spyOn(performance, "mark").mockImplementation((name) => {
      performanceMarks.push(name);
      return {} as PerformanceMark;
    });
    const runId = "b".repeat(32);
    const cachedMessage = {
      id: "c".repeat(32),
      created_at: "2026-08-24T03:00:00Z",
      session_id: TEST_SESSION_ID,
      role: "user",
      content: "cached history",
      full_message: null,
      turn_id: runId,
      turn_status: null,
    };
    historyCacheClient.get.mockResolvedValue([
      await historyBundle([cachedMessage], runId),
    ]);
    const liveBundle = deferred<unknown>();
    const transportGet = vi.fn((path: string) => {
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/bundle`) {
        return liveBundle.promise;
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    runtimeResourceOverride = managedRuntimeResource(transportGet);

    renderSession();

    await waitFor(() => {
      expect(bootstrapSessionMock).toHaveBeenCalledWith(
        [expect.objectContaining({ content: "cached history" })],
        runId,
      );
    });
    expect(clearMarksSpy.mock.calls.map(([name]) => name)).toEqual([
      "yinshi:session-history-start",
      "yinshi:session-history-cache-rendered",
      "yinshi:session-history-live-rendered",
      "yinshi:session-history-live-failed",
    ]);
    expect(performanceMarks).toEqual([
      "yinshi:session-history-start",
      "yinshi:session-history-cache-rendered",
    ]);
    expect(historyCacheClient.get).toHaveBeenCalledWith(
      "user-1",
      TEST_SESSION_ID,
    );
    expect(transportGet).toHaveBeenCalledWith(
      `/api/sessions/${TEST_SESSION_ID}/messages/bundle`,
    );
    expect(
      screen.queryByText("Loading conversation..."),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Refreshing session history before new prompts."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send Prompt" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Send Prompt" }));
    expect(sendPromptMock).not.toHaveBeenCalled();

    await act(async () => {
      liveBundle.resolve(await historyBundle([cachedMessage], runId));
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send Prompt" })).toBeEnabled(),
    );
    expect(performanceMarks).toEqual([
      "yinshi:session-history-start",
      "yinshi:session-history-cache-rendered",
      "yinshi:session-history-live-rendered",
    ]);
    expect(
      screen.queryByText("Refreshing session history before new prompts."),
    ).not.toBeInTheDocument();
  });

  it("marks a failed live refresh after cached history renders", async () => {
    mockCatalog();
    const cachedMessage = {
      id: "c".repeat(32),
      created_at: "2026-08-24T03:00:00Z",
      session_id: TEST_SESSION_ID,
      role: "user",
      content: "cached history",
      full_message: null,
      turn_id: null,
      turn_status: null,
    };
    historyCacheClient.get.mockResolvedValue([
      await historyBundle([cachedMessage], null),
    ]);
    const liveBundle = deferred<unknown>();
    const transportGet = vi.fn((path: string) => {
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/bundle`) {
        return liveBundle.promise;
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    runtimeResourceOverride = managedRuntimeResource(transportGet);
    const performanceMarks: string[] = [];
    vi.spyOn(performance, "mark").mockImplementation((name) => {
      performanceMarks.push(name);
      return {} as PerformanceMark;
    });
    renderSession();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Send Prompt" }),
      ).toBeDisabled(),
    );
    expect(performanceMarks).toEqual([
      "yinshi:session-history-start",
      "yinshi:session-history-cache-rendered",
    ]);

    await act(async () => {
      liveBundle.reject(new Error("offline"));
    });

    expect(
      await screen.findByText("Failed to refresh session history."),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send Prompt" })).toBeEnabled(),
    );
    expect(performanceMarks).toEqual([
      "yinshi:session-history-start",
      "yinshi:session-history-cache-rendered",
      "yinshi:session-history-live-failed",
    ]);
  });

  it("bootstraps a bundle active run without discovery and deduplicates its assistant", async () => {
    mockCatalog();
    const runId = "b".repeat(32);
    const bundle = deferred<unknown>();
    const storedMessages = [
      {
        id: "c".repeat(32),
        created_at: "2026-08-24T03:00:00Z",
        session_id: TEST_SESSION_ID,
        role: "user",
        content: "keep me",
        full_message: null,
        turn_id: runId,
        turn_status: null,
      },
      {
        id: "d".repeat(32),
        created_at: "2026-08-24T03:00:01Z",
        session_id: TEST_SESSION_ID,
        role: "assistant",
        content: "stored completion",
        full_message: null,
        turn_id: runId,
        turn_status: "completed",
      },
    ];
    const transportGet = vi.fn((path: string) => {
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/bundle`) {
        return bundle.promise;
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    runtimeResourceOverride = managedRuntimeResource(transportGet);

    renderSession();

    await waitFor(() => {
      expect(transportGet).toHaveBeenCalledWith(
        `/api/sessions/${TEST_SESSION_ID}/messages/bundle`,
      );
    });
    expect(transportGet).not.toHaveBeenCalledWith(
      `/api/sessions/${TEST_SESSION_ID}/runs/active`,
    );
    expect(bootstrapSessionMock).not.toHaveBeenCalled();

    await act(async () => {
      bundle.resolve(await historyBundle(storedMessages, runId));
    });

    await waitFor(() => expect(bootstrapSessionMock).toHaveBeenCalled());
    const [history, activeRunId] = bootstrapSessionMock.mock.calls[0];
    expect(activeRunId).toBe(runId);
    expect(history).toEqual([
      expect.objectContaining({ role: "user", content: "keep me" }),
    ]);
    expect(transportGet).not.toHaveBeenCalledWith(
      `/api/sessions/${TEST_SESSION_ID}/runs/active`,
    );
  });

  it("invalidates cached history before prompt send and navigation", async () => {
    mockCatalog();
    const emptyBundle = await historyBundle([], null);
    const transportGet = vi.fn((path: string) => {
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/bundle`) {
        return Promise.resolve(emptyBundle);
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    runtimeResourceOverride = managedRuntimeResource(transportGet);
    let cacheAvailable = true;
    historyCacheClient.delete.mockImplementation(() => {
      cacheAvailable = false;
      return new Promise<void>(() => {});
    });
    sendPromptMock.mockReturnValue(new Promise<void>(() => {}));
    const view = renderSession();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send Prompt" })).toBeEnabled(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Send Prompt" }));

    await waitFor(() => expect(sendPromptMock).toHaveBeenCalledTimes(1));
    expect(historyCacheClient.delete).toHaveBeenCalledWith(
      "user-1",
      TEST_SESSION_ID,
    );
    expect(historyCacheClient.delete.mock.invocationCallOrder[0]).toBeLessThan(
      sendPromptMock.mock.invocationCallOrder[0],
    );
    view.unmount();
    expect(cacheAvailable).toBe(false);
  });

  it("refreshes the managed history cache once after streaming ends", async () => {
    mockCatalog();
    let streaming = false;
    useAgentStreamMock.mockImplementation(() => ({
      messages: [],
      sendPrompt: sendPromptMock,
      cancel: cancelMock,
      streaming,
      setMessages: setMessagesMock,
      bootstrapSession: bootstrapSessionMock,
    }));
    const emptyBundle = await historyBundle([], null);
    const transportGet = vi.fn((path: string) => {
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/bundle`) {
        return Promise.resolve(emptyBundle);
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    runtimeResourceOverride = managedRuntimeResource(transportGet);
    const tree = () => (
      <MemoryRouter initialEntries={[`/app/sessions/${TEST_SESSION_ID}`]}>
        <Routes>
          <Route path="/app/sessions/:id" element={<Session />} />
        </Routes>
      </MemoryRouter>
    );
    const view = render(tree());
    await waitFor(() =>
      expect(historyCacheClient.put).toHaveBeenCalledTimes(1),
    );
    historyCacheClient.put.mockClear();

    streaming = true;
    view.rerender(tree());
    streaming = false;
    view.rerender(tree());

    await waitFor(() =>
      expect(historyCacheClient.put).toHaveBeenCalledTimes(1),
    );
    expect(transportGet).toHaveBeenCalledTimes(3);
  });

  it("discovers active state after a managed bundle falls back to legacy history", async () => {
    mockCatalog();
    const runId = "b".repeat(32);
    const transportGet = vi.fn((path: string) => {
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/bundle`) {
        return Promise.reject(
          new ApiError(413, "large", {
            code: "history_bundle_message_too_large",
          }),
        );
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/page`) {
        return Promise.resolve({ messages: [], next_cursor: null });
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}/runs/active`) {
        return Promise.resolve({
          id: runId,
          session_id: TEST_SESSION_ID,
          status: "running",
        });
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    runtimeResourceOverride = managedRuntimeResource(transportGet);

    renderSession();

    await waitFor(() => {
      expect(bootstrapSessionMock).toHaveBeenCalledWith([], runId);
    });
    const requestedPaths = transportGet.mock.calls.map((call) => call[0]);
    expect(
      requestedPaths.indexOf(`/api/sessions/${TEST_SESSION_ID}/messages/page`),
    ).toBeLessThan(
      requestedPaths.indexOf(`/api/sessions/${TEST_SESSION_ID}/runs/active`),
    );
  });

  it("loads history and resumes the discovered active run without starting it", async () => {
    mockCatalog();
    const runId = "b".repeat(32);
    mockSessionApi(sessionMetadata(), [], {
      id: runId,
      session_id: TEST_SESSION_ID,
      status: "running",
    });

    renderSession();

    await waitFor(() => {
      expect(bootstrapSessionMock).toHaveBeenCalledWith([], runId);
    });
    const requestedPaths = apiGetMock.mock.calls.map((call) => call[0]);
    expect(
      requestedPaths.indexOf(`/api/sessions/${TEST_SESSION_ID}/runs/active`),
    ).toBeLessThan(
      requestedPaths.indexOf(`/api/sessions/${TEST_SESSION_ID}/messages/page`),
    );
    expect(sendPromptMock).not.toHaveBeenCalled();
  });

  it("removes a stored assistant for the active turn before journal replay", async () => {
    mockCatalog();
    const runId = "b".repeat(32);
    mockSessionApi(
      sessionMetadata(),
      [
        {
          id: "c".repeat(32),
          created_at: "2026-08-24T03:00:00Z",
          session_id: TEST_SESSION_ID,
          role: "user",
          content: "keep me",
          full_message: null,
          turn_id: runId,
          turn_status: null,
        },
        {
          id: "d".repeat(32),
          created_at: "2026-08-24T03:00:01Z",
          session_id: TEST_SESSION_ID,
          role: "assistant",
          content: "stored completion",
          full_message: null,
          turn_id: runId,
          turn_status: "completed",
        },
      ],
      {
        id: runId,
        session_id: TEST_SESSION_ID,
        status: "running",
      },
    );

    renderSession();

    await waitFor(() => expect(bootstrapSessionMock).toHaveBeenCalled());
    const [history, activeRunId] = bootstrapSessionMock.mock.calls[0];
    expect(activeRunId).toBe(runId);
    expect(history.map((message: { role: string }) => message.role)).toEqual([
      "user",
    ]);
    expect(history[0]).toEqual(expect.objectContaining({ content: "keep me" }));
  });

  it("resumes a discovered active run when history loading fails", async () => {
    mockCatalog();
    const runId = "b".repeat(32);
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/api/runtime") {
        return Promise.resolve({
          provider: "local",
          status: "ready",
          artifact_version: null,
          last_error: null,
          runner_public_key: null,
        });
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}/runs/active`) {
        return Promise.resolve({
          id: runId,
          session_id: TEST_SESSION_ID,
          status: "running",
        });
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/page`) {
        return Promise.reject(new Error("history unavailable"));
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderSession();

    expect(
      await screen.findByText("Failed to load session history."),
    ).toBeInTheDocument();
    expect(bootstrapSessionMock).toHaveBeenCalledWith([], runId);
  });

  it("discovers a managed active run after a malformed bundle", async () => {
    mockCatalog();
    const runId = "b".repeat(32);
    const transportGet = vi.fn((path: string) => {
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/bundle`) {
        return Promise.resolve({ malformed: true });
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}/runs/active`) {
        return Promise.resolve({
          id: runId,
          session_id: TEST_SESSION_ID,
          status: "running",
        });
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    runtimeResourceOverride = managedRuntimeResource(transportGet);

    renderSession();

    expect(
      await screen.findByText("Failed to load session history."),
    ).toBeInTheDocument();
    expect(bootstrapSessionMock).toHaveBeenCalledWith([], runId);
    const paths = transportGet.mock.calls.map((call) => call[0]);
    expect(
      paths.indexOf(`/api/sessions/${TEST_SESSION_ID}/messages/bundle`),
    ).toBeLessThan(
      paths.indexOf(`/api/sessions/${TEST_SESSION_ID}/runs/active`),
    );
  });

  it("preserves managed history failure when post-bundle discovery fails", async () => {
    mockCatalog();
    const transportGet = vi.fn((path: string) => {
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/bundle`) {
        return Promise.resolve({ malformed: true });
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}/runs/active`) {
        return Promise.reject(new Error("discovery unavailable"));
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    runtimeResourceOverride = managedRuntimeResource(transportGet);

    renderSession();

    expect(
      await screen.findByText("Failed to load session history."),
    ).toBeInTheDocument();
    expect(bootstrapSessionMock).not.toHaveBeenCalled();
  });

  it("does not discover managed active state after cancelled bundle loading", async () => {
    mockCatalog();
    const bundle = deferred<unknown>();
    const transportGet = vi.fn((path: string) => {
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/bundle`) {
        return bundle.promise;
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });
    runtimeResourceOverride = managedRuntimeResource(transportGet);
    const rendered = renderSession();
    await waitFor(() =>
      expect(transportGet).toHaveBeenCalledWith(
        `/api/sessions/${TEST_SESSION_ID}/messages/bundle`,
      ),
    );

    rendered.unmount();
    await act(async () => {
      bundle.resolve({ malformed: true });
      await Promise.resolve();
    });

    expect(transportGet).not.toHaveBeenCalledWith(
      `/api/sessions/${TEST_SESSION_ID}/runs/active`,
    );
    expect(bootstrapSessionMock).not.toHaveBeenCalled();
  });

  it("keeps loaded history and reports failed active discovery", async () => {
    mockCatalog();
    const messageId = "c".repeat(32);
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/api/runtime") {
        return Promise.resolve({
          provider: "local",
          status: "ready",
          artifact_version: null,
          last_error: null,
          runner_public_key: null,
        });
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}/runs/active`) {
        return Promise.reject(new Error("discovery unavailable"));
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/page`) {
        return Promise.resolve({
          messages: [
            {
              id: messageId,
              created_at: "2026-08-24T03:00:00Z",
              session_id: TEST_SESSION_ID,
              role: "user",
              content_length: 4,
              full_message_length: null,
              turn_id: null,
              turn_status: null,
            },
          ],
          next_cursor: null,
        });
      }
      if (
        path.startsWith(
          `/api/sessions/${TEST_SESSION_ID}/messages/${messageId}/field`,
        )
      ) {
        return Promise.resolve({ value: "keep", offset: 0, next_offset: null });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderSession();

    expect(
      await screen.findByText("Failed to resume the active response."),
    ).toBeInTheDocument();
    expect(bootstrapSessionMock).toHaveBeenCalledWith(
      [expect.objectContaining({ content: "keep" })],
      null,
    );
  });

  it("ignores concurrent history results after cancellation", async () => {
    mockCatalog();
    const activeRun = deferred<unknown>();
    const historyPage = deferred<unknown>();
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/api/runtime") {
        return Promise.resolve({
          provider: "local",
          status: "ready",
          artifact_version: null,
          last_error: null,
          runner_public_key: null,
        });
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}/runs/active`) {
        return activeRun.promise;
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.resolve(sessionMetadata());
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/page`) {
        return historyPage.promise;
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    const rendered = renderSession();
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith(
        `/api/sessions/${TEST_SESSION_ID}/runs/active`,
      );
      expect(apiGetMock).toHaveBeenCalledWith(
        `/api/sessions/${TEST_SESSION_ID}/messages/page`,
      );
    });

    rendered.unmount();
    await act(async () => {
      activeRun.resolve({
        id: "b".repeat(32),
        session_id: TEST_SESSION_ID,
        status: "running",
      });
      historyPage.reject(new Error("late history failure"));
      await Promise.resolve();
    });

    expect(bootstrapSessionMock).not.toHaveBeenCalled();
    expect(cancelMock).not.toHaveBeenCalled();
  });

  it("keeps loaded history when metadata loading fails", async () => {
    mockCatalog();
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/api/runtime") {
        return Promise.resolve({
          provider: "local",
          status: "ready",
          artifact_version: null,
          last_error: null,
          runner_public_key: null,
        });
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}`) {
        return Promise.reject(new Error("metadata unavailable"));
      }
      if (path === `/api/sessions/${TEST_SESSION_ID}/messages/page`) {
        return Promise.resolve({ messages: [], next_cursor: null });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderSession();

    await waitFor(() => expect(setMessagesMock).toHaveBeenCalledWith([]));
    expect(
      await screen.findByText("Failed to load session metadata."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Failed to load session history."),
    ).not.toBeInTheDocument();
  });

  it("loads history above one MiB with a multi-chunk structured message", async () => {
    mockCatalog();
    const largeToolOutput = "界".repeat(70_000);
    const storedMessages = Array.from({ length: 36 }, (_, index) => {
      const messageId = (index + 1).toString(16).padStart(32, "0");
      const isLast = index === 35;
      return {
        id: messageId,
        created_at: `2026-04-26T00:00:${String(index).padStart(2, "0")}Z`,
        session_id: TEST_SESSION_ID,
        role: "assistant",
        content: `Done ${index}`,
        full_message: JSON.stringify({
          schema: "yinshi.assistant_turn.v1",
          events: isLast
            ? [
                {
                  type: "tool_use",
                  id: "tool-large",
                  name: "read",
                  input: { path: "large.txt" },
                },
                {
                  type: "tool_result",
                  tool_use_id: "tool-large",
                  content: largeToolOutput,
                },
                {
                  type: "assistant",
                  message: { content: [{ type: "text", text: "Done" }] },
                },
                { type: "result" },
              ]
            : [
                {
                  type: "assistant",
                  message: {
                    content: [{ type: "text", text: "x".repeat(30_000) }],
                  },
                },
                { type: "result" },
              ],
        }),
        turn_id: `turn-${index}`,
        turn_status: "completed",
      };
    });
    const storedBytes = new TextEncoder().encode(
      JSON.stringify(storedMessages),
    ).length;
    expect(storedBytes).toBeGreaterThan(1_048_576);
    mockSessionApi(sessionMetadata(), storedMessages);

    renderSession();

    await waitFor(() => {
      expect(setMessagesMock).toHaveBeenCalled();
    });
    const mappedMessages = setMessagesMock.mock.calls[0]?.[0];
    expect(mappedMessages).toHaveLength(36);
    expect(mappedMessages[35]).toMatchObject({
      id: (36).toString(16).padStart(32, "0"),
      role: "assistant",
      content: "Done 35",
      blocks: [
        {
          type: "tool_use",
          toolName: "read",
          toolInput: { path: "large.txt" },
          toolOutput: largeToolOutput,
        },
        { type: "text", text: "Done" },
      ],
    });
    const lastMessageId = (36).toString(16).padStart(32, "0");
    const largeFieldCalls = apiGetMock.mock.calls.filter(
      ([path]) =>
        typeof path === "string" &&
        path.includes(`/messages/${lastMessageId}/field`) &&
        path.includes("name=full_message"),
    );
    expect(largeFieldCalls.length).toBeGreaterThan(2);
    expect(apiGetMock).toHaveBeenCalledWith(`/api/sessions/${TEST_SESSION_ID}`);
  });

  it("reconstructs assistant trace blocks from stored full messages", async () => {
    mockCatalog();
    mockSessionApi({ model: minimaxModel.ref }, [
      {
        id: "b".repeat(32),
        created_at: "2026-04-26T00:00:00Z",
        session_id: TEST_SESSION_ID,
        role: "assistant",
        content: "Done",
        full_message: JSON.stringify({
          schema: "yinshi.assistant_turn.v1",
          events: [
            {
              type: "assistant",
              message: {
                content: [{ type: "thinking", thinking: "Inspect." }],
              },
            },
            {
              type: "tool_use",
              id: "tool-1",
              name: "read",
              input: { path: "README.md" },
            },
            {
              type: "tool_result",
              tool_use_id: "tool-1",
              content: "# Test",
            },
            {
              type: "assistant",
              message: { content: [{ type: "text", text: "Done" }] },
            },
            { type: "result" },
          ],
        }),
      },
    ]);

    renderSession();

    await waitFor(() => {
      expect(setMessagesMock).toHaveBeenCalled();
    });
    const mappedMessages = setMessagesMock.mock.calls[0]?.[0];

    expect(mappedMessages).toMatchObject([
      {
        id: "b".repeat(32),
        role: "assistant",
        content: "Done",
        blocks: [
          { type: "thinking", text: "Inspect." },
          {
            type: "tool_use",
            toolName: "read",
            toolInput: { path: "README.md" },
            toolOutput: "# Test",
          },
          { type: "text", text: "Done" },
        ],
      },
    ]);
  });
});
