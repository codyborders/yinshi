import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RuntimeTransport } from "./runtimeTransport";

let openRuntimeTerminal: typeof import("./terminalChannel").openRuntimeTerminal;

class FakeBroadcastChannel {
  static readonly instances = new Set<FakeBroadcastChannel>();
  static readonly delayedDeliveries: Array<() => void> = [];
  static delayDelivery = false;
  readonly name: string;
  onmessage: ((event: MessageEvent<unknown>) => void) | null = null;

  constructor(name: string) {
    this.name = name;
    FakeBroadcastChannel.instances.add(this);
  }

  postMessage(message: unknown): void {
    for (const channel of FakeBroadcastChannel.instances) {
      if (channel === this || channel.name !== this.name) continue;
      const deliver = () =>
        channel.onmessage?.(new MessageEvent("message", { data: message }));
      if (FakeBroadcastChannel.delayDelivery) {
        FakeBroadcastChannel.delayedDeliveries.push(deliver);
      } else {
        queueMicrotask(deliver);
      }
    }
  }

  close(): void {
    FakeBroadcastChannel.instances.delete(this);
  }

  static flushDelayed(): void {
    while (FakeBroadcastChannel.delayedDeliveries.length > 0) {
      FakeBroadcastChannel.delayedDeliveries.shift()?.();
    }
  }

  static reset(): void {
    for (const channel of FakeBroadcastChannel.instances) channel.close();
    FakeBroadcastChannel.delayedDeliveries.length = 0;
    FakeBroadcastChannel.delayDelivery = false;
  }
}

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
  beforeEach(async () => {
    window.sessionStorage.clear();
    FakeBroadcastChannel.reset();
    vi.stubGlobal("BroadcastChannel", FakeBroadcastChannel);
    vi.resetModules();
    ({ openRuntimeTerminal } = await import("./terminalChannel"));
  });

  it("reuses one validated tab owner in terminal start payloads", async () => {
    const firstTransport = transport();
    const secondTransport = transport();
    for (const runtimeTransport of [firstTransport, secondTransport]) {
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: terminalId,
        workspace_id: workspaceId,
        status: "attached",
      });
    }

    await openRuntimeTerminal(firstTransport, workspaceId, {
      cols: 80,
      rows: 24,
    });
    await openRuntimeTerminal(secondTransport, workspaceId, {
      cols: 100,
      rows: 30,
    });

    const firstStartBody = vi.mocked(firstTransport.post).mock.calls[0]?.[1];
    const secondStartBody = vi.mocked(secondTransport.post).mock.calls[0]?.[1];
    expect(firstStartBody).toEqual({
      cols: 80,
      rows: 24,
      owner_id: expect.stringMatching(/^[0-9a-f]{32}$/),
    });
    expect(secondStartBody).toEqual({
      cols: 100,
      rows: 30,
      owner_id: (firstStartBody as { owner_id: string }).owner_id,
    });
    expect(window.sessionStorage.getItem("yinshi:terminal-owner-id:v1")).toBe(
      (firstStartBody as { owner_id: string }).owner_id,
    );
  });

  it("rotates a cloned owner while its source tab remains live", async () => {
    const firstTransport = transport();
    const duplicateTransport = transport();
    for (const runtimeTransport of [firstTransport, duplicateTransport]) {
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: terminalId,
        workspace_id: workspaceId,
        status: "attached",
      });
    }

    await openRuntimeTerminal(firstTransport, workspaceId, {
      cols: 80,
      rows: 24,
    });
    const firstOwner = (
      vi.mocked(firstTransport.post).mock.calls[0]?.[1] as {
        owner_id: string;
      }
    ).owner_id;
    vi.resetModules();
    const duplicateModule = await import("./terminalChannel");
    await duplicateModule.openRuntimeTerminal(duplicateTransport, workspaceId, {
      cols: 80,
      rows: 24,
    });
    const duplicateOwner = (
      vi.mocked(duplicateTransport.post).mock.calls[0]?.[1] as {
        owner_id: string;
      }
    ).owner_id;

    expect(duplicateOwner).toMatch(/^[0-9a-f]{32}$/);
    expect(duplicateOwner).not.toBe(firstOwner);
    expect(window.sessionStorage.getItem("yinshi:terminal-owner-id:v1")).toBe(
      duplicateOwner,
    );
  });

  it("arbitrates simultaneous cloned-owner startup without sharing an owner", async () => {
    window.sessionStorage.setItem(
      "yinshi:terminal-owner-id:v1",
      "c".repeat(32),
    );
    const firstModule = await import("./terminalChannel");
    vi.resetModules();
    const secondModule = await import("./terminalChannel");
    const firstTransport = transport();
    const secondTransport = transport();
    for (const runtimeTransport of [firstTransport, secondTransport]) {
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: terminalId,
        workspace_id: workspaceId,
        status: "attached",
      });
    }

    await Promise.all([
      firstModule.openRuntimeTerminal(firstTransport, workspaceId, {
        cols: 80,
        rows: 24,
      }),
      secondModule.openRuntimeTerminal(secondTransport, workspaceId, {
        cols: 80,
        rows: 24,
      }),
    ]);
    const firstOwner = (
      vi.mocked(firstTransport.post).mock.calls[0]?.[1] as {
        owner_id: string;
      }
    ).owner_id;
    const secondOwner = (
      vi.mocked(secondTransport.post).mock.calls[0]?.[1] as {
        owner_id: string;
      }
    ).owner_id;

    expect(firstOwner).not.toBe(secondOwner);
  });

  it("converges when cloned-owner messages arrive after both claims", async () => {
    window.sessionStorage.setItem(
      "yinshi:terminal-owner-id:v1",
      "d".repeat(32),
    );
    FakeBroadcastChannel.delayDelivery = true;
    const firstModule = await import("./terminalChannel");
    vi.resetModules();
    const secondModule = await import("./terminalChannel");
    const firstTransport = transport();
    const secondTransport = transport();
    const firstReconnectTransport = transport();
    const secondReconnectTransport = transport();
    for (const runtimeTransport of [
      firstTransport,
      secondTransport,
      firstReconnectTransport,
      secondReconnectTransport,
    ]) {
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: terminalId,
        workspace_id: workspaceId,
        status: "attached",
      });
    }

    await Promise.all([
      firstModule.openRuntimeTerminal(firstTransport, workspaceId, {
        cols: 80,
        rows: 24,
      }),
      secondModule.openRuntimeTerminal(secondTransport, workspaceId, {
        cols: 80,
        rows: 24,
      }),
    ]);
    const firstInitialOwner = (
      vi.mocked(firstTransport.post).mock.calls[0]?.[1] as {
        owner_id: string;
      }
    ).owner_id;
    const secondInitialOwner = (
      vi.mocked(secondTransport.post).mock.calls[0]?.[1] as {
        owner_id: string;
      }
    ).owner_id;
    expect(firstInitialOwner).toBe(secondInitialOwner);

    FakeBroadcastChannel.flushDelayed();
    await Promise.resolve();
    await Promise.all([
      firstModule.openRuntimeTerminal(firstReconnectTransport, workspaceId, {
        cols: 80,
        rows: 24,
      }),
      secondModule.openRuntimeTerminal(secondReconnectTransport, workspaceId, {
        cols: 80,
        rows: 24,
      }),
    ]);
    const firstReconnectOwner = (
      vi.mocked(firstReconnectTransport.post).mock.calls[0]?.[1] as {
        owner_id: string;
      }
    ).owner_id;
    const secondReconnectOwner = (
      vi.mocked(secondReconnectTransport.post).mock.calls[0]?.[1] as {
        owner_id: string;
      }
    ).owner_id;

    expect(firstReconnectOwner).not.toBe(secondReconnectOwner);
    expect([firstReconnectOwner, secondReconnectOwner]).toContain(
      firstInitialOwner,
    );
  });

  it("retains its owner across reload after the prior document closes", async () => {
    const firstTransport = transport();
    const reloadTransport = transport();
    for (const runtimeTransport of [firstTransport, reloadTransport]) {
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: terminalId,
        workspace_id: workspaceId,
        status: "attached",
      });
    }

    await openRuntimeTerminal(firstTransport, workspaceId, {
      cols: 80,
      rows: 24,
    });
    const firstOwner = (
      vi.mocked(firstTransport.post).mock.calls[0]?.[1] as {
        owner_id: string;
      }
    ).owner_id;
    window.dispatchEvent(new Event("pagehide"));
    vi.resetModules();
    const reloadModule = await import("./terminalChannel");
    await reloadModule.openRuntimeTerminal(reloadTransport, workspaceId, {
      cols: 80,
      rows: 24,
    });
    const reloadOwner = (
      vi.mocked(reloadTransport.post).mock.calls[0]?.[1] as {
        owner_id: string;
      }
    ).owner_id;

    expect(reloadOwner).toBe(firstOwner);
  });

  it("reclaims its owner after a BFCache restore", async () => {
    const initialTransport = transport();
    const restoredTransport = transport();
    for (const runtimeTransport of [initialTransport, restoredTransport]) {
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: terminalId,
        workspace_id: workspaceId,
        status: "attached",
      });
    }

    await openRuntimeTerminal(initialTransport, workspaceId, {
      cols: 80,
      rows: 24,
    });
    const initialOwner = (
      vi.mocked(initialTransport.post).mock.calls[0]?.[1] as {
        owner_id: string;
      }
    ).owner_id;
    const pagehide = new Event("pagehide");
    Object.defineProperty(pagehide, "persisted", { value: true });
    window.dispatchEvent(pagehide);
    expect(FakeBroadcastChannel.instances.size).toBe(0);
    const pageshow = new Event("pageshow");
    Object.defineProperty(pageshow, "persisted", { value: true });
    window.dispatchEvent(pageshow);

    await openRuntimeTerminal(restoredTransport, workspaceId, {
      cols: 80,
      rows: 24,
    });
    const restoredOwner = (
      vi.mocked(restoredTransport.post).mock.calls[0]?.[1] as {
        owner_id: string;
      }
    ).owner_id;

    expect(restoredOwner).toBe(initialOwner);
    expect(FakeBroadcastChannel.instances.size).toBe(1);
  });

  it("reuses one in-memory owner when session storage is unavailable", async () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    vi.resetModules();
    ({ openRuntimeTerminal } = await import("./terminalChannel"));
    const originalSessionStorage = window.sessionStorage;
    const getItem = vi.fn(() => {
      throw new DOMException("Storage unavailable", "SecurityError");
    });
    const setItem = vi.fn(() => {
      throw new DOMException("Storage unavailable", "SecurityError");
    });
    Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      value: { getItem, setItem },
    });
    const firstTransport = transport();
    const secondTransport = transport();
    for (const runtimeTransport of [firstTransport, secondTransport]) {
      vi.mocked(runtimeTransport.post).mockResolvedValue({
        id: terminalId,
        workspace_id: workspaceId,
        status: "attached",
      });
    }

    try {
      await openRuntimeTerminal(firstTransport, workspaceId, {
        cols: 80,
        rows: 24,
      });
      await openRuntimeTerminal(secondTransport, workspaceId, {
        cols: 100,
        rows: 30,
      });
    } finally {
      Object.defineProperty(window, "sessionStorage", {
        configurable: true,
        value: originalSessionStorage,
      });
    }

    expect(getItem).toHaveBeenCalledTimes(1);
    expect(setItem).toHaveBeenCalledTimes(1);
    const firstStartBody = vi.mocked(firstTransport.post).mock
      .calls[0]?.[1] as {
      owner_id: string;
    };
    const secondStartBody = vi.mocked(secondTransport.post).mock
      .calls[0]?.[1] as {
      owner_id: string;
    };
    expect(firstStartBody.owner_id).toMatch(/^[0-9a-f]{32}$/);
    expect(secondStartBody.owner_id).toBe(firstStartBody.owner_id);
  });

  it("replaces a malformed stored owner before opening a terminal", async () => {
    window.sessionStorage.setItem(
      "yinshi:terminal-owner-id:v1",
      "malformed-owner",
    );
    const runtimeTransport = transport();
    vi.mocked(runtimeTransport.post).mockResolvedValue({
      id: terminalId,
      workspace_id: workspaceId,
      status: "attached",
    });

    await openRuntimeTerminal(runtimeTransport, workspaceId, {
      cols: 80,
      rows: 24,
    });

    const startBody = vi.mocked(runtimeTransport.post).mock.calls[0]?.[1] as {
      owner_id: string;
    };
    expect(startBody.owner_id).toMatch(/^[0-9a-f]{32}$/);
    expect(startBody.owner_id).not.toBe("malformed-owner");
    expect(window.sessionStorage.getItem("yinshi:terminal-owner-id:v1")).toBe(
      startBody.owner_id,
    );
  });

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
