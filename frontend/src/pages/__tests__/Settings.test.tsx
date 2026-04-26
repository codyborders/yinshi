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

vi.mock("../../components/PiReleaseNotesSection", () => ({
  default: () => <div data-testid="pi-release-notes-section" />,
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
    vi.clearAllMocks();
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
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/api/settings/connections") {
        return Promise.resolve([]);
      }
      if (path === "/api/settings/runner") {
        return Promise.resolve(null);
      }
      throw new Error(`Unexpected GET path: ${path}`);
    });
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

  it("switches between settings tabs", () => {
    render(<Settings />);

    fireEvent.click(screen.getByRole("tab", { name: "Cloud runner" }));
    expect(screen.getByRole("heading", { name: "Cloud Runner" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Pi config" }));
    expect(screen.getByTestId("pi-config-section")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Pi release notes" }));
    expect(screen.getByTestId("pi-release-notes-section")).toBeInTheDocument();
    expect(screen.queryByTestId("pi-config-section")).not.toBeInTheDocument();
  });

  it("shows all runner storage choices without creating hosted tokens", async () => {
    render(<Settings />);
    fireEvent.click(screen.getByRole("tab", { name: "Cloud runner" }));

    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith("/api/settings/runner");
    });

    expect(screen.getByRole("radio", { name: /Hosted Yinshi/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /AWS BYOC: EBS plus S3 Files/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Archil shared-files mode/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Archil all-POSIX mode/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create Token" })).not.toBeInTheDocument();
    expect(apiPostMock).not.toHaveBeenCalledWith("/api/settings/runner", expect.anything());
  });

  it("uses offline badge styling when the API returns an unknown runner status", async () => {
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/api/settings/connections") {
        return Promise.resolve([]);
      }
      if (path === "/api/settings/runner") {
        return Promise.resolve({
          id: "runner-degraded",
          created_at: "2026-04-25T00:00:00+00:00",
          updated_at: "2026-04-25T00:00:00+00:00",
          name: "Unexpected status runner",
          cloud_provider: "aws",
          region: "us-east-1",
          status: "degraded",
          registered_at: "2026-04-25T00:00:00+00:00",
          last_heartbeat_at: null,
          runner_version: "0.1.0",
          capabilities: {
            storage_profile: "aws_ebs_s3_files",
            sqlite_storage: "runner_ebs",
            shared_files_storage: "s3_files_mount",
          },
          data_dir: "/var/lib/yinshi",
        });
      }
      throw new Error(`Unexpected GET path: ${path}`);
    });

    render(<Settings />);
    fireEvent.click(screen.getByRole("tab", { name: "Cloud runner" }));

    const statusBadge = await screen.findByText("degraded");
    expect(statusBadge).toHaveClass("border-gray-600", "bg-gray-800", "text-gray-300");
  });

  it("creates an AWS BYOC runner registration token", async () => {
    apiPostMock.mockImplementation((path: string) => {
      if (path === "/api/settings/runner") {
        return Promise.resolve({
          runner: {
            id: "runner-1",
            created_at: "2026-04-25T00:00:00+00:00",
            updated_at: "2026-04-25T00:00:00+00:00",
            name: "AWS runner",
            cloud_provider: "aws",
            region: "us-east-1",
            status: "pending",
            registered_at: null,
            last_heartbeat_at: null,
            runner_version: null,
            capabilities: {
              storage_profile: "aws_ebs_s3_files",
              sqlite_storage: "runner_ebs",
              shared_files_storage: "s3_files_mount",
            },
            data_dir: null,
          },
          registration_token: "registration-token",
          registration_token_expires_at: "2026-04-25T01:00:00+00:00",
          control_url: "https://yinshi.example.com",
          environment: {
            YINSHI_CONTROL_URL: "https://yinshi.example.com",
            YINSHI_REGISTRATION_TOKEN: "registration-token",
            YINSHI_RUNNER_STORAGE_PROFILE: "aws_ebs_s3_files",
            YINSHI_RUNNER_SQLITE_STORAGE: "runner_ebs",
            YINSHI_RUNNER_SHARED_FILES_STORAGE: "s3_files_or_local_posix",
            YINSHI_RUNNER_DATA_DIR: "/var/lib/yinshi",
            YINSHI_RUNNER_SQLITE_DIR: "/var/lib/yinshi/sqlite",
            YINSHI_RUNNER_SHARED_FILES_DIR: "/mnt/yinshi-s3-files",
          },
        });
      }
      throw new Error(`Unexpected POST path: ${path}`);
    });

    render(<Settings />);
    fireEvent.click(screen.getByRole("tab", { name: "Cloud runner" }));

    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith("/api/settings/runner");
    });

    fireEvent.click(screen.getByRole("radio", { name: /AWS BYOC: EBS plus S3 Files/ }));
    fireEvent.click(screen.getByRole("button", { name: "Create Token" }));

    await waitFor(() => {
      expect(apiPostMock).toHaveBeenCalledWith("/api/settings/runner", {
        name: "AWS runner",
        cloud_provider: "aws",
        region: "us-east-1",
        storage_profile: "aws_ebs_s3_files",
      });
    });

    expect(screen.getByText("One-time registration values")).toBeInTheDocument();
    expect(screen.getByText("Runner EBS")).toBeInTheDocument();
    expect(screen.getByText("S3 Files mount")).toBeInTheDocument();
    expect(screen.getByDisplayValue(/YINSHI_REGISTRATION_TOKEN=registration-token/)).toBeInTheDocument();
    expect(screen.getByDisplayValue(/YINSHI_RUNNER_STORAGE_PROFILE=aws_ebs_s3_files/)).toBeInTheDocument();
    expect(screen.getByDisplayValue(/YINSHI_RUNNER_SQLITE_DIR=\/var\/lib\/yinshi\/sqlite/)).toBeInTheDocument();
  });

  it("creates an Archil shared-files runner with warning copy", async () => {
    apiPostMock.mockImplementation((path: string) => {
      if (path === "/api/settings/runner") {
        return Promise.resolve({
          runner: {
            id: "runner-archil-shared",
            created_at: "2026-04-25T00:00:00+00:00",
            updated_at: "2026-04-25T00:00:00+00:00",
            name: "AWS runner",
            cloud_provider: "aws",
            region: "us-east-1",
            status: "pending",
            registered_at: null,
            last_heartbeat_at: null,
            runner_version: null,
            capabilities: {
              storage_profile: "archil_shared_files",
              sqlite_storage: "runner_ebs",
              shared_files_storage: "archil",
            },
            data_dir: null,
          },
          registration_token: "registration-token",
          registration_token_expires_at: "2026-04-25T01:00:00+00:00",
          control_url: "https://yinshi.example.com",
          environment: {
            YINSHI_CONTROL_URL: "https://yinshi.example.com",
            YINSHI_REGISTRATION_TOKEN: "registration-token",
            YINSHI_RUNNER_STORAGE_PROFILE: "archil_shared_files",
            YINSHI_RUNNER_SQLITE_STORAGE: "runner_ebs",
            YINSHI_RUNNER_SHARED_FILES_STORAGE: "archil",
            YINSHI_RUNNER_SQLITE_DIR: "/var/lib/yinshi/sqlite",
            YINSHI_RUNNER_SHARED_FILES_DIR: "/mnt/archil/yinshi",
          },
        });
      }
      throw new Error(`Unexpected POST path: ${path}`);
    });

    render(<Settings />);
    fireEvent.click(screen.getByRole("tab", { name: "Cloud runner" }));
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith("/api/settings/runner"));

    fireEvent.click(screen.getByRole("radio", { name: /Archil shared-files mode/ }));
    expect(screen.getByText(/user-owned backing bucket/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create Token" }));

    await waitFor(() => {
      expect(apiPostMock).toHaveBeenCalledWith("/api/settings/runner", {
        name: "AWS runner",
        cloud_provider: "aws",
        region: "us-east-1",
        storage_profile: "archil_shared_files",
      });
    });
    expect(screen.getByText("Archil POSIX")).toBeInTheDocument();
    expect(screen.getByDisplayValue(/YINSHI_RUNNER_STORAGE_PROFILE=archil_shared_files/)).toBeInTheDocument();
  });

  it("creates an Archil all-POSIX runner with stronger warning copy", async () => {
    apiPostMock.mockImplementation((path: string) => {
      if (path === "/api/settings/runner") {
        return Promise.resolve({
          runner: {
            id: "runner-archil-all-posix",
            created_at: "2026-04-25T00:00:00+00:00",
            updated_at: "2026-04-25T00:00:00+00:00",
            name: "AWS runner",
            cloud_provider: "aws",
            region: "us-east-1",
            status: "pending",
            registered_at: null,
            last_heartbeat_at: null,
            runner_version: null,
            capabilities: {
              storage_profile: "archil_all_posix",
              sqlite_storage: "archil",
              shared_files_storage: "archil",
            },
            data_dir: null,
          },
          registration_token: "registration-token",
          registration_token_expires_at: "2026-04-25T01:00:00+00:00",
          control_url: "https://yinshi.example.com",
          environment: {
            YINSHI_CONTROL_URL: "https://yinshi.example.com",
            YINSHI_REGISTRATION_TOKEN: "registration-token",
            YINSHI_RUNNER_STORAGE_PROFILE: "archil_all_posix",
            YINSHI_RUNNER_SQLITE_STORAGE: "archil",
            YINSHI_RUNNER_SHARED_FILES_STORAGE: "archil",
            YINSHI_RUNNER_SQLITE_DIR: "/mnt/archil/yinshi/sqlite",
            YINSHI_RUNNER_SHARED_FILES_DIR: "/mnt/archil/yinshi",
          },
        });
      }
      throw new Error(`Unexpected POST path: ${path}`);
    });

    render(<Settings />);
    fireEvent.click(screen.getByRole("tab", { name: "Cloud runner" }));
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith("/api/settings/runner"));

    fireEvent.click(screen.getByRole("radio", { name: /Archil all-POSIX mode/ }));
    expect(screen.getByText(/Strong warning: live SQLite on Archil/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create Token" }));

    await waitFor(() => {
      expect(apiPostMock).toHaveBeenCalledWith("/api/settings/runner", {
        name: "AWS runner",
        cloud_provider: "aws",
        region: "us-east-1",
        storage_profile: "archil_all_posix",
      });
    });
    expect(screen.getByText("Archil all-POSIX")).toBeInTheDocument();
    expect(screen.getByDisplayValue(/YINSHI_RUNNER_SQLITE_DIR=\/mnt\/archil\/yinshi\/sqlite/)).toBeInTheDocument();
  });
});
