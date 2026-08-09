import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockRunnerRequest } = vi.hoisted(() => ({
  mockRunnerRequest: vi.fn(),
}));

vi.mock("./encryptedRunnerClient", () => ({
  requestEncryptedRunner: mockRunnerRequest,
}));

import { importRunnerRepository, listRunnerRepositories } from "./repositories";

const runnerKey = "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I";
const runnerTarget = { runnerId: "runner-1", runnerPublicKey: runnerKey };

describe("runner repository contract", () => {
  beforeEach(() => {
    mockRunnerRequest.mockReset();
  });

  it("lists repositories with read-only authority", async () => {
    mockRunnerRequest.mockResolvedValue([]);

    await expect(listRunnerRepositories(runnerTarget)).resolves.toEqual([]);
    expect(mockRunnerRequest).toHaveBeenCalledWith({
      expectedRunnerPublicKey: runnerKey,
      scopes: ["repository.read"],
      method: "GET",
      path: "/api/repos",
      query: {},
      body: null,
      maxSessionBytes: 262_144,
    });
  });

  it("imports only credential-free HTTPS repository URLs", async () => {
    mockRunnerRequest.mockResolvedValue({
      id: "repo-1",
      created_at: "2026-07-10T00:00:00Z",
      updated_at: "2026-07-10T00:00:00Z",
      name: "project",
      remote_url: "https://example.com/team/project.git",
      root_path: "/runner/repos/project",
      custom_prompt: null,
      agents_md: null,
    });

    await importRunnerRepository(
      runnerTarget,
      "project",
      "https://example.com/team/project.git",
    );
    expect(mockRunnerRequest).toHaveBeenCalledWith({
      expectedRunnerPublicKey: runnerKey,
      scopes: ["repository.write"],
      method: "POST",
      path: "/api/repos",
      query: {},
      body: {
        name: "project",
        remote_url: "https://example.com/team/project.git",
      },
      maxSessionBytes: 262_144,
    });

    await expect(
      importRunnerRepository(runnerTarget, "project", "https://user@example.com/project.git"),
    ).rejects.toThrow("must not include embedded credentials");
    await expect(
      importRunnerRepository(runnerTarget, "project", "file:///private/project"),
    ).rejects.toThrow("HTTPS");
  });
});
