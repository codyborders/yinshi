import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const {
  mockGet,
  mockPost,
  mockPatch,
  mockLogout,
  mockToggleTheme,
} = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockPatch: vi.fn(),
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
    mockLogout.mockReset();
    mockToggleTheme.mockReset();

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
    fireEvent.change(textarea, { target: { value: "Repo runtime instructions" } });
    fireEvent.click(screen.getByRole("button", { name: "Save AGENTS.md" }));

    await waitFor(() =>
      expect(mockPatch).toHaveBeenCalledWith("/api/repos/repo-1", {
        agents_md: "Repo runtime instructions",
      }),
    );
    expect(await screen.findByText("Repo instructions saved.")).toBeTruthy();
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
