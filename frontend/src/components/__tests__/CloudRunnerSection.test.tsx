import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const {
  mockCheckHealth,
  mockDelete,
  mockGet,
  mockImportRunnerRepository,
  mockListRunnerRepositories,
  mockPost,
} = vi.hoisted(() => ({
  mockCheckHealth: vi.fn(),
  mockDelete: vi.fn(),
  mockGet: vi.fn(),
  mockImportRunnerRepository: vi.fn(),
  mockListRunnerRepositories: vi.fn(),
  mockPost: vi.fn(),
}));

vi.mock("../../runner/encryptedRunnerClient", () => ({
  checkEncryptedRunnerHealth: mockCheckHealth,
}));

vi.mock("../../runner/repositories", () => ({
  importRunnerRepository: mockImportRunnerRepository,
  listRunnerRepositories: mockListRunnerRepositories,
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
    mockImportRunnerRepository.mockReset();
    mockListRunnerRepositories.mockReset();
    mockGet.mockResolvedValue(runner(false));
  });

  it("disables replacement while revocation is pending", async () => {
    mockDelete.mockReturnValue(new Promise(() => {}));
    render(<CloudRunnerSection />);

    const replaceButton = await screen.findByRole("button", {
      name: "Replace Runner",
    });
    fireEvent.click(screen.getByRole("button", { name: "Revoke Runner" }));

    expect(replaceButton).toBeDisabled();
  });

  it("keeps replacement token when an older revocation completes later", async () => {
    let resolveDelete: () => void = () => {};
    let resolveCreate: (value: unknown) => void = () => {};
    mockDelete.mockReturnValue(
      new Promise<void>((resolve) => {
        resolveDelete = resolve;
      }),
    );
    mockPost.mockReturnValue(
      new Promise<unknown>((resolve) => {
        resolveCreate = resolve;
      }),
    );
    render(<CloudRunnerSection />);

    const replaceButton = await screen.findByRole("button", {
      name: "Replace Runner",
    });
    const revokeButton = screen.getByRole("button", { name: "Revoke Runner" });
    fireEvent.click(replaceButton);
    revokeButton.removeAttribute("disabled");
    fireEvent.click(revokeButton);

    resolveCreate({
      runner: runner(false),
      registration_token: "replacement-token",
      registration_token_expires_at: "2026-07-11T00:00:00Z",
      control_url: "https://example.com",
      environment: { YINSHI_RUNNER_TOKEN: "replacement-token" },
    });
    expect(await screen.findByDisplayValue("YINSHI_RUNNER_TOKEN=replacement-token")).toBeInTheDocument();

    resolveDelete();
    await waitFor(() => {
      expect(screen.getByDisplayValue("YINSHI_RUNNER_TOKEN=replacement-token")).toBeInTheDocument();
    });
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

  it("lists and imports BYOC repositories through encrypted worker RPC", async () => {
    const repository = {
      id: "repo-1",
      created_at: "2026-07-10T00:00:00Z",
      updated_at: "2026-07-10T00:00:00Z",
      name: "project",
      remote_url: "https://example.com/team/project.git",
      root_path: "/runner/repos/project",
      custom_prompt: null,
      agents_md: null,
    };
    mockGet.mockResolvedValue(runner(true));
    mockListRunnerRepositories.mockResolvedValue([repository]);
    mockImportRunnerRepository.mockResolvedValue(repository);
    render(<CloudRunnerSection />);

    fireEvent.click(await screen.findByRole("button", { name: "Load BYOC repositories" }));
    expect(await screen.findByText("project")).toBeTruthy();
    expect(screen.getByText("BYOC")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Repository name"), {
      target: { value: "project" },
    });
    fireEvent.change(screen.getByLabelText("Repository HTTPS URL"), {
      target: { value: "https://example.com/team/project.git" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Import to BYOC" }));

    await waitFor(() =>
      expect(mockImportRunnerRepository).toHaveBeenCalledWith(
        {
          runnerId: "runner-1",
          runnerPublicKey: "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
        },
        "project",
        "https://example.com/team/project.git",
      ),
    );
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
