import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const {
  mockGet,
  mockPost,
  mockPatch,
  mockListRunnerRepositories,
  mockLogout,
  mockToggleTheme,
  mockResolveRuntimeRef,
  mockCreateRuntimeTransport,
} = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockPatch: vi.fn(),
  mockListRunnerRepositories: vi.fn(),
  mockLogout: vi.fn(),
  mockToggleTheme: vi.fn(),
  mockResolveRuntimeRef: vi.fn(),
  mockCreateRuntimeTransport: vi.fn(),
}));

vi.mock("../../hooks/useAuth", () => ({
  useAuth: vi.fn(() => ({
    status: "authenticated",
    email: "u@t.com",
    userId: "user-1",
    logout: mockLogout,
  })),
}));

vi.mock("../../hooks/useTheme", () => ({
  useTheme: vi.fn(() => ({
    theme: "dark",
    toggle: mockToggleTheme,
  })),
}));

vi.mock("../../runner/repositories", () => ({
  listRunnerRepositories: mockListRunnerRepositories,
}));

vi.mock("../../runtime/resolveRuntime", () => ({
  resolveRuntimeRef: mockResolveRuntimeRef,
}));

vi.mock("../../runtime/runtimeTransport", () => ({
  createRuntimeTransport: mockCreateRuntimeTransport,
}));

vi.mock("../../api/client", () => ({
  ApiError: class extends Error {},
  api: {
    get: mockGet,
    post: mockPost,
    patch: mockPatch,
  },
}));

import Sidebar from "../Sidebar";

function renderSidebar() {
  return render(
    <MemoryRouter initialEntries={["/app"]}>
      <Routes>
        <Route path="/app" element={<Sidebar />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Sidebar repo settings", () => {
  beforeEach(() => {
    localStorage.clear();
    mockGet.mockReset();
    mockPost.mockReset();
    mockPatch.mockReset();
    mockListRunnerRepositories.mockReset();
    mockLogout.mockReset();
    mockToggleTheme.mockReset();
    mockResolveRuntimeRef.mockReset();
    mockCreateRuntimeTransport.mockReset();
    delete (window as { yinshiDesktop?: YinshiDesktopBridge }).yinshiDesktop;

    mockResolveRuntimeRef.mockImplementation(async (runtime) => runtime);
    mockCreateRuntimeTransport.mockImplementation((runtime) => ({
      runtime,
      get: mockGet,
      post: mockPost,
      patch: mockPatch,
      put: vi.fn(),
      delete: vi.fn(),
      upload: vi.fn(),
      close: vi.fn(),
    }));
    mockGet.mockImplementation(async (path: string) => {
      if (path === "/api/repos") {
        return [
          {
            id: "repo-1",
            created_at: "2026-04-12T00:00:00Z",
            updated_at: "2026-04-12T00:00:00Z",
            name: "demo-repo",
            remote_url: null,
            root_path: "/tmp/demo-repo",
            custom_prompt: null,
            agents_md: null,
          },
        ];
      }
      if (path === "/api/github/installations") {
        return [];
      }
      if (path === "/api/repos/repo-1/workspaces") {
        return [];
      }
      throw new Error(`Unexpected GET ${path}`);
    });
  });

  it("groups delegated workspaces separately from primary workspaces", async () => {
    mockGet.mockImplementation(async (path: string) => {
      if (path === "/api/repos") {
        return [{
          id: "repo-1",
          created_at: "2026-04-12T00:00:00Z",
          updated_at: "2026-04-12T00:00:00Z",
          name: "demo-repo",
          remote_url: null,
          root_path: "/tmp/demo-repo",
          custom_prompt: null,
          agents_md: null,
        }];
      }
      if (path === "/api/github/installations") return [];
      if (path === "/api/repos/repo-1/workspaces") {
        return [
          {
            id: "workspace-primary",
            created_at: "2026-04-12T00:00:00Z",
            updated_at: "2026-04-12T00:00:00Z",
            repo_id: "repo-1",
            name: "main work",
            branch: "main-work",
            path: "/tmp/primary",
            state: "ready",
            kind: "primary",
            parent_workspace_id: null,
            delegation_id: null,
            delegation_status: null,
          },
          {
            id: "workspace-child",
            created_at: "2026-04-12T00:01:00Z",
            updated_at: "2026-04-12T00:01:00Z",
            repo_id: "repo-1",
            name: "parser check",
            branch: "yinshi/thread/parser-check",
            path: "/tmp/child",
            state: "ready",
            kind: "delegated",
            parent_workspace_id: "workspace-primary",
            delegation_id: "delegation-1",
            delegation_status: "running",
          },
        ];
      }
      if (path.startsWith("/api/workspaces/")) return [];
      throw new Error(`Unexpected GET ${path}`);
    });

    renderSidebar();

    expect(await screen.findByText("main work")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Delegated/ })).toBeInTheDocument();
    expect(screen.getByText("parser check")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("resolves the browser primary runtime before loading repositories through its transport", async () => {
    let finishResolution:
      | ((runtime: { location: "managed"; runnerPublicKey: string }) => void)
      | undefined;
    const transportGet = vi.fn().mockImplementation(async (path: string) => {
      if (path === "/api/repos") return [];
      throw new Error(`Unexpected transport GET ${path}`);
    });
    mockCreateRuntimeTransport.mockImplementation((runtime) => ({
      runtime,
      get: transportGet,
      post: mockPost,
      patch: mockPatch,
      put: vi.fn(),
      delete: vi.fn(),
      upload: vi.fn(),
    }));
    mockResolveRuntimeRef.mockReturnValue(
      new Promise((resolve) => {
        finishResolution = resolve;
      }),
    );

    renderSidebar();

    expect(mockGet).not.toHaveBeenCalledWith("/api/repos");
    expect(mockCreateRuntimeTransport).not.toHaveBeenCalled();

    const resolvedPrimary = {
      location: "managed" as const,
      runnerPublicKey: "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
    };
    finishResolution?.(resolvedPrimary);

    await waitFor(() => {
      expect(mockCreateRuntimeTransport).toHaveBeenCalledWith(resolvedPrimary);
      expect(transportGet).toHaveBeenCalledWith("/api/repos");
    });
    expect(mockGet).not.toHaveBeenCalledWith("/api/repos");
    expect(mockResolveRuntimeRef).toHaveBeenCalledWith({ location: "hosted" });
  });

  it("closes the temporary desktop transport after installation loading", async () => {
    const close = vi.fn();
    Object.defineProperty(window, "yinshiDesktop", {
      configurable: true,
      value: { signOut: vi.fn() },
    });
    mockCreateRuntimeTransport.mockImplementation((runtime) => ({
      runtime,
      get: mockGet,
      post: mockPost,
      patch: mockPatch,
      put: vi.fn(),
      delete: vi.fn(),
      upload: vi.fn(),
      close,
    }));

    renderSidebar();

    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledWith("/api/github/installations");
    });
    expect(close).toHaveBeenCalled();
  });

  it("keeps the desktop title clear of macOS window controls", () => {
    Object.defineProperty(window, "yinshiDesktop", {
      configurable: true,
      value: { signOut: vi.fn() },
    });

    renderSidebar();

    expect(screen.getByText("Workspaces").parentElement?.className).toContain(
      "pl-24",
    );
  });

  it("ignores a revoked BYOC runner", async () => {
    mockGet.mockImplementation(async (path: string) => {
      if (path === "/api/repos") return [];
      if (path === "/api/github/installations") return [];
      if (path === "/api/settings/runner") {
        return {
          id: "runner-1",
          status: "revoked",
          noise_public_key: "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
          noise_key_confirmed: true,
        };
      }
      throw new Error(`Unexpected GET ${path}`);
    });
    mockListRunnerRepositories.mockResolvedValue([]);

    renderSidebar();

    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith("/api/settings/runner"),
    );
    expect(mockListRunnerRepositories).not.toHaveBeenCalled();
    expect(screen.queryByText("BYOC")).toBeNull();
  });

  it("aggregates paired BYOC repositories with explicit location labels", async () => {
    mockGet.mockImplementation(async (path: string) => {
      if (path === "/api/repos") return [];
      if (path === "/api/github/installations") return [];
      if (path === "/api/settings/runner") {
        return {
          id: "runner-1",
          status: "online",
          noise_public_key: "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
          noise_key_confirmed: true,
        };
      }
      throw new Error(`Unexpected GET ${path}`);
    });
    mockListRunnerRepositories.mockResolvedValue([
      {
        id: "a".repeat(32),
        created_at: "2026-07-10T00:00:00Z",
        updated_at: "2026-07-10T00:00:00Z",
        name: "remote-project",
        remote_url: "https://example.com/team/project.git",
        root_path: "/runner/repos/project",
        custom_prompt: null,
        agents_md: null,
      },
    ]);

    renderSidebar();

    expect(await screen.findByText("remote-project")).toBeTruthy();
    expect(screen.getByText("BYOC")).toBeTruthy();
    expect(mockListRunnerRepositories).toHaveBeenCalledWith({
      runnerId: "runner-1",
      runnerPublicKey: "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
    });
  });

  it("creates only one session when a workspace is double-clicked", async () => {
    mockGet.mockImplementation(async (path: string) => {
      if (path === "/api/repos") {
        return [{
          id: "repo-1",
          created_at: "2026-04-12T00:00:00Z",
          updated_at: "2026-04-12T00:00:00Z",
          name: "demo-repo",
          remote_url: null,
          root_path: "/tmp/demo-repo",
          custom_prompt: null,
          agents_md: null,
        }];
      }
      if (path === "/api/github/installations") return [];
      if (path === "/api/repos/repo-1/workspaces") return [{
        id: "workspace-1",
        created_at: "2026-08-18T00:00:00Z",
        updated_at: "2026-08-18T00:00:00Z",
        repo_id: "repo-1",
        name: "steady-river",
        branch: "steady-river",
        path: "/tmp/steady-river",
        state: "ready",
      }];
      if (path === "/api/workspaces/workspace-1/sessions") return [];
      throw new Error(`Unexpected GET ${path}`);
    });
    mockPost.mockReturnValue(new Promise(() => {}));

    renderSidebar();

    const workspaceButton = (await screen.findByText("steady-river")).closest("button");
    if (!workspaceButton) throw new Error("Workspace button was not found");
    fireEvent.click(workspaceButton);
    fireEvent.click(workspaceButton);

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
  });

  it("creates a new worktree session with the user's latest remembered model", async () => {
    mockPost.mockImplementation(async (path: string) => {
      if (path === "/api/repos/repo-1/workspaces") {
        return {
          id: "workspace-1",
          created_at: "2026-08-18T00:00:00Z",
          updated_at: "2026-08-18T00:00:00Z",
          repo_id: "repo-1",
          name: "steady-river",
          branch: "steady-river",
          path: "/tmp/steady-river",
          state: "ready",
        };
      }
      if (path === "/api/workspaces/workspace-1/sessions") {
        return {
          id: "b".repeat(32),
          created_at: "2026-08-18T00:00:00Z",
          updated_at: "2026-08-18T00:00:00Z",
          workspace_id: "workspace-1",
          status: "idle",
          model: "openai-codex/gpt-5.6-sol",
          pi_context_version: 1,
        };
      }
      throw new Error(`Unexpected POST ${path}`);
    });

    renderSidebar();

    await screen.findByText("demo-repo");
    localStorage.setItem(
      "yinshi:last-session-model:user-1",
      "openai-codex/gpt-5.6-sol",
    );
    fireEvent.click(screen.getByTitle("New branch"));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(
        "/api/workspaces/workspace-1/sessions",
        { model: "openai-codex/gpt-5.6-sol" },
      );
    });
  });

  it("uses the remembered model when an existing worktree has no session", async () => {
    localStorage.setItem(
      "yinshi:last-session-model:user-1",
      "openai-codex/gpt-5.6-sol",
    );
    mockGet.mockImplementation(async (path: string) => {
      if (path === "/api/repos") {
        return [
          {
            id: "repo-1",
            created_at: "2026-04-12T00:00:00Z",
            updated_at: "2026-04-12T00:00:00Z",
            name: "demo-repo",
            remote_url: null,
            root_path: "/tmp/demo-repo",
            custom_prompt: null,
            agents_md: null,
          },
        ];
      }
      if (path === "/api/github/installations") return [];
      if (path === "/api/repos/repo-1/workspaces") {
        return [
          {
            id: "workspace-1",
            created_at: "2026-08-18T00:00:00Z",
            updated_at: "2026-08-18T00:00:00Z",
            repo_id: "repo-1",
            name: "steady-river",
            branch: "steady-river",
            path: "/tmp/steady-river",
            state: "ready",
          },
        ];
      }
      if (path === "/api/workspaces/workspace-1/sessions") return [];
      throw new Error(`Unexpected GET ${path}`);
    });
    mockPost.mockResolvedValue({
      id: "b".repeat(32),
      created_at: "2026-08-18T00:00:00Z",
      updated_at: "2026-08-18T00:00:00Z",
      workspace_id: "workspace-1",
      status: "idle",
      model: "openai-codex/gpt-5.6-sol",
      pi_context_version: 1,
    });

    renderSidebar();

    fireEvent.click(await screen.findByText("steady-river"));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(
        "/api/workspaces/workspace-1/sessions",
        { model: "openai-codex/gpt-5.6-sol" },
      );
    });
  });

  it("saves a repo AGENTS.md override", async () => {
    mockPatch.mockResolvedValue({
      id: "repo-1",
      created_at: "2026-04-12T00:00:00Z",
      updated_at: "2026-04-12T00:01:00Z",
      name: "demo-repo",
      remote_url: null,
      root_path: "/tmp/demo-repo",
      custom_prompt: null,
      agents_md: "Repo runtime instructions",
    });

    renderSidebar();

    await screen.findByText("demo-repo");
    fireEvent.click(screen.getByTitle("Repo settings"));

    const textarea = screen.getByLabelText("AGENTS.md override");
    fireEvent.change(textarea, {
      target: { value: "Repo runtime instructions" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save AGENTS.md" }));

    await waitFor(() =>
      expect(mockPatch).toHaveBeenCalledWith("/api/repos/repo-1", {
        agents_md: "Repo runtime instructions",
      }),
    );
    expect(await screen.findByText("Repo instructions saved.")).toBeTruthy();
  });

  it("imports a local path through the hosted development backend", async () => {
    mockGet.mockImplementation(async (path: string) => {
      if (path === "/api/repos") return [];
      if (path === "/api/github/installations") return [];
      throw new Error(`Unexpected GET ${path}`);
    });
    mockPost.mockResolvedValue({
      id: "repo-2",
      created_at: "2026-07-10T00:00:00Z",
      updated_at: "2026-07-10T00:00:00Z",
      name: "local-repo",
      remote_url: null,
      root_path: "/tmp/local-repo",
      custom_prompt: null,
      agents_md: null,
    });

    renderSidebar();
    fireEvent.click(await screen.findByTitle("Add repository"));
    fireEvent.change(
      screen.getByPlaceholderText("GitHub URL, user/repo, or local path"),
      { target: { value: "/tmp/local-repo" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Import" }));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/api/repos", {
        name: "local-repo",
        local_path: "/tmp/local-repo",
      }),
    );
  });

  it("imports a desktop-selected repository without exposing a local path", async () => {
    const importLocalRepository = vi.fn().mockResolvedValue({
      status: "imported",
      repository: { id: "repo-2", name: "local-repo" },
    });
    Object.defineProperty(window, "yinshiDesktop", {
      configurable: true,
      value: { importLocalRepository, signOut: vi.fn() },
    });
    mockGet.mockImplementation(async (path: string) => {
      if (path === "/api/repos") return [];
      if (path === "/api/github/installations") return [];
      if (path === "/api/repos/repo-2") {
        return {
          id: "repo-2",
          created_at: "2026-07-10T00:00:00Z",
          updated_at: "2026-07-10T00:00:00Z",
          name: "local-repo",
          remote_url: null,
          root_path: "/private/path-must-not-cross-ipc",
          custom_prompt: null,
          agents_md: null,
        };
      }
      throw new Error(`Unexpected GET ${path}`);
    });

    renderSidebar();
    fireEvent.click(await screen.findByTitle("Add repository"));
    fireEvent.click(
      screen.getByRole("button", { name: "Choose local repository" }),
    );

    await waitFor(() => expect(importLocalRepository).toHaveBeenCalledTimes(1));
    expect(mockPost).not.toHaveBeenCalled();
    expect(await screen.findByText("local-repo")).toBeTruthy();
    delete (window as { yinshiDesktop?: YinshiDesktopBridge }).yinshiDesktop;
  });

  it("opens the repo settings editor from a collapsed repo", async () => {
    renderSidebar();

    const repoLabel = await screen.findByText("demo-repo");
    const repoButton = repoLabel.closest("button");

    expect(repoButton).toBeTruthy();
    fireEvent.click(repoButton!);
    fireEvent.click(screen.getByTitle("Repo settings"));

    expect(await screen.findByLabelText("AGENTS.md override")).toBeTruthy();
  });

  it("clears a repo AGENTS.md override by saving an empty value", async () => {
    mockGet.mockImplementation(async (path: string) => {
      if (path === "/api/repos") {
        return [
          {
            id: "repo-1",
            created_at: "2026-04-12T00:00:00Z",
            updated_at: "2026-04-12T00:00:00Z",
            name: "demo-repo",
            remote_url: null,
            root_path: "/tmp/demo-repo",
            custom_prompt: null,
            agents_md: "Existing repo instructions",
          },
        ];
      }
      if (path === "/api/github/installations") {
        return [];
      }
      if (path === "/api/repos/repo-1/workspaces") {
        return [];
      }
      throw new Error(`Unexpected GET ${path}`);
    });

    mockPatch.mockResolvedValue({
      id: "repo-1",
      created_at: "2026-04-12T00:00:00Z",
      updated_at: "2026-04-12T00:01:00Z",
      name: "demo-repo",
      remote_url: null,
      root_path: "/tmp/demo-repo",
      custom_prompt: null,
      agents_md: null,
    });

    renderSidebar();

    await screen.findByText("demo-repo");
    fireEvent.click(screen.getByTitle("Repo settings"));

    const textarea = screen.getByLabelText("AGENTS.md override");
    fireEvent.change(textarea, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save AGENTS.md" }));

    await waitFor(() =>
      expect(mockPatch).toHaveBeenCalledWith("/api/repos/repo-1", {
        agents_md: null,
      }),
    );
  });
});
