import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockConnect, mockConnection } = vi.hoisted(() => ({
  mockConnect: vi.fn(),
  mockConnection: {
    request: vi.fn(),
    close: vi.fn(),
  },
}));

vi.mock("../runner/encryptedRunnerClient", () => ({
  connectEncryptedRunner: mockConnect,
  RunnerRpcError: class RunnerRpcError extends Error {},
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

  it("rejects oversized files before issuing a capability", async () => {
    const file = { name: "large.zip", size: 50 * 1024 * 1024 + 1 } as File;

    await expect(uploadEncryptedPiConfig(runtime, file)).rejects.toThrow("50MB");
    expect(mockConnect).not.toHaveBeenCalled();
  });
});
