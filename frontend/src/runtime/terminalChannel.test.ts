import { describe, expect, it, vi } from "vitest";

import type { RuntimeTransport } from "./runtimeTransport";
import { openRuntimeTerminal } from "./terminalChannel";

const workspaceId = "a".repeat(32);
const terminalId = "b".repeat(32);

function transport(): RuntimeTransport {
  return {
    runtime: {
      location: "byoc",
      runnerId: "runner-1",
      runnerPublicKey: "runner-public-key",
    },
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    upload: vi.fn(),
    close: vi.fn(),
  };
}

describe("runtime terminal channel", () => {
  it("multiplexes terminal input, resize, output, and close by channel ID", async () => {
    const runtimeTransport = transport();
    vi.mocked(runtimeTransport.post).mockResolvedValue({
      id: terminalId,
      workspace_id: workspaceId,
      status: "attached",
    });
    vi.mocked(runtimeTransport.get).mockResolvedValue({
      terminal_id: terminalId,
      events: [{ type: "terminal_data", data: "ready\r\n" }],
      next_sequence: 1,
      closed: true,
    });
    const channel = await openRuntimeTerminal(runtimeTransport, workspaceId, {
      cols: 80,
      rows: 24,
      pollDelayMs: 0,
    });

    await channel.sendInput("pwd\r");
    await channel.resize(100, 30);
    const events = [];
    for await (const event of channel.events()) events.push(event);
    await channel.close();

    expect(events).toEqual([{ type: "terminal_data", data: "ready\r\n" }]);
    expect(runtimeTransport.post).toHaveBeenCalledWith(
      `/api/workspaces/${workspaceId}/terminals/${terminalId}/input`,
      { data: "pwd\r" },
    );
    expect(runtimeTransport.post).toHaveBeenCalledWith(
      `/api/workspaces/${workspaceId}/terminals/${terminalId}/resize`,
      { cols: 100, rows: 30 },
    );
    expect(runtimeTransport.delete).toHaveBeenCalledWith(
      `/api/workspaces/${workspaceId}/terminals/${terminalId}`,
    );
  });

  it("rejects mismatched terminal output before exposing it", async () => {
    const runtimeTransport = transport();
    vi.mocked(runtimeTransport.post).mockResolvedValue({
      id: terminalId,
      workspace_id: workspaceId,
      status: "attached",
    });
    vi.mocked(runtimeTransport.get).mockResolvedValue({
      terminal_id: "c".repeat(32),
      events: [],
      next_sequence: 0,
      closed: false,
    });
    const channel = await openRuntimeTerminal(runtimeTransport, workspaceId, {
      cols: 80,
      rows: 24,
      pollDelayMs: 0,
    });

    await expect(async () => {
      for await (const _event of channel.events()) {
        // Consume to validate the first batch.
      }
    }).rejects.toThrow("did not match");
  });
});
