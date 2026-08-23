import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const useAuthMock = vi.fn();
const useCatalogMock = vi.fn();
const apiGetMock = vi.fn();
const apiPostMock = vi.fn();
const apiDeleteMock = vi.fn();
const closeRuntimeTransportMock = vi.fn();

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

vi.mock("../../runtime/runtimeTransport", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("../../runtime/runtimeTransport")
  >();
  return {
    ...actual,
    createRuntimeTransport: (runtime: { location: "hosted" }) => ({
      runtime,
      get: apiGetMock,
      post: apiPostMock,
      patch: vi.fn(),
      put: vi.fn(),
      delete: apiDeleteMock,
      upload: vi.fn(),
      close: closeRuntimeTransportMock,
    }),
  };
});

vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => apiGetMock(...args),
      post: (...args: unknown[]) => apiPostMock(...args),
      delete: (...args: unknown[]) => apiDeleteMock(...args),
    },
  };
});

import Settings from "../Settings";

describe("Settings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    Object.defineProperty(window, "yinshiDesktop", {
      configurable: true,
      value: undefined,
    });
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
      if (path === "/api/runtime") {
        return Promise.resolve({ provider: "local", status: "ready" });
      }
      if (path === "/api/settings/connections") {
        return Promise.resolve([]);
      }
      if (path === "/api/settings/runner") {
        return Promise.resolve(null);
      }
      if (path.startsWith("/auth/providers/openai-codex/callback?")) {
        return new Promise(() => {});
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
          authorization_mode: "browser",
          user_code: null,
          instructions: "Open the browser and sign in.",
          manual_input_required: true,
          manual_input_prompt: "Paste the final redirect URL or authorization code here.",
          manual_input_submitted: false,
        });
      }
      if (path === "/auth/providers/openai-codex/callback") {
        return Promise.resolve({
          status: "pending",
          provider: "openai-codex",
          flow_id: "flow-openai-codex",
          authorization_mode: "browser",
          user_code: null,
          instructions: "Open the browser and sign in.",
          progress: ["Received manual OAuth callback input."],
          manual_input_required: true,
          manual_input_prompt: "Paste the final redirect URL or authorization code here.",
          manual_input_submitted: true,
          error: null,
        });
      }
      throw new Error(`Unexpected POST path: ${path}`);
    });
    vi.spyOn(window, "open").mockImplementation(() => null);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("closes the selected runtime transport when Settings unmounts", async () => {
    const { unmount } = render(<Settings />);
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith("/api/runtime");
    });
    await waitFor(() => {
      expect(closeRuntimeTransportMock).not.toHaveBeenCalled();
      expect(apiGetMock).toHaveBeenCalledWith("/api/settings/connections");
    });

    unmount();

    expect(closeRuntimeTransportMock).toHaveBeenCalledTimes(1);
  });

  it("offers an explicit managed runtime provision action", async () => {
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/api/runtime") {
        return Promise.resolve({
          provider: "fly_sprites",
          status: "absent",
          runner_public_key: null,
        });
      }
      if (path === "/api/settings/runner") return Promise.resolve(null);
      throw new Error(`Unexpected GET path: ${path}`);
    });
    apiPostMock.mockImplementation((path: string) => {
      if (path === "/api/runtime/provision") {
        return Promise.resolve({
          provider: "fly_sprites",
          status: "ready",
          runner_public_key:
            "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
        });
      }
      throw new Error(`Unexpected POST path: ${path}`);
    });

    render(<Settings />);
    const provision = await screen.findByRole("button", {
      name: "Provision managed runtime",
    });
    expect(apiPostMock).not.toHaveBeenCalledWith("/api/runtime/provision", undefined);

    fireEvent.click(provision);

    await waitFor(() => {
      expect(apiPostMock).toHaveBeenCalledWith("/api/runtime/provision");
    });
  });

  it("waits for browser primary runtime resolution before loading execution settings", async () => {
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/api/runtime") return new Promise(() => {});
      if (path === "/api/settings/runner") return Promise.resolve(null);
      if (path === "/api/settings/connections") return Promise.resolve([]);
      throw new Error(`Unexpected GET path: ${path}`);
    });

    render(<Settings />);

    expect(screen.getByText("Loading provider catalog...")).toBeInTheDocument();
    expect(useCatalogMock).not.toHaveBeenCalled();
    expect(apiGetMock).not.toHaveBeenCalledWith("/api/settings/connections");
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
    expect(
      screen.getByText(
        "The localhost error page is expected. Copy its full address, return to Yinshi, paste it below, then submit.",
      ),
    ).toBeInTheDocument();

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
      expect(apiPostMock).toHaveBeenCalledWith(
        "/auth/providers/openai-codex/callback",
        {
          flow_id: "flow-openai-codex",
          authorization_input:
            "http://localhost:1455/auth/callback?code=test-code&state=test-state",
        },
      );
    });

    expect(
      screen.getByText("Waiting for the provider to finish the OAuth flow."),
    ).toBeInTheDocument();
  });

  it("shows managed device authorization without callback input", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    apiPostMock.mockImplementation((path: string) => {
      if (path !== "/auth/providers/openai-codex/start") {
        throw new Error(`Unexpected POST path: ${path}`);
      }
      return Promise.resolve({
        flow_id: "flow-openai-codex",
        provider: "openai-codex",
        auth_url: "https://auth.openai.com/codex/device",
        authorization_mode: "device_code",
        user_code: "TEST-CODE",
        instructions: "Open the verification page and enter the displayed code.",
        manual_input_required: false,
        manual_input_prompt: null,
        manual_input_submitted: false,
      });
    });
    render(<Settings />);

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect Provider" }),
    );

    expect(await screen.findByText("TEST-CODE")).toBeInTheDocument();
    expect(
      screen.queryByPlaceholderText(
        "http://localhost:1455/auth/callback?code=...",
      ),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/The localhost error page is expected/),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith("TEST-CODE");
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Open verification page" }),
    );
    expect(window.open).toHaveBeenLastCalledWith(
      "https://auth.openai.com/codex/device",
      "_blank",
      "noopener,noreferrer",
    );
    expect(apiPostMock).toHaveBeenCalledTimes(1);
    expect(apiPostMock).toHaveBeenCalledWith(
      "/auth/providers/openai-codex/start",
    );
  });

  it("clears a device code when authorization fails", async () => {
    apiPostMock.mockResolvedValue({
      flow_id: "flow-openai-codex",
      provider: "openai-codex",
      auth_url: "https://auth.openai.com/codex/device",
      authorization_mode: "device_code",
      user_code: "TEST-CODE",
      instructions: "Open the verification page and enter the displayed code.",
      manual_input_required: false,
      manual_input_prompt: null,
      manual_input_submitted: false,
    });
    apiGetMock.mockImplementation((path: string) => {
      if (path === "/api/runtime") {
        return Promise.resolve({ provider: "local", status: "ready" });
      }
      if (path === "/api/settings/connections") {
        return Promise.resolve([]);
      }
      if (path === "/api/settings/runner") {
        return Promise.resolve(null);
      }
      if (path.startsWith("/auth/providers/openai-codex/callback?")) {
        return Promise.resolve({
          status: "error",
          provider: "openai-codex",
          flow_id: "flow-openai-codex",
          authorization_mode: "device_code",
          user_code: "TEST-CODE",
          manual_input_required: false,
          manual_input_prompt: null,
          manual_input_submitted: false,
          error: "provider-specific failure",
        });
      }
      throw new Error(`Unexpected GET path: ${path}`);
    });
    render(<Settings />);

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect Provider" }),
    );
    expect(await screen.findByText("TEST-CODE")).toBeInTheDocument();
    expect(await screen.findByText("Provider authorization failed")).toBeInTheDocument();
    expect(screen.queryByText("TEST-CODE")).not.toBeInTheDocument();
    expect(screen.queryByText("provider-specific failure")).not.toBeInTheDocument();
  });

  it("fills manual OAuth callback input from the clipboard without submitting it", async () => {
    const callbackUrl =
      "http://localhost:1455/auth/callback?code=test-code&state=test-state";
    const readText = vi.fn().mockResolvedValue(` ${callbackUrl} `);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { readText },
    });
    render(<Settings />);

    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith("/api/settings/connections");
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect Provider" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Paste from clipboard" }),
    );

    await waitFor(() => {
      expect(readText).toHaveBeenCalledTimes(1);
      expect(
        screen.getByPlaceholderText(
          "http://localhost:1455/auth/callback?code=...",
        ),
      ).toHaveValue(callbackUrl);
    });
    expect(apiPostMock).not.toHaveBeenCalledWith(
      "/auth/providers/openai-codex/callback",
      expect.anything(),
    );
  });

  it("preserves newer manual input when clipboard access finishes later", async () => {
    let finishClipboardRead: ((value: string) => void) | undefined;
    const readText = vi.fn(
      () =>
        new Promise<string>((resolve) => {
          finishClipboardRead = resolve;
        }),
    );
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { readText },
    });
    render(<Settings />);

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect Provider" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Paste from clipboard" }),
    );
    const textarea = screen.getByPlaceholderText(
      "http://localhost:1455/auth/callback?code=...",
    );
    fireEvent.change(textarea, {
      target: { value: "newer-manual-code" },
    });
    await act(async () => {
      finishClipboardRead?.("older-clipboard-code");
      await Promise.resolve();
    });

    expect(readText).toHaveBeenCalledTimes(1);
    expect(textarea).toHaveValue("newer-manual-code");
  });

  it("ignores a delayed clipboard rejection after manual input", async () => {
    let rejectClipboardRead: ((reason?: unknown) => void) | undefined;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        readText: vi.fn(
          () =>
            new Promise<string>((_resolve, reject) => {
              rejectClipboardRead = reject;
            }),
        ),
      },
    });
    render(<Settings />);

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect Provider" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Paste from clipboard" }),
    );
    fireEvent.change(
      screen.getByPlaceholderText(
        "http://localhost:1455/auth/callback?code=...",
      ),
      { target: { value: "newer-manual-code" } },
    );
    await act(async () => {
      rejectClipboardRead?.(new Error("denied"));
      await Promise.resolve();
    });

    expect(
      screen.queryByText(
        "Clipboard access was denied. Paste the address manually.",
      ),
    ).not.toBeInTheDocument();
  });

  it("ignores delayed clipboard input after manual callback submission", async () => {
    let finishClipboardRead: ((value: string) => void) | undefined;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        readText: vi.fn(
          () =>
            new Promise<string>((resolve) => {
              finishClipboardRead = resolve;
            }),
        ),
      },
    });
    render(<Settings />);

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect Provider" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Paste from clipboard" }),
    );
    const textarea = screen.getByPlaceholderText(
      "http://localhost:1455/auth/callback?code=...",
    );
    fireEvent.change(textarea, {
      target: {
        value:
          "http://localhost:1455/auth/callback?code=manual-code&state=manual-state",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit Callback URL" }));
    await screen.findByText("Waiting for the provider to finish the OAuth flow.");

    await act(async () => {
      finishClipboardRead?.("older-clipboard-code");
      await Promise.resolve();
    });

    expect(textarea).toHaveValue("");
  });

  it("keeps hosted callback helpers out of the desktop flow", async () => {
    Object.defineProperty(window, "yinshiDesktop", {
      configurable: true,
      value: {
        fileVaultStatus: vi.fn().mockResolvedValue({ status: "enabled" }),
        listProfiles: vi.fn().mockResolvedValue([]),
      },
    });
    render(<Settings />);

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect Provider" }),
    );

    expect(
      await screen.findByPlaceholderText(
        "http://localhost:1455/auth/callback?code=...",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        "The localhost error page is expected. Copy its full address, return to Yinshi, paste it below, then submit.",
      ),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Paste from clipboard" }),
    ).not.toBeInTheDocument();
  });

  it("switches between settings tabs", async () => {
    render(<Settings />);

    fireEvent.click(screen.getByRole("tab", { name: "Cloud runner" }));
    expect(screen.getByRole("heading", { name: "Cloud Runner" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Pi config" }));
    expect(await screen.findByTestId("pi-config-section")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Pi release notes" }));
    expect(await screen.findByTestId("pi-release-notes-section")).toBeInTheDocument();
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
    screen.getAllByText("Supported").forEach((badge) => {
      expect(badge).toHaveClass("text-emerald-950");
    });
    screen.getAllByText("Experimental").forEach((badge) => {
      expect(badge).toHaveClass("text-amber-950");
    });
    expect(screen.getByText(/not certified full Git and Pi workloads/)).toHaveClass(
      "text-amber-950",
    );
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
