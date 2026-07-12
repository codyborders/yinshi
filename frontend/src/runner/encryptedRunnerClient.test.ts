import { describe, expect, it, vi } from "vitest";

import {
  checkEncryptedRunnerHealth,
  connectEncryptedRunner,
  requestEncryptedRunner,
  type RunnerClientDependencies,
} from "./encryptedRunnerClient";

const runnerPublicKey = "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I";
const clientPublicKey = "a8OCKiqn9OaYHWU4aSs83z5t-e6m7SaetB2TwidXt1o";

class FakeWebSocket extends EventTarget {
  static readonly OPEN = 1;
  readonly sent: Array<string | ArrayBufferView | ArrayBuffer | Blob> = [];
  binaryType = "blob";
  readyState = FakeWebSocket.OPEN;

  constructor() {
    super();
    queueMicrotask(() => this.dispatchEvent(new Event("open")));
  }

  send(data: string | ArrayBufferView | ArrayBuffer | Blob): void {
    this.sent.push(data);
    if (typeof data === "string") {
      queueMicrotask(() =>
        this.dispatchEvent(new MessageEvent("message", { data: '{"type":"ready"}' })),
      );
      return;
    }
    const response = this.sent.length === 2 ? Uint8Array.of(11) : Uint8Array.of(12);
    queueMicrotask(() =>
      this.dispatchEvent(new MessageEvent("message", { data: response.buffer })),
    );
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
      publicKey: Uint8Array.from(Buffer.from(clientPublicKey + "=", "base64url")),
    }),
    createInitiator: async () => ({
      get handshakeHash() {
        return new Uint8Array(32);
      },
      writeHandshakeMessage: () => Uint8Array.of(1),
      readHandshakeMessage: () =>
        new TextEncoder().encode(
          '{"protocol":"yinshi-runner-v1","transfer_id":"11111111-1111-4111-8111-111111111111"}',
        ),
      encrypt: () => Uint8Array.of(2),
      decrypt: () =>
        new TextEncoder().encode(
          JSON.stringify({
            body: responseBody,
            request_id: "22222222-2222-4222-8222-222222222222",
            sequence: 0,
            status: 200,
            type: "response",
            v: 2,
          }),
        ),
      dispose: vi.fn(),
    }),
    issueCapability: vi.fn().mockResolvedValue({
      capability: "signed-capability-value-that-is-long-enough-for-strict-validation-123456789",
      transfer_id: "11111111-1111-4111-8111-111111111111",
      runner_id: "runner-1",
      runner_public_key: runnerPublicKey,
      protocol: "yinshi-runner-v1",
      issued_at: 1_900_000_000,
      expires_at: 1_900_000_300,
      max_frame_bytes: 65_535,
      max_session_bytes: 65_536,
      relay_url: "wss://yinshi.example/api/runner/relay/11111111-1111-4111-8111-111111111111",
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

    expect(clientDependencies.issueCapability).toHaveBeenCalledWith({
      initiator_public_key: clientPublicKey,
      scopes: ["worker.health"],
      max_session_bytes: 65_536,
    });
    expect(socket.sent[0]).toBe(
      "signed-capability-value-that-is-long-enough-for-strict-validation-123456789",
    );
    expect(Array.from(socket.sent[1] as Uint8Array)).toEqual([1]);
    expect(Array.from(socket.sent[2] as Uint8Array)).toEqual([2]);
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
    expect(clientDependencies.issueCapability).toHaveBeenCalledWith({
      initiator_public_key: clientPublicKey,
      scopes: ["repository.read"],
      max_session_bytes: 65_536,
    });
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
          return new TextEncoder().encode(
            JSON.stringify({
              body: { sequence },
              request_id: requestIds[sequence],
              sequence,
              status: 200,
              type: "response",
              v: 2,
            }),
          );
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

  it("clears the ephemeral private key when capability issuance fails", async () => {
    const socket = new FakeWebSocket();
    const clientDependencies = dependencies(socket);
    const privateKey = Uint8Array.of(...new Array<number>(32).fill(7));
    clientDependencies.createKeypair = async () => ({
      privateKey,
      publicKey: Uint8Array.from(Buffer.from(clientPublicKey + "=", "base64url")),
    });
    clientDependencies.issueCapability = vi.fn().mockRejectedValue(new Error("offline"));

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
