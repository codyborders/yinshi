import { describe, expect, it, vi } from "vitest";

import { createRuntimeTransport } from "./runtimeTransport";

const runnerPublicKey = "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I";

function apiClient() {
  return {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    upload: vi.fn().mockResolvedValue({ status: "direct" }),
  };
}

describe("runtime transport", () => {
  it("keeps local and hosted JSON calls on their selected gateway", async () => {
    const client = apiClient();
    client.get.mockResolvedValue([{ id: "repo-1" }]);
    const encryptedRequest = vi.fn();
    const transport = createRuntimeTransport(
      { location: "local" },
      { apiClient: client, encryptedRequest },
    );

    await expect(transport.get("/api/repos")).resolves.toEqual([
      { id: "repo-1" },
    ]);
    expect(client.get).toHaveBeenCalledWith("/api/repos");
    expect(encryptedRequest).not.toHaveBeenCalled();
  });

  it("maps BYOC JSON routes to least-privilege encrypted capabilities", async () => {
    const client = apiClient();
    const encryptedRequest = vi.fn().mockResolvedValue([]);
    const transport = createRuntimeTransport(
      { location: "byoc", runnerId: "runner-1", runnerPublicKey },
      { apiClient: client, encryptedRequest },
    );

    await expect(transport.get("/api/repos")).resolves.toEqual([]);
    expect(encryptedRequest).toHaveBeenCalledWith({
      expectedRunnerPublicKey: runnerPublicKey,
      scopes: ["repository.read"],
      method: "GET",
      path: "/api/repos",
      query: {},
      body: null,
      maxSessionBytes: 16_777_216,
    });
    expect(client.get).not.toHaveBeenCalled();
  });

  it("routes managed JSON calls through its pinned encrypted capability endpoint", async () => {
    const client = apiClient();
    const encryptedRequest = vi.fn().mockResolvedValue([]);
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      { apiClient: client, encryptedRequest },
    );

    await expect(transport.get("/api/repos?owner=me")).resolves.toEqual([]);

    expect(encryptedRequest).toHaveBeenCalledWith({
      expectedRunnerPublicKey: runnerPublicKey,
      scopes: ["repository.read"],
      method: "GET",
      path: "/api/repos",
      query: { owner: "me" },
      body: null,
      maxSessionBytes: 16_777_216,
      capabilityEndpoint: "/api/runtime/capabilities",
    });
    expect(client.get).not.toHaveBeenCalled();
  });

  it("keeps managed config uploads off the browser multipart client", async () => {
    const client = apiClient();
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      { apiClient: client, encryptedRequest: vi.fn() },
    );
    const file = {
      name: "large.zip",
      size: 50 * 1024 * 1024 + 1,
    } as File;

    await expect(
      transport.upload("/api/settings/pi-config/upload", file),
    ).rejects.toThrow("50MB");
    expect(client.upload).not.toHaveBeenCalled();
  });

  it("routes BYOC provider OAuth state through encrypted query data", async () => {
    const encryptedRequest = vi.fn().mockResolvedValue({ status: "pending" });
    const transport = createRuntimeTransport(
      { location: "byoc", runnerId: "runner-1", runnerPublicKey },
      { apiClient: apiClient(), encryptedRequest },
    );
    const flowId = "11111111-1111-4111-8111-111111111111";

    await transport.get(
      `/auth/providers/openai-codex/callback?flow_id=${flowId}`,
    );

    expect(encryptedRequest).toHaveBeenCalledWith({
      expectedRunnerPublicKey: runnerPublicKey,
      scopes: ["provider.configure"],
      method: "GET",
      path: "/auth/providers/openai-codex/callback",
      query: { flow_id: flowId },
      body: null,
      maxSessionBytes: 16_777_216,
    });
  });

  it("rejects control-plane and malformed BYOC routes before issuing a capability", async () => {
    const encryptedRequest = vi.fn();
    const transport = createRuntimeTransport(
      { location: "byoc", runnerId: "runner-1", runnerPublicKey },
      { apiClient: apiClient(), encryptedRequest },
    );

    await expect(transport.get("/api/settings/runner")).rejects.toThrow(
      "not allowed",
    );
    await expect(transport.get("/api/repos/../../control")).rejects.toThrow(
      "not allowed",
    );
    expect(encryptedRequest).not.toHaveBeenCalled();
  });

  it("separates workspace and session read and write scopes", async () => {
    const encryptedRequest = vi.fn().mockResolvedValue({});
    const transport = createRuntimeTransport(
      { location: "byoc", runnerId: "runner-1", runnerPublicKey },
      { apiClient: apiClient(), encryptedRequest },
    );
    const resourceId = "a".repeat(32);

    await transport.post(`/api/repos/${resourceId}/workspaces`, {
      name: "feature",
    });
    await transport.get(`/api/sessions/${resourceId}/messages`);

    expect(encryptedRequest).toHaveBeenNthCalledWith(1, {
      expectedRunnerPublicKey: runnerPublicKey,
      scopes: ["workspace.write"],
      method: "POST",
      path: `/api/repos/${resourceId}/workspaces`,
      query: {},
      body: { name: "feature" },
      maxSessionBytes: 16_777_216,
    });
    expect(encryptedRequest).toHaveBeenNthCalledWith(2, {
      expectedRunnerPublicKey: runnerPublicKey,
      scopes: ["session.read"],
      method: "GET",
      path: `/api/sessions/${resourceId}/messages`,
      query: {},
      body: null,
      maxSessionBytes: 16_777_216,
    });
  });
});
