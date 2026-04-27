import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

  closeFromServer(): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.dispatchEvent(new Event("close"));
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
    render(<WorkspaceInspector workspaceId="workspace-1" refreshKey={0} />);

    await waitFor(() => {
      expect(FakeWebSocket.instances).toHaveLength(1);
    });

    FakeWebSocket.instances[0].closeFromServer();

    expect(await screen.findByText("Disconnected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restart" }));

    await waitFor(() => {
      expect(FakeWebSocket.instances).toHaveLength(2);
    });
    expect(terminalResetMock).toHaveBeenCalled();
    expect(FakeWebSocket.instances[1].url).toBe("ws://test.local/api/workspaces/workspace-1/terminal");
    expect(screen.getByText("Connecting...")).toBeInTheDocument();
  });
});
