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
    const connection = {
      request: vi.fn().mockResolvedValue([]),
      close: vi.fn(),
    };
    const connectEncrypted = vi.fn().mockResolvedValue(connection);
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      { apiClient: client, encryptedRequest: vi.fn(), connectEncrypted },
    );

    await expect(transport.get("/api/repos?owner=me")).resolves.toEqual([]);

    expect(connectEncrypted).toHaveBeenCalledWith({
      expectedRunnerPublicKey: runnerPublicKey,
      scopes: ["repository.read"],
      maxSessionBytes: 16_777_216,
      capabilityEndpoint: "/api/runtime/capabilities",
    });
    expect(connection.request).toHaveBeenCalledWith({
      method: "GET",
      path: "/api/repos",
      query: { owner: "me" },
      body: null,
    });
    expect(client.get).not.toHaveBeenCalled();
  });

  it("keeps managed OAuth callback input inside the encrypted connection", async () => {
    const client = apiClient();
    const connection = {
      request: vi.fn().mockResolvedValue({ status: "pending" }),
      close: vi.fn(),
    };
    const connectEncrypted = vi.fn().mockResolvedValue(connection);
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      { apiClient: client, encryptedRequest: vi.fn(), connectEncrypted },
    );
    const callback = {
      flow_id: "11111111-1111-4111-8111-111111111111",
      authorization_input:
        "http://localhost:1455/auth/callback?code=test-code&state=test-state",
    };

    await transport.post("/auth/providers/openai-codex/callback", callback);

    expect(connectEncrypted).toHaveBeenCalledWith({
      expectedRunnerPublicKey: runnerPublicKey,
      scopes: ["provider.configure"],
      maxSessionBytes: 16_777_216,
      capabilityEndpoint: "/api/runtime/capabilities",
    });
    expect(connection.request).toHaveBeenCalledWith({
      method: "POST",
      path: "/auth/providers/openai-codex/callback",
      query: {},
      body: callback,
    });
    expect(client.post).not.toHaveBeenCalled();
  });

  it("reuses one managed encrypted connection for sequential same-scope requests", async () => {
    const connection = {
      request: vi.fn().mockResolvedValue([]),
      close: vi.fn(),
    };
    const connectEncrypted = vi.fn().mockResolvedValue(connection);
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
      },
    );

    await transport.get("/api/repos");
    await transport.get(`/api/repos/${"a".repeat(32)}`);

    expect(connectEncrypted).toHaveBeenCalledTimes(1);
    expect(connection.request).toHaveBeenCalledTimes(2);
    transport.close();
    await Promise.resolve();
    expect(connection.close).toHaveBeenCalledTimes(1);
  });

  it("serializes concurrent managed requests on one scoped connection", async () => {
    let activeRequests = 0;
    let releaseFirst: (() => void) | undefined;
    const firstReleased = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    const request = vi.fn().mockImplementation(async () => {
      activeRequests += 1;
      if (activeRequests > 1) {
        throw new Error("requests overlapped");
      }
      await firstReleased;
      activeRequests -= 1;
      return [];
    });
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted: vi.fn().mockResolvedValue({
          request,
          close: vi.fn(),
        }),
      },
    );

    const first = transport.get("/api/repos");
    const second = transport.get(`/api/repos/${"a".repeat(32)}`);
    await Promise.resolve();
    releaseFirst?.();

    await expect(Promise.all([first, second])).resolves.toEqual([[], []]);
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("closes every managed connection when the transport closes", async () => {
    const connections = [
      { request: vi.fn().mockResolvedValue([]), close: vi.fn() },
      { request: vi.fn().mockResolvedValue({}), close: vi.fn() },
    ];
    const connectEncrypted = vi
      .fn()
      .mockResolvedValueOnce(connections[0])
      .mockResolvedValueOnce(connections[1]);
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
      },
    );
    await transport.get("/api/repos");
    await transport.post(`/api/repos/${"a".repeat(32)}/workspaces`, {});

    transport.close();
    await Promise.resolve();

    expect(connections[0].close).toHaveBeenCalledTimes(1);
    expect(connections[1].close).toHaveBeenCalledTimes(1);
    await expect(transport.get("/api/repos")).rejects.toThrow(
      "Runtime transport is closed",
    );
  });

  it("evicts a connection after its capability expires", async () => {
    let nowMs = 1_000;
    const firstConnection = {
      request: vi.fn().mockResolvedValue([]),
      close: vi.fn(),
      expiresAtMs: 2_000,
    };
    const secondConnection = {
      request: vi.fn().mockResolvedValue([]),
      close: vi.fn(),
      expiresAtMs: 4_000,
    };
    const connectEncrypted = vi
      .fn()
      .mockResolvedValueOnce(firstConnection)
      .mockResolvedValueOnce(secondConnection);
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
        now: () => nowMs,
      },
    );

    await transport.get("/api/repos");
    nowMs = 2_001;
    await transport.get("/api/repos");

    expect(connectEncrypted).toHaveBeenCalledTimes(2);
    expect(firstConnection.close).toHaveBeenCalledTimes(1);
  });

  it("shares one replacement when concurrent requests find an expired connection", async () => {
    let releaseExpired: (() => void) | undefined;
    const expiredReady = new Promise<void>((resolve) => {
      releaseExpired = resolve;
    });
    const expiredConnection = {
      request: vi.fn().mockResolvedValue([]),
      close: vi.fn(),
      expiresAtMs: 2_000,
    };
    const replacementConnection = {
      request: vi.fn().mockResolvedValue([]),
      close: vi.fn(),
      expiresAtMs: 4_000,
    };
    const connectEncrypted = vi
      .fn()
      .mockImplementationOnce(async () => {
        await expiredReady;
        return expiredConnection;
      })
      .mockResolvedValue(replacementConnection);
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
        now: () => 3_000,
      },
    );

    const first = transport.get("/api/repos");
    const second = transport.get(`/api/repos/${"a".repeat(32)}`);
    releaseExpired?.();
    await Promise.all([first, second]);

    expect(connectEncrypted).toHaveBeenCalledTimes(2);
    expect(expiredConnection.close).toHaveBeenCalledTimes(1);
    expect(replacementConnection.request).toHaveBeenCalledTimes(2);
  });

  it("evicts a rejected expiry replacement so a later request can retry", async () => {
    const expiredConnection = {
      request: vi.fn(),
      close: vi.fn(),
      expiresAtMs: 2_000,
    };
    const recoveredConnection = {
      request: vi.fn().mockResolvedValue([]),
      close: vi.fn(),
      expiresAtMs: 4_000,
    };
    const connectEncrypted = vi
      .fn()
      .mockResolvedValueOnce(expiredConnection)
      .mockRejectedValueOnce(new Error("renewal failed"))
      .mockResolvedValueOnce(recoveredConnection);
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
        now: () => 3_000,
      },
    );

    await expect(transport.get("/api/repos")).rejects.toThrow(
      "renewal failed",
    );
    await expect(transport.get("/api/repos")).resolves.toEqual([]);

    expect(connectEncrypted).toHaveBeenCalledTimes(3);
    expect(recoveredConnection.request).toHaveBeenCalledTimes(1);
  });

  it("does not open an expiry replacement after transport close", async () => {
    let releaseExpired: (() => void) | undefined;
    const expiredReady = new Promise<void>((resolve) => {
      releaseExpired = resolve;
    });
    const expiredConnection = {
      request: vi.fn(),
      close: vi.fn(),
      expiresAtMs: 2_000,
    };
    const connectEncrypted = vi.fn().mockImplementation(async () => {
      await expiredReady;
      return expiredConnection;
    });
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
        now: () => 3_000,
      },
    );

    const request = transport.get("/api/repos");
    transport.close();
    releaseExpired?.();

    await expect(request).rejects.toThrow("Runtime transport is closed");
    expect(connectEncrypted).toHaveBeenCalledTimes(1);
    expect(expiredConnection.close).toHaveBeenCalled();
  });

  it("handles a pending connection rejection after transport close", async () => {
    let rejectConnection: ((error: Error) => void) | undefined;
    const pendingConnection = new Promise<never>((_resolve, reject) => {
      rejectConnection = reject;
    });
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted: vi.fn().mockReturnValue(pendingConnection),
      },
    );

    const request = transport.get("/api/repos");
    transport.close();
    rejectConnection?.(new Error("connection failed"));

    await expect(request).rejects.toThrow("connection failed");
    await Promise.resolve();
  });

  it("evicts a failed managed connection without retrying the operation", async () => {
    const failedConnection = {
      request: vi.fn().mockRejectedValue(new Error("transport failed")),
      close: vi.fn(),
    };
    const replacementConnection = {
      request: vi.fn().mockResolvedValue([]),
      close: vi.fn(),
    };
    const connectEncrypted = vi
      .fn()
      .mockResolvedValueOnce(failedConnection)
      .mockResolvedValueOnce(replacementConnection);
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
      },
    );

    await expect(transport.get("/api/repos")).rejects.toThrow(
      "transport failed",
    );
    expect(connectEncrypted).toHaveBeenCalledTimes(1);
    await expect(transport.get("/api/repos")).resolves.toEqual([]);
    expect(connectEncrypted).toHaveBeenCalledTimes(2);
    expect(failedConnection.close).toHaveBeenCalledTimes(1);
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

  it("passes managed bounded history query data unchanged", async () => {
    const connection = {
      request: vi.fn().mockResolvedValue({}),
      close: vi.fn(),
    };
    const connectEncrypted = vi.fn().mockResolvedValue(connection);
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
      },
    );
    const sessionId = "a".repeat(32);
    const messageId = "b".repeat(32);

    await transport.get(
      `/api/sessions/${sessionId}/messages/page?cursor=abc_123`,
    );
    await transport.get(
      `/api/sessions/${sessionId}/messages/${messageId}/field?name=content&offset=32768`,
    );

    expect(connection.request).toHaveBeenNthCalledWith(1, {
      method: "GET",
      path: `/api/sessions/${sessionId}/messages/page`,
      query: { cursor: "abc_123" },
      body: null,
    });
    expect(connection.request).toHaveBeenNthCalledWith(2, {
      method: "GET",
      path: `/api/sessions/${sessionId}/messages/${messageId}/field`,
      query: { name: "content", offset: "32768" },
      body: null,
    });
  });

  it("allows only exact bounded history routes with session read scope", async () => {
    const encryptedRequest = vi.fn().mockResolvedValue({});
    const transport = createRuntimeTransport(
      { location: "byoc", runnerId: "runner-1", runnerPublicKey },
      { apiClient: apiClient(), encryptedRequest },
    );
    const sessionId = "a".repeat(32);
    const messageId = "b".repeat(32);

    await transport.get(
      `/api/sessions/${sessionId}/messages/page?cursor=abc_123`,
    );
    await transport.get(
      `/api/sessions/${sessionId}/messages/${messageId}/field?name=full_message&offset=32768`,
    );

    expect(encryptedRequest).toHaveBeenNthCalledWith(1, {
      expectedRunnerPublicKey: runnerPublicKey,
      scopes: ["session.read"],
      method: "GET",
      path: `/api/sessions/${sessionId}/messages/page`,
      query: { cursor: "abc_123" },
      body: null,
      maxSessionBytes: 16_777_216,
    });
    expect(encryptedRequest).toHaveBeenNthCalledWith(2, {
      expectedRunnerPublicKey: runnerPublicKey,
      scopes: ["session.read"],
      method: "GET",
      path: `/api/sessions/${sessionId}/messages/${messageId}/field`,
      query: { name: "full_message", offset: "32768" },
      body: null,
      maxSessionBytes: 16_777_216,
    });

    await expect(
      transport.get(`/api/sessions/${sessionId}/messages/pages`),
    ).rejects.toThrow("not allowed");
    await expect(
      transport.get(
        `/api/sessions/${sessionId}/messages/${messageId}/fields?name=content&offset=0`,
      ),
    ).rejects.toThrow("not allowed");
    await expect(
      transport.get(
        `/api/sessions/${sessionId}/messages/${messageId}/field?name=content&%6eame=full_message&offset=0`,
      ),
    ).rejects.toThrow("query keys must be unique");
    expect(encryptedRequest).toHaveBeenCalledTimes(2);
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
