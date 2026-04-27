import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const WORKSPACE_ID = "workspace-1";
const WORKSPACE_TERMINAL_URL = `ws://test.local/api/workspaces/${WORKSPACE_ID}/terminal`;
const TERMINAL_RECONNECT_DELAY_MS = 2000;
const TERMINAL_TEMPORARY_FAILURE_CLOSE_CODE = 1011;

const LIGHT_TERMINAL_BACKGROUND = "rgb(247, 240, 227)";
const LIGHT_TERMINAL_FOREGROUND = "rgb(45, 37, 32)";
const DARK_TERMINAL_BACKGROUND = "rgb(15, 12, 9)";
const DARK_TERMINAL_FOREGROUND = "rgb(224, 209, 184)";

const apiGetMock = vi.fn();
const terminalResetMock = vi.fn();
const terminalInstances: Array<{ options: { theme?: unknown } }> = [];

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
    options: { theme?: unknown };

    constructor(options: { theme?: unknown } = {}) {
      this.options = options;
      terminalInstances.push(this);
    }

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

function setTerminalThemeVariables(background: string, foreground: string): void {
  const rootStyle = document.documentElement.style;
  rootStyle.setProperty("--gray-950", background);
  rootStyle.setProperty("--gray-200", foreground);
  rootStyle.setProperty("--gray-50", foreground);
  rootStyle.setProperty("--gray-600", foreground);
  rootStyle.setProperty("--gray-400", foreground);
}

describe("WorkspaceInspector terminal", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    document.documentElement.classList.remove("dark");
    document.documentElement.removeAttribute("style");
  });

  beforeEach(() => {
    vi.clearAllMocks();
    FakeWebSocket.instances = [];
    terminalInstances.length = 0;
    apiGetMock.mockResolvedValue({ files: [] });
    setTerminalThemeVariables("247 240 227", "45 37 32");
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

  it("updates terminal colors when the document theme changes", async () => {
    render(<WorkspaceInspector workspaceId={WORKSPACE_ID} refreshKey={0} />);

    await waitForWebSocketCount(1);
    expect(terminalInstances[0].options.theme).toMatchObject({
      background: LIGHT_TERMINAL_BACKGROUND,
      foreground: LIGHT_TERMINAL_FOREGROUND,
      cursor: "#c23b22",
    });

    setTerminalThemeVariables("15 12 9", "224 209 184");
    document.documentElement.classList.add("dark");

    await waitFor(() => {
      expect(terminalInstances[0].options.theme).toMatchObject({
        background: DARK_TERMINAL_BACKGROUND,
        foreground: DARK_TERMINAL_FOREGROUND,
        cursor: "#c23b22",
      });
    });
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
