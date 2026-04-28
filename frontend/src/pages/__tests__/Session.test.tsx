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
const useCatalogMock = vi.fn();
const useAgentStreamMock = vi.fn();

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

vi.mock("../../api/client", () => ({
  api: {
    get: (...args: unknown[]) => apiGetMock(...args),
    patch: (...args: unknown[]) => apiPatchMock(...args),
  },
}));

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

vi.mock("../../hooks/useCatalog", () => ({
  useCatalog: () => useCatalogMock(),
}));

vi.mock("../../hooks/usePiCommands", () => ({
  usePiCommands: () => [],
}));

import Session from "../Session";

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

function sessionMetadata(overrides: Record<string, unknown> = {}) {
  return {
    id: "session-123",
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
  sessionMetadataValue: Record<string, unknown> | Promise<Record<string, unknown>> = sessionMetadata(),
  messages: unknown[] = [],
) {
  const metadata =
    sessionMetadataValue instanceof Promise
      ? sessionMetadataValue
      : sessionMetadata(sessionMetadataValue);
  apiGetMock.mockImplementation((path: string) => {
    if (path === "/api/sessions/session-123/messages") {
      return Promise.resolve(messages);
    }
    if (path === "/api/sessions/session-123") {
      return Promise.resolve(metadata);
    }
    throw new Error(`Unexpected GET path: ${path}`);
  });
}

function renderSession() {
  return render(
    <MemoryRouter initialEntries={["/app/sessions/session-123"]}>
      <Routes>
        <Route path="/app/sessions/:id" element={<Session />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Session", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    sendPromptMock.mockResolvedValue(undefined);
    cancelMock.mockResolvedValue(undefined);
    useAgentStreamMock.mockReturnValue({
      messages: [],
      sendPrompt: sendPromptMock,
      cancel: cancelMock,
      streaming: false,
      setMessages: setMessagesMock,
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
    });
    mockSessionApi(sessionMetadata({ pi_context_version: 0 }));

    renderSession();

    const sendButton = await screen.findByRole("button", { name: "Send Prompt" });
    expect(sendButton).toBeDisabled();
    expect(
      screen.getByText(/predates durable Pi context/),
    ).toBeInTheDocument();
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

  it("reconstructs assistant trace blocks from stored full messages", async () => {
    mockCatalog();
    mockSessionApi({ model: minimaxModel.ref }, [
      {
        id: "message-1",
        created_at: "2026-04-26T00:00:00Z",
        session_id: "session-123",
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
        id: "message-1",
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
