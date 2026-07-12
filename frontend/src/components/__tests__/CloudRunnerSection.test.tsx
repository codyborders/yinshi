import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockCheckHealth, mockDelete, mockGet, mockPost } = vi.hoisted(() => ({
  mockCheckHealth: vi.fn(),
  mockDelete: vi.fn(),
  mockGet: vi.fn(),
  mockPost: vi.fn(),
}));

vi.mock("../../runner/encryptedRunnerClient", () => ({
  checkEncryptedRunnerHealth: mockCheckHealth,
}));

vi.mock("../../api/client", () => ({  api: {
    delete: mockDelete,
    get: mockGet,
    post: mockPost,
  },
}));

import CloudRunnerSection from "../CloudRunnerSection";

const fingerprint =
  "SHA256:33c654429b9e72816f49ff72f238d0e32ea926184707759a0c38a84a7e1b4c9d";

function runner(noiseKeyConfirmed: boolean) {
  return {
    id: "runner-1",
    created_at: "2026-07-10T00:00:00Z",
    updated_at: "2026-07-10T00:00:00Z",
    name: "Private runner",
    cloud_provider: "aws",
    region: "us-west-2",
    status: "online",
    registered_at: "2026-07-10T00:00:00Z",
    last_heartbeat_at: "2026-07-10T00:00:00Z",
    runner_version: "0.2.0",
    capabilities: { storage_profile: "aws_ebs_s3_files" },
    data_dir: "/var/lib/yinshi",
    noise_public_key: "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
    noise_key_fingerprint: fingerprint,
    noise_key_confirmed: noiseKeyConfirmed,
  };
}

describe("CloudRunnerSection pairing", () => {
  beforeEach(() => {
    mockDelete.mockReset();
    mockGet.mockReset();
    mockPost.mockReset();
    mockCheckHealth.mockReset();
    mockGet.mockResolvedValue(runner(false));
  });

  it("requires explicit confirmation of the displayed runner fingerprint", async () => {
    mockPost.mockResolvedValue(runner(true));
    render(<CloudRunnerSection />);

    expect(await screen.findByText(fingerprint)).toBeTruthy();
    expect(screen.getByText("Encrypted sessions are blocked until pairing is complete.")).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "I verified this fingerprint" }),
    );

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith(
        "/api/settings/runner/noise-key/confirm",
        { noise_public_key: "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I" },
      ),
    );
    expect(await screen.findByText("Runner identity paired")).toBeTruthy();
  });

  it("checks worker health through the encrypted runner channel", async () => {
    mockGet.mockResolvedValue(runner(true));
    mockCheckHealth.mockResolvedValue({ protocol: "yinshi-runner-v1", status: "ok" });
    render(<CloudRunnerSection />);

    fireEvent.click(await screen.findByRole("button", { name: "Test encrypted connection" }));

    await waitFor(() =>
      expect(mockCheckHealth).toHaveBeenCalledWith(
        "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
      ),
    );
    expect(await screen.findByText("Encrypted runner connection is healthy.")).toBeTruthy();
  });
});
