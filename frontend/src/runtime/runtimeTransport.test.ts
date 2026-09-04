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

  it("allows exact read-only thread routes with session read authority", async () => {
    const encryptedRequest = vi.fn().mockResolvedValue({});
    const transport = createRuntimeTransport(
      { location: "byoc", runnerId: "runner-1", runnerPublicKey },
      { apiClient: apiClient(), encryptedRequest },
    );
    const sessionId = "a".repeat(32);
    const paths = [
      `/api/threads/${sessionId}`,
      `/api/threads/${sessionId}/tree`,
      `/api/threads/${sessionId}/children`,
      `/api/threads/${sessionId}/result`,
      `/api/threads/${sessionId}/limits`,
    ];

    for (const path of paths) await transport.get(path);

    expect(encryptedRequest).toHaveBeenCalledTimes(paths.length);
    paths.forEach((path, index) => {
      expect(encryptedRequest).toHaveBeenNthCalledWith(index + 1, {
        expectedRunnerPublicKey: runnerPublicKey,
        scopes: ["session.read"],
        method: "GET",
        path,
        query: {},
        body: null,
        maxSessionBytes: 16_777_216,
      });
    });
    await expect(
      transport.post(`/api/threads/${sessionId}`, {}),
    ).rejects.toThrow("not allowed");
  });

  it("uses session stream authority for exact active-run discovery", async () => {
    const encryptedRequest = vi.fn().mockResolvedValue(null);
    const transport = createRuntimeTransport(
      { location: "byoc", runnerId: "runner-1", runnerPublicKey },
      { apiClient: apiClient(), encryptedRequest },
    );
    const sessionId = "a".repeat(32);

    await expect(
      transport.get(`/api/sessions/${sessionId}/runs/active`),
    ).resolves.toBeNull();
    expect(encryptedRequest).toHaveBeenCalledWith({
      expectedRunnerPublicKey: runnerPublicKey,
      scopes: ["session.stream"],
      method: "GET",
      path: `/api/sessions/${sessionId}/runs/active`,
      query: {},
      body: null,
      maxSessionBytes: 16_777_216,
    });
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

  it("selects push only for exact advertised managed history bundles", async () => {
    const connection = {
      request: vi.fn().mockResolvedValue({}),
      close: vi.fn(),
    };
    const connectEncrypted = vi.fn().mockResolvedValue(connection);
    const transport = createRuntimeTransport(
      {
        location: "managed",
        runnerPublicKey,
        runnerRpcPushSupported: true,
      },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
      },
    );
    const sessionId = "a".repeat(32);

    await transport.get(`/api/sessions/${sessionId}/messages/bundle`);
    await transport.get(`/api/sessions/${sessionId}/messages/page`);

    expect(connectEncrypted).toHaveBeenCalledWith({
      expectedRunnerPublicKey: runnerPublicKey,
      scopes: ["session.read"],
      maxSessionBytes: 16_777_216,
      capabilityEndpoint: "/api/runtime/capabilities",
      pushResponsesSupported: true,
    });
    expect(connection.request).toHaveBeenNthCalledWith(1, {
      method: "GET",
      path: `/api/sessions/${sessionId}/messages/bundle`,
      query: {},
      body: null,
      response_mode: "push",
    });
    expect(connection.request).toHaveBeenNthCalledWith(2, {
      method: "GET",
      path: `/api/sessions/${sessionId}/messages/page`,
      query: {},
      body: null,
    });
  });

  it("keeps managed bundles on pull when push advertisement is absent", async () => {
    const connection = {
      request: vi.fn().mockResolvedValue({}),
      close: vi.fn(),
    };
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted: vi.fn().mockResolvedValue(connection),
      },
    );

    await transport.get(`/api/sessions/${"a".repeat(32)}/messages/bundle`);

    expect(connection.request).toHaveBeenCalledWith({
      method: "GET",
      path: `/api/sessions/${"a".repeat(32)}/messages/bundle`,
      query: {},
      body: null,
    });
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

  it("does not let a managed terminal event poll block terminal input", async () => {
    let releaseEvents: (() => void) | undefined;
    const eventsReleased = new Promise<void>((resolve) => {
      releaseEvents = resolve;
    });
    const eventConnection = {
      request: vi.fn().mockImplementation(async () => {
        await eventsReleased;
        return { events: [] };
      }),
      close: vi.fn(),
    };
    const commandConnection = {
      request: vi.fn().mockResolvedValue(undefined),
      close: vi.fn(),
    };
    const connectEncrypted = vi
      .fn()
      .mockResolvedValueOnce(eventConnection)
      .mockResolvedValueOnce(commandConnection);
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
      },
    );
    const workspaceId = "a".repeat(32);
    const terminalId = "b".repeat(32);
    const eventsRequest = transport.get(
      `/api/workspaces/${workspaceId}/terminals/${terminalId}/events/0`,
    );
    await vi.waitFor(() => {
      expect(eventConnection.request).toHaveBeenCalledTimes(1);
    });

    const inputRequest = transport.post(
      `/api/workspaces/${workspaceId}/terminals/${terminalId}/input`,
      { data: "a" },
    );
    try {
      await vi.waitFor(() => {
        expect(commandConnection.request).toHaveBeenCalledWith({
          method: "POST",
          path: `/api/workspaces/${workspaceId}/terminals/${terminalId}/input`,
          query: {},
          body: { data: "a" },
        });
      });
      await expect(inputRequest).resolves.toBeUndefined();
      expect(connectEncrypted).toHaveBeenCalledTimes(2);
      expect(connectEncrypted).toHaveBeenNthCalledWith(1, {
        expectedRunnerPublicKey: runnerPublicKey,
        scopes: ["terminal"],
        maxSessionBytes: 16_777_216,
        capabilityEndpoint: "/api/runtime/capabilities",
      });
      expect(connectEncrypted).toHaveBeenNthCalledWith(2, {
        expectedRunnerPublicKey: runnerPublicKey,
        scopes: ["terminal"],
        maxSessionBytes: 16_777_216,
        capabilityEndpoint: "/api/runtime/capabilities",
      });
    } finally {
      releaseEvents?.();
      await Promise.allSettled([eventsRequest, inputRequest]);
      transport.close();
    }
  });

  it("keeps managed terminal commands sequential on one connection", async () => {
    let releaseInput: (() => void) | undefined;
    const inputReleased = new Promise<void>((resolve) => {
      releaseInput = resolve;
    });
    const commandConnection = {
      request: vi
        .fn()
        .mockImplementationOnce(async () => {
          await inputReleased;
          return undefined;
        })
        .mockResolvedValueOnce(undefined),
      close: vi.fn(),
    };
    const connectEncrypted = vi.fn().mockResolvedValue(commandConnection);
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
      },
    );
    const workspaceId = "a".repeat(32);
    const terminalId = "b".repeat(32);
    const inputRequest = transport.post(
      `/api/workspaces/${workspaceId}/terminals/${terminalId}/input`,
      { data: "a" },
    );
    await vi.waitFor(() => {
      expect(commandConnection.request).toHaveBeenCalledTimes(1);
    });

    const resizeRequest = transport.post(
      `/api/workspaces/${workspaceId}/terminals/${terminalId}/resize`,
      { cols: 80, rows: 24 },
    );
    await Promise.resolve();
    expect(commandConnection.request).toHaveBeenCalledTimes(1);

    releaseInput?.();
    await expect(Promise.all([inputRequest, resizeRequest])).resolves.toEqual([
      undefined,
      undefined,
    ]);
    expect(connectEncrypted).toHaveBeenCalledTimes(1);
    expect(commandConnection.request).toHaveBeenNthCalledWith(2, {
      method: "POST",
      path: `/api/workspaces/${workspaceId}/terminals/${terminalId}/resize`,
      query: {},
      body: { cols: 80, rows: 24 },
    });
  });

  it("closes both managed terminal connections", async () => {
    let releaseEvents: (() => void) | undefined;
    const eventsReleased = new Promise<void>((resolve) => {
      releaseEvents = resolve;
    });
    const eventConnection = {
      request: vi.fn().mockImplementation(async () => {
        await eventsReleased;
        return { events: [] };
      }),
      close: vi.fn(),
    };
    const commandConnection = {
      request: vi.fn().mockResolvedValue(undefined),
      close: vi.fn(),
    };
    const connectEncrypted = vi
      .fn()
      .mockResolvedValueOnce(eventConnection)
      .mockResolvedValueOnce(commandConnection);
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
      },
    );
    const workspaceId = "a".repeat(32);
    const terminalId = "b".repeat(32);
    const eventsRequest = transport.get(
      `/api/workspaces/${workspaceId}/terminals/${terminalId}/events/0`,
    );
    await vi.waitFor(() => {
      expect(eventConnection.request).toHaveBeenCalledTimes(1);
    });
    await transport.post(
      `/api/workspaces/${workspaceId}/terminals/${terminalId}/input`,
      { data: "a" },
    );

    transport.close();
    await Promise.resolve();
    expect(eventConnection.close).toHaveBeenCalledTimes(1);
    expect(commandConnection.close).toHaveBeenCalledTimes(1);

    releaseEvents?.();
    await eventsRequest;
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

  it("uses at most eight sequential connections for managed history fields", async () => {
    const connections: Array<{
      request: ReturnType<typeof vi.fn>;
      close: ReturnType<typeof vi.fn>;
    }> = [];
    const releases: Array<() => void> = [];
    let activeRequests = 0;
    let maximumActiveRequests = 0;
    let requestsStarted = 0;
    const connectEncrypted = vi.fn().mockImplementation(() => {
      let connectionActive = false;
      const connection = {
        request: vi.fn().mockImplementation(async () => {
          if (connectionActive)
            throw new Error("connection requests overlapped");
          connectionActive = true;
          activeRequests += 1;
          maximumActiveRequests = Math.max(
            maximumActiveRequests,
            activeRequests,
          );
          requestsStarted += 1;
          await new Promise<void>((resolve) => releases.push(resolve));
          activeRequests -= 1;
          connectionActive = false;
          return {};
        }),
        close: vi.fn(),
      };
      connections.push(connection);
      return Promise.resolve(connection);
    });
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
      },
    );
    const sessionId = "a".repeat(32);
    const requests = Array.from({ length: 9 }, (_value, index) => {
      const messageId = index.toString(16).padStart(32, "0");
      return transport.get(
        `/api/sessions/${sessionId}/messages/${messageId}/field?name=content&offset=0`,
      );
    });

    await vi.waitFor(() => expect(requestsStarted).toBe(8));
    expect(connectEncrypted).toHaveBeenCalledTimes(8);
    expect(maximumActiveRequests).toBe(8);

    releases[0]();
    await vi.waitFor(() => expect(requestsStarted).toBe(9));
    expect(connectEncrypted).toHaveBeenCalledTimes(8);

    for (const release of releases.slice(1)) release();
    await expect(Promise.all(requests)).resolves.toHaveLength(9);
    expect(
      connections.every(
        (connection) => connection.request.mock.calls.length <= 2,
      ),
    ).toBe(true);

    transport.close();
    await Promise.resolve();
    expect(
      connections.every(
        (connection) => connection.close.mock.calls.length === 1,
      ),
    ).toBe(true);
  });

  it("rotates managed history page and field lanes independently", async () => {
    const connections: Array<{
      request: ReturnType<typeof vi.fn>;
      close: ReturnType<typeof vi.fn>;
    }> = [];
    const connectEncrypted = vi.fn().mockImplementation(() => {
      const connection = {
        request: vi.fn().mockResolvedValue({}),
        close: vi.fn(),
      };
      connections.push(connection);
      return Promise.resolve(connection);
    });
    const transport = createRuntimeTransport(
      { location: "managed", runnerPublicKey },
      {
        apiClient: apiClient(),
        encryptedRequest: vi.fn(),
        connectEncrypted,
      },
    );
    const sessionId = "a".repeat(32);
    const messageId = `${"b".repeat(31)}1`;
    const pagePath = `/api/sessions/${sessionId}/messages/page`;
    const fieldPath = `/api/sessions/${sessionId}/messages/${messageId}/field`;
    const requests = Array.from({ length: 17 }, (_value, index) =>
      transport.get(`${pagePath}?cursor=${index}`),
    );
    requests.push(
      ...Array.from({ length: 17 }, (_value, index) =>
        transport.get(`${fieldPath}?name=content&offset=${index}`),
      ),
    );

    await expect(Promise.all(requests)).resolves.toHaveLength(34);

    expect(connectEncrypted).toHaveBeenCalledTimes(4);
    const pageConnections = connections.filter(
      (connection) => connection.request.mock.calls[0]?.[0].path === pagePath,
    );
    const fieldConnections = connections.filter(
      (connection) => connection.request.mock.calls[0]?.[0].path === fieldPath,
    );
    expect(pageConnections).toHaveLength(2);
    expect(fieldConnections).toHaveLength(2);
    for (const laneConnections of [pageConnections, fieldConnections]) {
      expect(laneConnections[0].request).toHaveBeenCalledTimes(16);
      expect(laneConnections[1].request).toHaveBeenCalledTimes(1);
      expect(laneConnections[0].close).toHaveBeenCalledTimes(1);
      expect(laneConnections[1].close).not.toHaveBeenCalled();
    }
    for (const [options] of connectEncrypted.mock.calls) {
      expect(options).toMatchObject({
        scopes: ["session.read"],
        maxSessionBytes: 16_777_216,
        capabilityEndpoint: "/api/runtime/capabilities",
      });
    }
  });

  it("keeps ordinary managed session reads sequential on one connection", async () => {
    let releaseFirst: (() => void) | undefined;
    const firstReleased = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    let activeRequests = 0;
    const connection = {
      request: vi.fn().mockImplementation(async () => {
        activeRequests += 1;
        if (activeRequests > 1) throw new Error("session reads overlapped");
        if (connection.request.mock.calls.length === 1) await firstReleased;
        activeRequests -= 1;
        return [];
      }),
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

    const metadata = transport.get(`/api/sessions/${sessionId}`);
    const messages = transport.get(`/api/sessions/${sessionId}/messages`);
    await vi.waitFor(() => expect(connection.request).toHaveBeenCalledTimes(1));
    releaseFirst?.();

    await expect(Promise.all([metadata, messages])).resolves.toEqual([[], []]);
    expect(connectEncrypted).toHaveBeenCalledTimes(1);
    expect(connection.request).toHaveBeenCalledTimes(2);
    expect(connection.close).not.toHaveBeenCalled();
  });

  it("keeps non-history managed scopes on one connection", async () => {
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

    await Promise.all(
      Array.from({ length: 10 }, () => transport.get("/api/repos")),
    );

    expect(connectEncrypted).toHaveBeenCalledTimes(1);
    expect(connection.request).toHaveBeenCalledTimes(10);
    expect(connection.close).not.toHaveBeenCalled();
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

    await expect(transport.get("/api/repos")).rejects.toThrow("renewal failed");
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
      `/api/sessions/${sessionId}/messages/bundle?cursor=cursor_1&through=through_1&snapshot=123&snapshot_count=66&snapshot_tail=tail_1`,
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
      path: `/api/sessions/${sessionId}/messages/bundle`,
      query: {
        cursor: "cursor_1",
        through: "through_1",
        snapshot: "123",
        snapshot_count: "66",
        snapshot_tail: "tail_1",
      },
      body: null,
      maxSessionBytes: 16_777_216,
    });
    expect(encryptedRequest).toHaveBeenNthCalledWith(3, {
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
      transport.get(`/api/sessions/${sessionId}/messages/bundles`),
    ).rejects.toThrow("not allowed");
    await expect(
      transport.post(`/api/sessions/${sessionId}/messages/bundle`, {}),
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
    expect(encryptedRequest).toHaveBeenCalledTimes(3);
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
