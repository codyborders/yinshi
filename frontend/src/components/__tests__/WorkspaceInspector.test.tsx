import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeTransport } from "../../runtime/runtimeTransport";

const WORKSPACE_ID = "workspace-1";
const TERMINAL_RECONNECT_DELAY_MS = 2000;

const LIGHT_TERMINAL_BACKGROUND = "rgb(240, 230, 211)";
const LIGHT_TERMINAL_FOREGROUND = "rgb(45, 37, 32)";
const DARK_TERMINAL_BACKGROUND = "rgb(26, 20, 16)";
const DARK_TERMINAL_FOREGROUND = "rgb(224, 209, 184)";
const TERMINAL_CURSOR = "#c23b22";

type TerminalMock = { options: { theme?: unknown } };

const apiGetMock = vi.fn();
const openRuntimeTerminalMock = vi.fn();
const terminalRestartMock = vi.fn();
const terminalResetMock = vi.fn();
const terminalInstances: TerminalMock[] = [];
const terminalEventFinishers: Array<() => void> = [];
const runtimeTransport: RuntimeTransport = {
  runtime: { location: "hosted" },
  get: (...args) => apiGetMock(...args),
  post: vi.fn(),
  patch: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
  upload: vi.fn(),
};

vi.mock("../../api/client", () => ({
  api: {
    get: (...args: unknown[]) => apiGetMock(...args),
  },
}));

vi.mock("../../runtime/terminalChannel", () => ({
  openRuntimeTerminal: (...args: unknown[]) => openRuntimeTerminalMock(...args),
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

class FakeResizeObserver {
  observe(): void {
    return undefined;
  }

  disconnect(): void {
    return undefined;
  }
}

async function waitForTerminalChannelCount(count: number): Promise<void> {
  await waitFor(() => {
    expect(openRuntimeTerminalMock).toHaveBeenCalledTimes(count);
  });
}

function runtimeTerminalChannel() {
  let finishEvents: () => void = () => undefined;
  const eventsFinished = new Promise<void>((resolve) => {
    finishEvents = resolve;
  });
  terminalEventFinishers.push(finishEvents);
  return {
    close: vi.fn(async () => finishEvents()),
    events: async function* () {
      await eventsFinished;
    },
    resize: vi.fn().mockResolvedValue(undefined),
    restart: terminalRestartMock.mockResolvedValue(undefined),
    sendInput: vi.fn().mockResolvedValue(undefined),
  };
}

function setTerminalThemeVariables(
  background: string,
  foreground: string,
): void {
  const rootStyle = document.documentElement.style;
  rootStyle.setProperty("--gray-900", background);
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
    terminalEventFinishers.length = 0;
    terminalInstances.length = 0;
    apiGetMock.mockResolvedValue({ files: [] });
    openRuntimeTerminalMock.mockImplementation(async () =>
      runtimeTerminalChannel(),
    );
    setTerminalThemeVariables("240 230 211", "45 37 32");
    vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  });

  it("restarts the location-aware terminal channel", async () => {
    render(
      <WorkspaceInspector
        workspaceId={WORKSPACE_ID}
        transport={runtimeTransport}
        refreshKey={0}
      />,
    );

    await waitForTerminalChannelCount(1);
    expect(openRuntimeTerminalMock).toHaveBeenCalledWith(
      runtimeTransport,
      WORKSPACE_ID,
      expect.objectContaining({ cols: 80, rows: 24 }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Restart" }));

    expect(terminalResetMock).toHaveBeenCalled();
    expect(terminalRestartMock).toHaveBeenCalledOnce();
    expect(openRuntimeTerminalMock).toHaveBeenCalledOnce();
  });

  it("updates terminal colors when the document theme changes", async () => {
    render(
      <WorkspaceInspector
        workspaceId={WORKSPACE_ID}
        transport={runtimeTransport}
        refreshKey={0}
      />,
    );

    await waitForTerminalChannelCount(1);
    expect(terminalInstances[0].options.theme).toMatchObject({
      background: LIGHT_TERMINAL_BACKGROUND,
      foreground: LIGHT_TERMINAL_FOREGROUND,
      cursor: TERMINAL_CURSOR,
    });

    setTerminalThemeVariables("26 20 16", "224 209 184");
    document.documentElement.classList.add("dark");

    await waitFor(() => {
      expect(terminalInstances[0].options.theme).toMatchObject({
        background: DARK_TERMINAL_BACKGROUND,
        foreground: DARK_TERMINAL_FOREGROUND,
        cursor: TERMINAL_CURSOR,
      });
    });
  });

  it("automatically retries when the terminal event journal disconnects", async () => {
    render(
      <WorkspaceInspector
        workspaceId={WORKSPACE_ID}
        transport={runtimeTransport}
        refreshKey={0}
      />,
    );

    await waitForTerminalChannelCount(1);

    vi.useFakeTimers();
    try {
      await act(async () => {
        terminalEventFinishers[0]();
        await Promise.resolve();
      });

      expect(screen.getByText("Disconnected. Retrying...")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(TERMINAL_RECONNECT_DELAY_MS);
      });

      expect(openRuntimeTerminalMock).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
