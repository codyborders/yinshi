import { describe, expect, it, vi } from "vitest";

import {
  checkEncryptedRunnerHealth,
  connectEncryptedRunner,
  requestEncryptedRunner,
  type RunnerClientDependencies,
} from "./encryptedRunnerClient";

const runnerPublicKey = "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I";
const clientPublicKey = "a8OCKiqn9OaYHWU4aSs83z5t-e6m7SaetB2TwidXt1o";
const transportHeaderBytes = 17;
const transportPayloadBytes = 65_535 - 16 - transportHeaderBytes;

function transportFrame(
  kind: number,
  index: number,
  count: number,
  total: number,
  payload?: Uint8Array,
  payloadStart = 0,
  payloadEnd = payload?.length ?? 0,
): Uint8Array {
  const frame = new Uint8Array(
    transportHeaderBytes + payloadEnd - payloadStart,
  );
  frame.set(new TextEncoder().encode("YRP1"));
  const view = new DataView(frame.buffer);
  view.setUint8(4, kind);
  view.setUint32(5, index);
  view.setUint32(9, count);
  view.setUint32(13, total);
  if (payload !== undefined) {
    for (
      let source = payloadStart, target = transportHeaderBytes;
      source < payloadEnd;
      source += 1, target += 1
    ) {
      frame[target] = payload[source];
    }
  }
  return frame;
}

function transportResponse(payload: Uint8Array, index: number): Uint8Array {
  const count = Math.max(1, Math.ceil(payload.length / transportPayloadBytes));
  const start = index * transportPayloadBytes;
  const end = Math.min(payload.length, start + transportPayloadBytes);
  return transportFrame(3, index, count, payload.length, payload, start, end);
}

class FakeWebSocket extends EventTarget {
  static readonly OPEN = 1;
  readonly sent: Array<string | ArrayBufferView | ArrayBuffer | Blob> = [];
  binaryType = "blob";
  readyState = FakeWebSocket.OPEN;

  constructor(private readonly rpcResponseDelayMs = 0) {
    super();
    queueMicrotask(() => this.dispatchEvent(new Event("open")));
  }

  send(data: string | ArrayBufferView | ArrayBuffer | Blob): void {
    this.sent.push(data);
    if (typeof data === "string") {
      queueMicrotask(() =>
        this.dispatchEvent(
          new MessageEvent("message", { data: '{"type":"ready"}' }),
        ),
      );
      return;
    }
    const response =
      this.sent.length === 2 ? Uint8Array.of(11) : Uint8Array.of(12);
    const dispatchResponse = () =>
      this.dispatchEvent(
        new MessageEvent("message", { data: response.buffer }),
      );
    if (this.sent.length > 2 && this.rpcResponseDelayMs > 0) {
      window.setTimeout(dispatchResponse, this.rpcResponseDelayMs);
    } else {
      queueMicrotask(dispatchResponse);
    }
  }

  close(): void {
    this.readyState = 3;
    this.dispatchEvent(new CloseEvent("close", { code: 1000, wasClean: true }));
  }
}

function dependencies(
  socket: FakeWebSocket,
  responseBody: unknown = { protocol: "yinshi-runner-v1", status: "ok" },
): RunnerClientDependencies {
  return {
    createKeypair: async () => ({
      privateKey: Uint8Array.of(...new Array<number>(32).fill(1)),
      publicKey: Uint8Array.from(
        Buffer.from(clientPublicKey + "=", "base64url"),
      ),
    }),
    createInitiator: async () => {
      const response = new TextEncoder().encode(
        JSON.stringify({
          body: responseBody,
          request_id: "22222222-2222-4222-8222-222222222222",
          sequence: 0,
          status: 200,
          type: "response",
          v: 2,
        }),
      );
      const pending: Uint8Array[] = [];
      return {
        get handshakeHash() {
          return new Uint8Array(32);
        },
        writeHandshakeMessage: () => Uint8Array.of(1),
        readHandshakeMessage: () =>
          new TextEncoder().encode(
            '{"protocol":"yinshi-runner-v1","transfer_id":"11111111-1111-4111-8111-111111111111"}',
          ),
        encrypt: (plaintext: Uint8Array) => {
          const isTransport =
            plaintext.length >= 4 &&
            new TextDecoder().decode(plaintext.subarray(0, 4)) === "YRP1";
          if (!isTransport) {
            pending.push(
              response.length <= 65_535 - 16
                ? response
                : transportResponse(response, 0),
            );
            return Uint8Array.of(2);
          }
          const view = new DataView(
            plaintext.buffer,
            plaintext.byteOffset,
            plaintext.byteLength,
          );
          const kind = view.getUint8(4);
          const index = view.getUint32(5);
          const count = view.getUint32(9);
          const total = view.getUint32(13);
          if (kind === 1 && index + 1 < count) {
            pending.push(transportFrame(2, index, count, total));
          } else if (kind === 1) {
            pending.push(transportResponse(response, 0));
          } else if (kind === 4) {
            pending.push(transportResponse(response, index));
          }
          return Uint8Array.of(2);
        },
        decrypt: () => {
          const plaintext = pending.shift();
          if (plaintext === undefined) {
            throw new Error("Missing fake encrypted response");
          }
          return plaintext;
        },
        dispose: vi.fn(),
      };
    },
    issueCapability: vi.fn().mockResolvedValue({
      capability:
        "signed-capability-value-that-is-long-enough-for-strict-validation-123456789",
      transfer_id: "11111111-1111-4111-8111-111111111111",
      runner_id: "runner-1",
      runner_public_key: runnerPublicKey,
      protocol: "yinshi-runner-v1",
      issued_at: 1_900_000_000,
      expires_at: 1_900_000_300,
      max_frame_bytes: 65_535,
      max_session_bytes: 65_536,
      relay_url:
        "wss://yinshi.example/api/runner/relay/11111111-1111-4111-8111-111111111111",
    }),
    openWebSocket: vi.fn(() => socket as unknown as WebSocket),
    createRequestId: () => "22222222-2222-4222-8222-222222222222",
  };
}

describe("checkEncryptedRunnerHealth", () => {
  it("keeps RPC payloads inside a capability-bound Noise transport", async () => {
    const socket = new FakeWebSocket();
    const clientDependencies = dependencies(socket);

    await expect(
      checkEncryptedRunnerHealth(runnerPublicKey, clientDependencies),
    ).resolves.toEqual({ protocol: "yinshi-runner-v1", status: "ok" });

    expect(clientDependencies.issueCapability).toHaveBeenCalledWith(
      {
        initiator_public_key: clientPublicKey,
        scopes: ["worker.health"],
        max_session_bytes: 65_536,
      },
      "/api/settings/runner/capabilities",
    );
    expect(socket.sent[0]).toBe(
      "signed-capability-value-that-is-long-enough-for-strict-validation-123456789",
    );
    expect(Array.from(socket.sent[1] as Uint8Array)).toEqual([1]);
    expect(Array.from(socket.sent[2] as Uint8Array)).toEqual([2]);
  });

  it("uses legacy plaintext RPC for a request that fits one Noise frame", async () => {
    const socket = new FakeWebSocket();
    const clientDependencies = dependencies(socket, { status: "legacy" });
    const encryptedPlaintexts: Uint8Array[] = [];
    clientDependencies.createInitiator = async () => ({
      get handshakeHash() {
        return new Uint8Array(32);
      },
      writeHandshakeMessage: () => Uint8Array.of(1),
      readHandshakeMessage: () =>
        new TextEncoder().encode(
          '{"protocol":"yinshi-runner-v1","transfer_id":"11111111-1111-4111-8111-111111111111"}',
        ),
      encrypt: (plaintext) => {
        encryptedPlaintexts.push(plaintext);
        return Uint8Array.of(2);
      },
      decrypt: () =>
        new TextEncoder().encode(
          JSON.stringify({
            body: { status: "legacy" },
            request_id: "22222222-2222-4222-8222-222222222222",
            sequence: 0,
            status: 200,
            type: "response",
            v: 2,
          }),
        ),
      dispose: vi.fn(),
    });

    await expect(
      requestEncryptedRunner<{ status: string }>(
        {
          expectedRunnerPublicKey: runnerPublicKey,
          scopes: ["repository.read"],
          method: "GET",
          path: "/api/repos",
        },
        clientDependencies,
      ),
    ).resolves.toEqual({ status: "legacy" });
    expect(new TextDecoder().decode(encryptedPlaintexts[0])).toContain(
      '"type":"request"',
    );
  });

  it("supports repository requests through the same encrypted contract", async () => {
    const socket = new FakeWebSocket();
    const clientDependencies = dependencies(socket, []);

    await expect(
      requestEncryptedRunner<unknown[]>(
        {
          expectedRunnerPublicKey: runnerPublicKey,
          scopes: ["repository.read"],
          method: "GET",
          path: "/api/repos",
        },
        clientDependencies,
      ),
    ).resolves.toEqual([]);
    expect(clientDependencies.issueCapability).toHaveBeenCalledWith(
      {
        initiator_public_key: clientPublicKey,
        scopes: ["repository.read"],
        max_session_bytes: 65_536,
      },
      "/api/settings/runner/capabilities",
    );
  });

  it("selects runtime capability issuance for encrypted requests", async () => {
    const socket = new FakeWebSocket();
    const clientDependencies = dependencies(socket, []);

    await expect(
      requestEncryptedRunner<unknown[]>(
        {
          expectedRunnerPublicKey: runnerPublicKey,
          scopes: ["repository.read"],
          capabilityEndpoint: "/api/runtime/capabilities",
          method: "GET",
          path: "/api/repos",
        },
        clientDependencies,
      ),
    ).resolves.toEqual([]);
    expect(clientDependencies.issueCapability).toHaveBeenCalledWith(
      {
        initiator_public_key: clientPublicKey,
        scopes: ["repository.read"],
        max_session_bytes: 65_536,
      },
      "/api/runtime/capabilities",
    );
  });

  it("rejects unknown capability endpoints before creating keys", async () => {
    const socket = new FakeWebSocket();
    const clientDependencies = dependencies(socket);
    const createKeypair = vi.fn(clientDependencies.createKeypair);
    clientDependencies.createKeypair = createKeypair;

    await expect(
      connectEncryptedRunner(
        {
          expectedRunnerPublicKey: runnerPublicKey,
          scopes: ["repository.read"],
          capabilityEndpoint: "/api/unknown/capabilities",
        } as unknown as Parameters<typeof connectEncryptedRunner>[0],
        clientDependencies,
      ),
    ).rejects.toThrow("Runner capability endpoint is invalid");
    expect(createKeypair).not.toHaveBeenCalled();
    expect(clientDependencies.issueCapability).not.toHaveBeenCalled();
    expect(clientDependencies.openWebSocket).not.toHaveBeenCalled();
  });

  it("reuses one Noise session for ordered multi-request transfers", async () => {
    const socket = new FakeWebSocket();
    const clientDependencies = dependencies(socket);
    const requestIds = [
      "22222222-2222-4222-8222-222222222222",
      "33333333-3333-4333-8333-333333333333",
    ];
    const originalCreateInitiator = clientDependencies.createInitiator;
    clientDependencies.createRequestId = vi
      .fn()
      .mockReturnValueOnce(requestIds[0])
      .mockReturnValueOnce(requestIds[1]);
    clientDependencies.createInitiator = async (options) => {
      const initiator = await originalCreateInitiator(options);
      let responseSequence = 0;
      return {
        ...initiator,
        decrypt: () => {
          const sequence = responseSequence;
          responseSequence += 1;
          const response = new TextEncoder().encode(
            JSON.stringify({
              body: { sequence },
              request_id: requestIds[sequence],
              sequence,
              status: 200,
              type: "response",
              v: 2,
            }),
          );
          return transportResponse(response, 0);
        },
      };
    };

    const connection = await connectEncryptedRunner(
      {
        expectedRunnerPublicKey: runnerPublicKey,
        scopes: ["repository.read"],
      },
      clientDependencies,
    );
    await expect(
      connection.request({ method: "GET", path: "/api/repos" }),
    ).resolves.toEqual({ sequence: 0 });
    await expect(
      connection.request({
        method: "GET",
        path: `/api/repos/${"a".repeat(32)}`,
      }),
    ).resolves.toEqual({ sequence: 1 });
    connection.close();

    expect(clientDependencies.issueCapability).toHaveBeenCalledTimes(1);
    expect(socket.sent).toHaveLength(4);
  });

  it("keeps an accepted RPC open while the worker completes within 30 seconds", async () => {
    vi.useFakeTimers();
    try {
      const socket = new FakeWebSocket(30_000);
      const request = requestEncryptedRunner<{ status: string }>(
        {
          expectedRunnerPublicKey: runnerPublicKey,
          scopes: ["repository.read"],
          method: "GET",
          path: "/api/repos",
        },
        dependencies(socket, { status: "ready" }),
      );

      await vi.advanceTimersByTimeAsync(15_001);
      expect(socket.readyState).toBe(FakeWebSocket.OPEN);
      await vi.advanceTimersByTimeAsync(14_999);

      await expect(request).resolves.toEqual({ status: "ready" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("carries bounded large requests and responses through encrypted fragments", async () => {
    const socket = new FakeWebSocket();
    const responseBody = {
      event: "e".repeat(1 * 1_024 * 1_024),
      generic: "r".repeat(8 * 1_024 * 1_024),
    };
    const clientDependencies = dependencies(socket, responseBody);
    clientDependencies.issueCapability = vi.fn().mockResolvedValue({
      ...(await clientDependencies.issueCapability({
        initiator_public_key: clientPublicKey,
        scopes: ["files.write"],
        max_session_bytes: 32 * 1_024 * 1_024,
      })),
      max_session_bytes: 32 * 1_024 * 1_024,
    });

    await expect(
      requestEncryptedRunner<typeof responseBody>(
        {
          expectedRunnerPublicKey: runnerPublicKey,
          scopes: ["files.write"],
          method: "PUT",
          path: `/api/workspaces/${"1".repeat(32)}/files/content`,
          query: { path: "large.txt" },
          body: {
            content: "f".repeat(512 * 1_024),
            prompt: "p".repeat(100_000),
          },
          maxSessionBytes: 32 * 1_024 * 1_024,
        },
        clientDependencies,
      ),
    ).resolves.toEqual(responseBody);
    expect(socket.sent.length).toBeGreaterThan(100);
  });

  it("closes the connection after an out-of-order response fragment", async () => {
    const socket = new FakeWebSocket();
    const clientDependencies = dependencies(socket);
    const originalCreateInitiator = clientDependencies.createInitiator;
    clientDependencies.createInitiator = async (options) => {
      const initiator = await originalCreateInitiator(options);
      return {
        ...initiator,
        decrypt: () => transportFrame(3, 1, 2, 70_000, Uint8Array.of(1)),
      };
    };

    await expect(
      checkEncryptedRunnerHealth(runnerPublicKey, clientDependencies),
    ).rejects.toThrow("Runner RPC transport fragment is invalid");
    expect(socket.readyState).toBe(3);
  });

  it("clears the ephemeral private key when capability issuance fails", async () => {
    const socket = new FakeWebSocket();
    const clientDependencies = dependencies(socket);
    const privateKey = Uint8Array.of(...new Array<number>(32).fill(7));
    clientDependencies.createKeypair = async () => ({
      privateKey,
      publicKey: Uint8Array.from(
        Buffer.from(clientPublicKey + "=", "base64url"),
      ),
    });
    clientDependencies.issueCapability = vi
      .fn()
      .mockRejectedValue(new Error("offline"));

    await expect(
      checkEncryptedRunnerHealth(runnerPublicKey, clientDependencies),
    ).rejects.toThrow("offline");
    expect(privateKey.every((value) => value === 0)).toBe(true);
    expect(clientDependencies.openWebSocket).not.toHaveBeenCalled();
  });

  it("rejects a responder key change before opening the relay", async () => {
    const socket = new FakeWebSocket();
    const clientDependencies = dependencies(socket);
    clientDependencies.issueCapability = vi.fn().mockResolvedValue({
      ...(await clientDependencies.issueCapability({
        initiator_public_key: clientPublicKey,
        scopes: ["worker.health"],
        max_session_bytes: 65_536,
      })),
      runner_public_key: clientPublicKey,
    });

    await expect(
      checkEncryptedRunnerHealth(runnerPublicKey, clientDependencies),
    ).rejects.toThrow("Runner identity changed");
    expect(clientDependencies.openWebSocket).not.toHaveBeenCalled();
  });
});
