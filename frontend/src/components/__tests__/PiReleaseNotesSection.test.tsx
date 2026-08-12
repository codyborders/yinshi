import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeTransport } from "../../runtime/runtimeTransport";

const apiGetMock = vi.fn();

vi.mock("../../api/client", () => ({
  api: {
    get: (...args: unknown[]) => apiGetMock(...args),
  },
}));

import PiReleaseNotesSection from "../PiReleaseNotesSection";

const transport: RuntimeTransport = {
  runtime: { location: "hosted" },
  get: (...args) => apiGetMock(...args),
  post: vi.fn(),
  patch: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
  upload: vi.fn(),
  close: vi.fn(),
};

const releaseNotesPayload = {
  package_name: "@earendil-works/pi-coding-agent",
  installed_version: "0.80.6",
  latest_version: "0.80.6",
  node_version: "v22.19.0",
  release_notes_url: "https://github.com/earendil-works/pi/releases",
  update_policy: "Updated through reviewed lockfile deployments",
  runtime_error: null,
  release_error: null,
  releases: [
    {
      tag_name: "v0.80.6",
      version: "0.80.6",
      name: "v0.80.6",
      published_at: "2026-04-24T12:21:42Z",
      html_url: "https://github.com/earendil-works/pi/releases/tag/v0.80.6",
      body_markdown: "### Fixed\n\n- Fixed provider retry controls.",
    },
  ],
};

describe("PiReleaseNotesSection", () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiGetMock.mockResolvedValue(releaseNotesPayload);
  });

  it("renders runtime version, deployment policy, and release notes", async () => {
    render(<PiReleaseNotesSection transport={transport} />);

    expect(screen.getByText("Loading pi release notes...")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText("Up to date")).toBeInTheDocument();
    });

    expect(screen.getAllByText("0.80.6").length).toBeGreaterThan(0);
    expect(screen.getByText("Node v22.19.0")).toBeInTheDocument();
    expect(screen.getByText("Updated through reviewed lockfile deployments")).toBeInTheDocument();
    expect(screen.getByText("Fixed provider retry controls.")).toBeInTheDocument();
  });

  it("refreshes release notes on demand", async () => {
    render(<PiReleaseNotesSection transport={transport} />);

    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledTimes(2);
    });
  });
});
