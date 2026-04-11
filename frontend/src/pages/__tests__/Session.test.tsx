import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiGetMock = vi.fn();
const apiPatchMock = vi.fn();
const cancelMock = vi.fn();
const sendPromptMock = vi.fn();
const setMessagesMock = vi.fn();
const useCatalogMock = vi.fn();
const useAgentStreamMock = vi.fn();

vi.mock("../../api/client", () => ({
  api: {
    get: (...args: unknown[]) => apiGetMock(...args),
    patch: (...args: unknown[]) => apiPatchMock(...args),
  },
}));

vi.mock("../../components/ChatView", () => ({
  default: ({ onSend }: { onSend: (prompt: string) => void | Promise<void> }) => (
    <button
      type="button"
      onClick={() => {
        void onSend("Ship it");
      }}
    >
      Send Prompt
    </button>
  ),
}));

vi.mock("../../hooks/useAgentStream", () => ({
  useAgentStream: (...args: unknown[]) => useAgentStreamMock(...args),
}));

vi.mock("../../hooks/useCatalog", () => ({
  useCatalog: () => useCatalogMock(),
}));

import Session from "../Session";

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
    apiPatchMock.mockResolvedValue({ model: "minimax/MiniMax-M2.7" });
  });

  it("does not override the persisted model or thinking settings before the user changes them", async () => {
    const pendingSessionPromise = new Promise<{ model: string }>(() => {});
    useCatalogMock.mockReturnValue({
      catalog: {
        default_model: "minimax/MiniMax-M2.7",
        providers: [
          {
            id: "minimax",
            label: "MiniMax",
            auth_strategies: ["api_key"],
            setup_fields: [],
            docs_url: "https://example.com/minimax",
            connected: true,
            model_count: 1,
          },
        ],
        models: [
          {
            ref: "minimax/MiniMax-M2.7",
            provider: "minimax",
            id: "MiniMax-M2.7",
            label: "MiniMax M2.7",
            api: "responses",
            reasoning: true,
            inputs: ["text"],
            context_window: 1000,
            max_tokens: 1000,
          },
        ],
      },
      loading: false,
    });
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/api/sessions/session-123/messages") {
        return Promise.resolve([]);
      }
      if (path === "/api/sessions/session-123") {
        return pendingSessionPromise;
      }
      throw new Error(`Unexpected GET path: ${path}`);
    });

    renderSession();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Send Prompt" })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Send Prompt" }));

    await waitFor(() => {
      expect(sendPromptMock).toHaveBeenCalledWith("Ship it", undefined, undefined);
    });
  });

  it("omits the thinking override for models that do not support reasoning", async () => {
    useCatalogMock.mockReturnValue({
      catalog: {
        default_model: "openai/gpt-4.1",
        providers: [
          {
            id: "openai",
            label: "OpenAI",
            auth_strategies: ["api_key"],
            setup_fields: [],
            docs_url: "https://example.com/openai",
            connected: true,
            model_count: 1,
          },
        ],
        models: [
          {
            ref: "openai/gpt-4.1",
            provider: "openai",
            id: "gpt-4.1",
            label: "GPT-4.1",
            api: "responses",
            reasoning: false,
            inputs: ["text"],
            context_window: 1000,
            max_tokens: 1000,
          },
        ],
      },
      loading: false,
    });
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/api/sessions/session-123/messages") {
        return Promise.resolve([]);
      }
      if (path === "/api/sessions/session-123") {
        return Promise.resolve({ model: "openai/gpt-4.1" });
      }
      throw new Error(`Unexpected GET path: ${path}`);
    });

    renderSession();

    const thinkingButton = await screen.findByRole("button", { name: "Thinking" });

    await waitFor(() => {
      expect(thinkingButton).toBeDisabled();
      expect(thinkingButton).toHaveAttribute("title", "This model does not support thinking");
    });

    fireEvent.click(screen.getByRole("button", { name: "Send Prompt" }));

    await waitFor(() => {
      expect(sendPromptMock).toHaveBeenCalledWith("Ship it", undefined, undefined);
    });
  });

  it("forwards an explicit thinking override for reasoning models", async () => {
    useCatalogMock.mockReturnValue({
      catalog: {
        default_model: "minimax/MiniMax-M2.7",
        providers: [
          {
            id: "minimax",
            label: "MiniMax",
            auth_strategies: ["api_key"],
            setup_fields: [],
            docs_url: "https://example.com/minimax",
            connected: true,
            model_count: 1,
          },
        ],
        models: [
          {
            ref: "minimax/MiniMax-M2.7",
            provider: "minimax",
            id: "MiniMax-M2.7",
            label: "MiniMax M2.7",
            api: "responses",
            reasoning: true,
            inputs: ["text"],
            context_window: 1000,
            max_tokens: 1000,
          },
        ],
      },
      loading: false,
    });
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/api/sessions/session-123/messages") {
        return Promise.resolve([]);
      }
      if (path === "/api/sessions/session-123") {
        return Promise.resolve({ model: "minimax/MiniMax-M2.7" });
      }
      throw new Error(`Unexpected GET path: ${path}`);
    });

    renderSession();

    const thinkingButton = await screen.findByRole("button", { name: "Thinking" });
    fireEvent.click(thinkingButton);
    fireEvent.click(screen.getByRole("button", { name: "Send Prompt" }));

    await waitFor(() => {
      expect(sendPromptMock).toHaveBeenCalledWith("Ship it", undefined, false);
    });
  });
});
