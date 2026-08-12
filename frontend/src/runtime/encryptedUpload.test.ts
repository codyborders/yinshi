import { beforeEach, describe, expect, it, vi } from "vitest";

const { MockRunnerRpcError, mockConnect, mockConnection } = vi.hoisted(() => ({
  MockRunnerRpcError: class MockRunnerRpcError extends Error {},
  mockConnect: vi.fn(),
  mockConnection: {
    request: vi.fn(),
    close: vi.fn(),
  },
}));

vi.mock("../runner/encryptedRunnerClient", () => ({
  connectEncryptedRunner: mockConnect,
  RunnerRpcError: MockRunnerRpcError,
}));

import { uploadEncryptedPiConfig } from "./encryptedUpload";

const runtime = {
  location: "byoc" as const,
  runnerId: "runner-1",
  runnerPublicKey: "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
};
const uploadId = "a".repeat(32);

describe("encrypted runtime upload", () => {
  beforeEach(() => {
    mockConnect.mockReset();
    mockConnection.request.mockReset();
    mockConnection.close.mockReset();
    mockConnect.mockResolvedValue(mockConnection);
  });

  it("streams ordered chunks through one capability-bound Noise session", async () => {
    const payload = new Uint8Array(30_000).fill(7);
    const file = new File([payload], "config.zip", { type: "application/zip" });
    mockConnection.request
      .mockResolvedValueOnce({
        id: uploadId,
        purpose: "pi_config",
        filename: "config.zip",
        size_bytes: payload.length,
        next_chunk_index: 0,
      })
      .mockResolvedValueOnce({
        id: uploadId,
        purpose: "pi_config",
        filename: "config.zip",
        size_bytes: payload.length,
        next_chunk_index: 1,
      })
      .mockResolvedValueOnce({
        id: uploadId,
        purpose: "pi_config",
        filename: "config.zip",
        size_bytes: payload.length,
        next_chunk_index: 2,
      })
      .mockResolvedValueOnce({ status: "ready" });

    await expect(uploadEncryptedPiConfig(runtime, file)).resolves.toEqual({
      status: "ready",
    });

    expect(mockConnect).toHaveBeenCalledWith({
      expectedRunnerPublicKey: runtime.runnerPublicKey,
      scopes: ["pi.configure"],
      maxSessionBytes: 134_217_728,
    });
    expect(mockConnection.request).toHaveBeenCalledTimes(4);
    expect(mockConnection.request).toHaveBeenLastCalledWith({
      method: "POST",
      path: `/api/settings/pi-config/uploads/${uploadId}/complete`,
    });
    expect(mockConnection.close).toHaveBeenCalledTimes(1);
  });

  it("does not retry an ambiguous start failure without a known upload ID", async () => {
    const file = new File([new Uint8Array([7])], "config.zip", {
      type: "application/zip",
    });
    const connectionError = new Error("connection closed");
    mockConnection.request.mockRejectedValue(connectionError);

    await expect(uploadEncryptedPiConfig(runtime, file)).rejects.toBe(connectionError);

    expect(mockConnect).toHaveBeenCalledTimes(1);
    expect(mockConnection.request).toHaveBeenCalledTimes(1);
    expect(mockConnection.request).toHaveBeenCalledWith({
      method: "POST",
      path: "/api/settings/pi-config/uploads",
      body: {
        purpose: "pi_config",
        filename: "config.zip",
        size_bytes: 1,
        sha256: expect.stringMatching(/^[0-9a-f]{64}$/u),
      },
    });
    expect(mockConnection.close).toHaveBeenCalledTimes(1);
  });

  it("retries a chunk connection failure through the fifth attempt", async () => {
    const file = new File([new Uint8Array([7])], "config.zip", {
      type: "application/zip",
    });
    const started = {
      id: uploadId,
      purpose: "pi_config",
      filename: "config.zip",
      size_bytes: 1,
      next_chunk_index: 0,
    };
    const appended = { ...started, next_chunk_index: 1 };
    mockConnection.request
      .mockResolvedValueOnce(started)
      .mockRejectedValueOnce(new Error("connection closed 1"))
      .mockRejectedValueOnce(new Error("connection closed 2"))
      .mockRejectedValueOnce(new Error("connection closed 3"))
      .mockRejectedValueOnce(new Error("connection closed 4"))
      .mockResolvedValueOnce(appended)
      .mockResolvedValueOnce({ status: "ready" });

    await expect(uploadEncryptedPiConfig(runtime, file)).resolves.toEqual({
      status: "ready",
    });

    const chunkOperation = {
      method: "POST",
      path: `/api/settings/pi-config/uploads/${uploadId}/chunks/0`,
      body: { data: "Bw" },
    };
    expect(mockConnect).toHaveBeenCalledTimes(5);
    expect(mockConnection.request).toHaveBeenCalledTimes(7);
    for (let call = 1; call <= 5; call += 1) {
      expect(mockConnection.request).toHaveBeenNthCalledWith(call + 1, chunkOperation);
    }
  });

  it("does not retry an ambiguous completion failure and cancels once", async () => {
    const file = new File([new Uint8Array([7])], "config.zip", {
      type: "application/zip",
    });
    const connectionError = new Error("connection closed");
    mockConnection.request
      .mockResolvedValueOnce({
        id: uploadId,
        purpose: "pi_config",
        filename: "config.zip",
        size_bytes: 1,
        next_chunk_index: 0,
      })
      .mockResolvedValueOnce({
        id: uploadId,
        purpose: "pi_config",
        filename: "config.zip",
        size_bytes: 1,
        next_chunk_index: 1,
      })
      .mockRejectedValueOnce(connectionError)
      .mockResolvedValueOnce({ status: "cancelled" });

    await expect(uploadEncryptedPiConfig(runtime, file)).rejects.toBe(connectionError);

    expect(mockConnect).toHaveBeenCalledTimes(2);
    expect(mockConnection.request).toHaveBeenCalledTimes(4);
    expect(mockConnection.request).toHaveBeenNthCalledWith(3, {
      method: "POST",
      path: `/api/settings/pi-config/uploads/${uploadId}/complete`,
    });
    expect(mockConnection.request).toHaveBeenNthCalledWith(4, {
      method: "DELETE",
      path: `/api/settings/pi-config/uploads/${uploadId}`,
    });
  });

  it("makes one best-effort cancel attempt for a known upload", async () => {
    const file = new File([new Uint8Array([7])], "config.zip", {
      type: "application/zip",
    });
    mockConnection.request
      .mockResolvedValueOnce({
        id: uploadId,
        purpose: "pi_config",
        filename: "config.zip",
        size_bytes: 1,
        next_chunk_index: 0,
      })
      .mockResolvedValueOnce({ status: "invalid chunk response" })
      .mockRejectedValueOnce(new Error("cancel connection closed"));

    await expect(uploadEncryptedPiConfig(runtime, file)).rejects.toThrow(
      "Encrypted upload response did not match the transfer",
    );

    expect(mockConnect).toHaveBeenCalledTimes(1);
    expect(mockConnection.request).toHaveBeenCalledTimes(3);
    expect(mockConnection.request).toHaveBeenLastCalledWith({
      method: "DELETE",
      path: `/api/settings/pi-config/uploads/${uploadId}`,
    });
    expect(mockConnection.close).toHaveBeenCalledTimes(1);
  });

  it("does not retry RunnerRpcError for a chunk", async () => {
    const file = new File([new Uint8Array([7])], "config.zip", {
      type: "application/zip",
    });
    const rpcError = new MockRunnerRpcError("chunk rejected");
    mockConnection.request
      .mockResolvedValueOnce({
        id: uploadId,
        purpose: "pi_config",
        filename: "config.zip",
        size_bytes: 1,
        next_chunk_index: 0,
      })
      .mockRejectedValueOnce(rpcError)
      .mockResolvedValueOnce({ status: "cancelled" });

    await expect(uploadEncryptedPiConfig(runtime, file)).rejects.toBe(rpcError);

    expect(mockConnect).toHaveBeenCalledTimes(1);
    expect(mockConnection.request).toHaveBeenCalledTimes(3);
    expect(mockConnection.request).toHaveBeenNthCalledWith(2, {
      method: "POST",
      path: `/api/settings/pi-config/uploads/${uploadId}/chunks/0`,
      body: { data: "Bw" },
    });
    expect(mockConnection.request).toHaveBeenNthCalledWith(3, {
      method: "DELETE",
      path: `/api/settings/pi-config/uploads/${uploadId}`,
    });
  });

  it("uses the managed capability endpoint for encrypted config chunks", async () => {
    const managedRuntime = {
      location: "managed" as const,
      runnerPublicKey: runtime.runnerPublicKey,
    };
    const file = new File([new Uint8Array([7])], "config.zip", {
      type: "application/zip",
    });
    mockConnection.request
      .mockResolvedValueOnce({
        id: uploadId,
        purpose: "pi_config",
        filename: "config.zip",
        size_bytes: 1,
        next_chunk_index: 0,
      })
      .mockResolvedValueOnce({
        id: uploadId,
        purpose: "pi_config",
        filename: "config.zip",
        size_bytes: 1,
        next_chunk_index: 1,
      })
      .mockResolvedValueOnce({ status: "ready" });

    await expect(uploadEncryptedPiConfig(managedRuntime, file)).resolves.toEqual({
      status: "ready",
    });

    expect(mockConnect).toHaveBeenCalledWith({
      expectedRunnerPublicKey: managedRuntime.runnerPublicKey,
      scopes: ["pi.configure"],
      maxSessionBytes: 134_217_728,
      capabilityEndpoint: "/api/runtime/capabilities",
    });
  });

  it("rejects oversized files before issuing a capability", async () => {
    const file = { name: "large.zip", size: 50 * 1024 * 1024 + 1 } as File;

    await expect(uploadEncryptedPiConfig(runtime, file)).rejects.toThrow("50MB");
    expect(mockConnect).not.toHaveBeenCalled();
  });
});
