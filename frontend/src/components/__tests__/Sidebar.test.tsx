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
} = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockPatch: vi.fn(),
  mockListRunnerRepositories: vi.fn(),
  mockLogout: vi.fn(),
  mockToggleTheme: vi.fn(),
}));

vi.mock("../../hooks/useAuth", () => ({
  useAuth: vi.fn(() => ({
    status: "authenticated",
    email: "u@t.com",
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
    mockGet.mockReset();
    mockPost.mockReset();
    mockPatch.mockReset();
    mockListRunnerRepositories.mockReset();
    mockLogout.mockReset();
    mockToggleTheme.mockReset();
    delete (window as { yinshiDesktop?: YinshiDesktopBridge }).yinshiDesktop;

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
