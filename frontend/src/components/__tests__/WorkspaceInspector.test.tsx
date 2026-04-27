import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const WORKSPACE_ID = "workspace-1";
const WORKSPACE_TERMINAL_URL = `ws://test.local/api/workspaces/${WORKSPACE_ID}/terminal`;
const TERMINAL_RECONNECT_DELAY_MS = 2000;
const TERMINAL_TEMPORARY_FAILURE_CLOSE_CODE = 1011;

const apiGetMock = vi.fn();
const terminalResetMock = vi.fn();

vi.mock("../../api/client", () => ({
  api: {
    get: (...args: unknown[]) => apiGetMock(...args),
  },
  workspaceTerminalUrl: (workspaceId: string) => `ws://test.local/api/workspaces/${workspaceId}/terminal`,
}));

vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    cols = 80;
    rows = 24;

    loadAddon(): void {
      return undefined;
    }

    open(): void {
      return undefined;
    }

    onData(): { dispose: () => void } {
      return { dispose: vi.fn() };
    }

    write(): void {
      return undefined;
    }

    reset(): void {
      terminalResetMock();
    }

    dispose(): void {
      return undefined;
    }
  },
}));

vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class {
    fit(): void {
      return undefined;
    }
  },
}));

import WorkspaceInspector from "../WorkspaceInspector";

class FakeWebSocket extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.CONNECTING;
  readonly sentMessages: string[] = [];
  readonly url: string;

  constructor(url: string | URL) {
    super();
    this.url = String(url);
    FakeWebSocket.instances.push(this);
  }

  send(message: string): void {
    this.sentMessages.push(message);
  }

  close(): void {
    this.readyState = FakeWebSocket.CLOSED;
  }

  closeFromServer(code = 1006): void {
    this.readyState = FakeWebSocket.CLOSED;
    const event = new Event("close") as Event & { code: number };
    Object.defineProperty(event, "code", { value: code });
    this.dispatchEvent(event);
  }
}

class FakeResizeObserver {
  observe(): void {
    return undefined;
  }

  disconnect(): void {
    return undefined;
  }
}

async function waitForWebSocketCount(count: number): Promise<void> {
  await waitFor(() => {
    expect(FakeWebSocket.instances).toHaveLength(count);
  });
}

describe("WorkspaceInspector terminal", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    FakeWebSocket.instances = [];
    apiGetMock.mockResolvedValue({ files: [] });
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  });

  it("reconnects when Restart is clicked after the terminal socket closes", async () => {
    render(<WorkspaceInspector workspaceId={WORKSPACE_ID} refreshKey={0} />);

    await waitForWebSocketCount(1);

    act(() => {
      FakeWebSocket.instances[0].closeFromServer();
    });

    expect(screen.getByText("Disconnected. Retrying...")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restart" }));

    await waitForWebSocketCount(2);
    expect(terminalResetMock).toHaveBeenCalled();
    expect(FakeWebSocket.instances[1].url).toBe(WORKSPACE_TERMINAL_URL);
    expect(screen.getByText("Connecting...")).toBeInTheDocument();
  });

  it("automatically retries when the terminal runtime is temporarily unavailable", async () => {
    render(<WorkspaceInspector workspaceId={WORKSPACE_ID} refreshKey={0} />);

    await waitForWebSocketCount(1);

    vi.useFakeTimers();
    try {
      act(() => {
        FakeWebSocket.instances[0].closeFromServer(TERMINAL_TEMPORARY_FAILURE_CLOSE_CODE);
      });

      expect(screen.getByText("Terminal unavailable. Retrying...")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(TERMINAL_RECONNECT_DELAY_MS);
      });

      expect(FakeWebSocket.instances).toHaveLength(2);
      expect(FakeWebSocket.instances[1].url).toBe(WORKSPACE_TERMINAL_URL);
    } finally {
      vi.useRealTimers();
    }
  });
});
