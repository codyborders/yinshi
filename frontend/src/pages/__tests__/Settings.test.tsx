import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const useAuthMock = vi.fn();
const useCatalogMock = vi.fn();
const apiGetMock = vi.fn();
const apiPostMock = vi.fn();
const apiDeleteMock = vi.fn();
const pollAuthFlowMock = vi.fn();
const submitAuthFlowInputMock = vi.fn();

vi.mock("../../hooks/useAuth", () => ({
  useAuth: () => useAuthMock(),
}));

vi.mock("../../hooks/useCatalog", () => ({
  useCatalog: () => useCatalogMock(),
}));

vi.mock("../../components/PiConfigSection", () => ({
  default: () => <div data-testid="pi-config-section" />,
}));

vi.mock("../../api/client", () => ({
  api: {
    get: (...args: unknown[]) => apiGetMock(...args),
    post: (...args: unknown[]) => apiPostMock(...args),
    delete: (...args: unknown[]) => apiDeleteMock(...args),
  },
  pollAuthFlow: (...args: unknown[]) => pollAuthFlowMock(...args),
  submitAuthFlowInput: (...args: unknown[]) => submitAuthFlowInputMock(...args),
}));

import Settings from "../Settings";

describe("Settings", () => {
  beforeEach(() => {
    useAuthMock.mockReturnValue({ email: "tester@example.com" });
    useCatalogMock.mockReturnValue({
      catalog: {
        default_model: "minimax/MiniMax-M2.7",
        providers: [
          {
            id: "openai-codex",
            label: "OpenAI Codex",
            auth_strategies: ["oauth"],
            setup_fields: [],
            docs_url: "https://example.com/openai-codex",
            connected: false,
            model_count: 1,
          },
        ],
        models: [],
      },
      loading: false,
      error: null,
    });
    apiGetMock.mockResolvedValue([]);
    apiDeleteMock.mockResolvedValue(undefined);
    apiPostMock.mockImplementation((path: string) => {
      if (path === "/auth/providers/openai-codex/start") {
        return Promise.resolve({
          flow_id: "flow-openai-codex",
          provider: "openai-codex",
          auth_url: "https://auth.openai.com/oauth/authorize",
          instructions: "Open the browser and sign in.",
          manual_input_required: true,
          manual_input_prompt: "Paste the final redirect URL or authorization code here.",
          manual_input_submitted: false,
        });
      }
      throw new Error(`Unexpected POST path: ${path}`);
    });
    pollAuthFlowMock.mockImplementation(() => new Promise(() => {}));
    submitAuthFlowInputMock.mockResolvedValue({
      status: "pending",
      provider: "openai-codex",
      flow_id: "flow-openai-codex",
      instructions: "Open the browser and sign in.",
      progress: ["Received manual OAuth callback input."],
      manual_input_required: true,
      manual_input_prompt: "Paste the final redirect URL or authorization code here.",
      manual_input_submitted: true,
      error: null,
    });
    vi.spyOn(window, "open").mockImplementation(() => null);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows and submits manual OAuth callback input for localhost redirect providers", async () => {
    render(<Settings />);

    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith("/api/settings/connections");
    });

    fireEvent.click(screen.getByRole("button", { name: "Connect Provider" }));

    await waitFor(() => {
      expect(screen.getByText("Open the browser and sign in.")).toBeInTheDocument();
    });

    const textarea = screen.getByPlaceholderText(
      "http://localhost:1455/auth/callback?code=...",
    );
    fireEvent.change(textarea, {
      target: {
        value: "http://localhost:1455/auth/callback?code=test-code&state=test-state",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit Callback URL" }));

    await waitFor(() => {
      expect(submitAuthFlowInputMock).toHaveBeenCalledWith(
        "openai-codex",
        "flow-openai-codex",
        "http://localhost:1455/auth/callback?code=test-code&state=test-state",
      );
    });

    expect(
      screen.getByText("Waiting for the provider to finish the OAuth flow."),
    ).toBeInTheDocument();
  });
});
