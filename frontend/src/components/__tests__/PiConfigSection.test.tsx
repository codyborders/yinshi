import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const usePiConfigMock = vi.fn();

vi.mock("../../hooks/usePiConfig", () => ({
  usePiConfig: () => usePiConfigMock(),
}));

import PiConfigSection from "../PiConfigSection";

describe("PiConfigSection", () => {
  it("renders upload and github tabs when no config exists", () => {
    usePiConfigMock.mockReturnValue({
      config: null,
      loading: false,
      error: null,
      importing: false,
      syncing: false,
      importFromGithub: vi.fn(),
      importFromUpload: vi.fn(),
      loadConfig: vi.fn(),
      syncConfig: vi.fn(),
      removeConfig: vi.fn(),
      toggleCategory: vi.fn(),
    });

    render(<PiConfigSection />);

    expect(screen.getByText("Pi Agent Configuration")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Upload" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "GitHub" })).toBeTruthy();
  });

  it("renders toggles for available categories when config is ready", () => {
    const toggleCategory = vi.fn();
    usePiConfigMock.mockReturnValue({
      config: {
        id: "cfg-1",
        created_at: "2026-03-20T12:00:00Z",
        updated_at: "2026-03-20T12:00:00Z",
        source_type: "github",
        source_label: "example/repo",
        last_synced_at: "2026-03-20T12:00:00Z",
        status: "ready",
        error_message: null,
        available_categories: ["skills", "settings"],
        enabled_categories: ["skills"],
      },
      loading: false,
      error: null,
      importing: false,
      syncing: false,
      importFromGithub: vi.fn(),
      importFromUpload: vi.fn(),
      loadConfig: vi.fn(),
      syncConfig: vi.fn(),
      removeConfig: vi.fn(),
      toggleCategory,
    });

    render(<PiConfigSection />);
    const toggles = screen.getAllByRole("checkbox");
    expect(toggles).toHaveLength(2);

    fireEvent.click(toggles[1]);
    expect(toggleCategory).toHaveBeenCalledWith("settings", true);
  });
});
