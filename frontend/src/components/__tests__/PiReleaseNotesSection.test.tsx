import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiGetMock = vi.fn();

vi.mock("../../api/client", () => ({
  api: {
    get: (...args: unknown[]) => apiGetMock(...args),
  },
}));

import PiReleaseNotesSection from "../PiReleaseNotesSection";

const releaseNotesPayload = {
  package_name: "@mariozechner/pi-coding-agent",
  installed_version: "0.70.2",
  latest_version: "0.70.2",
  node_version: "v20.20.1",
  release_notes_url: "https://github.com/badlogic/pi-mono/releases",
  update_schedule: "Daily around 04:17 UTC with up to 1 hour randomized delay",
  update_status: {
    checked_at: "2026-04-25T04:30:00Z",
    status: "current",
    previous_version: "0.70.2",
    current_version: "0.70.2",
    latest_version: "0.70.2",
    updated: false,
    message: "@mariozechner/pi-coding-agent is already current",
  },
  runtime_error: null,
  release_error: null,
  releases: [
    {
      tag_name: "v0.70.2",
      version: "0.70.2",
      name: "v0.70.2",
      published_at: "2026-04-24T12:21:42Z",
      html_url: "https://github.com/badlogic/pi-mono/releases/tag/v0.70.2",
      body_markdown: "### Fixed\n\n- Fixed provider retry controls.",
    },
  ],
};

describe("PiReleaseNotesSection", () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiGetMock.mockResolvedValue(releaseNotesPayload);
  });

  it("renders runtime version, update status, and release notes", async () => {
    render(<PiReleaseNotesSection />);

    expect(screen.getByText("Loading pi release notes...")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText("Up to date")).toBeInTheDocument();
    });

    expect(screen.getAllByText("0.70.2").length).toBeGreaterThan(0);
    expect(screen.getByText("Node v20.20.1")).toBeInTheDocument();
    expect(screen.getByText("@mariozechner/pi-coding-agent is already current")).toBeInTheDocument();
    expect(screen.getByText("Fixed provider retry controls.")).toBeInTheDocument();
  });

  it("refreshes release notes on demand", async () => {
    render(<PiReleaseNotesSection />);

    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledTimes(2);
    });
  });
});
