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
function transport(
  runtime: RuntimeTransport["runtime"] = { location: "hosted" },
): RuntimeTransport {
  return {
    runtime,
    get: (...args) => apiGetMock(...args),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    upload: vi.fn(),
    close: vi.fn(),
  };
}

const runtimeTransport = transport();

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
    Reflect.deleteProperty(window, "yinshiDesktop");
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

  it("labels each requested inspector view", async () => {
    const rendered = render(
      <WorkspaceInspector
        workspaceId={WORKSPACE_ID}
        transport={runtimeTransport}
        refreshKey={0}
        view="files"
      />,
    );

    await waitFor(() => {
      expect(
        screen.getByRole("complementary", { name: "Workspace files" }),
      ).toBeInTheDocument();
    });

    rendered.rerender(
      <WorkspaceInspector
        workspaceId={WORKSPACE_ID}
        transport={runtimeTransport}
        refreshKey={0}
        view="terminal"
      />,
    );
    await waitFor(() => {
      expect(
        screen.getByRole("complementary", { name: "Workspace terminal" }),
      ).toBeInTheDocument();
    });

    rendered.rerender(
      <WorkspaceInspector
        workspaceId={WORKSPACE_ID}
        transport={runtimeTransport}
        refreshKey={0}
      />,
    );
    await waitFor(() => {
      expect(
        screen.getByRole("complementary", { name: "Workspace inspector" }),
      ).toBeInTheDocument();
    });
  });

  it("renders only requested view and avoids terminal connections in files mode", async () => {
    render(
      <WorkspaceInspector
        workspaceId={WORKSPACE_ID}
        transport={runtimeTransport}
        refreshKey={0}
        view="files"
      />,
    );

    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith(
        `/api/workspaces/${WORKSPACE_ID}/files/tree`,
      );
    });
    expect(screen.queryByText("Terminal")).toBeNull();
    expect(openRuntimeTerminalMock).not.toHaveBeenCalled();

    cleanup();
    apiGetMock.mockClear();
    render(
      <WorkspaceInspector
        workspaceId={WORKSPACE_ID}
        transport={runtimeTransport}
        refreshKey={0}
        view="terminal"
      />,
    );

    await waitForTerminalChannelCount(1);
    expect(apiGetMock).not.toHaveBeenCalled();
    expect(screen.getByText("Terminal")).toBeInTheDocument();
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

  it("hides direct file downloads for managed runtimes", async () => {
    apiGetMock.mockImplementation(async (path: string) => {
      if (path.endsWith("/files/tree")) {
        return {
          files: [{ name: "notes.txt", path: "notes.txt", type: "file" }],
        };
      }
      return path.endsWith("/files/changed")
        ? { files: [] }
        : { content: "managed file" };
    });

    render(
      <WorkspaceInspector
        workspaceId={WORKSPACE_ID}
        transport={transport({
          location: "managed",
          runnerPublicKey: "MeAwP9ZBjS-MDni5HyLoyu0Pvkhlbc9HZ-SDT3Abj2I",
        })}
        refreshKey={0}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "notes.txt" }));

    expect(screen.queryByRole("link", { name: "Download" })).toBeNull();
  });

  it.each([
    ["local", { location: "local" } as const],
    ["desktop-hosted", { location: "hosted" } as const],
  ])("keeps direct file downloads for %s runtimes", async (label, runtime) => {
    if (label === "desktop-hosted") {
      Object.defineProperty(window, "yinshiDesktop", {
        configurable: true,
        value: {},
      });
    }
    apiGetMock.mockImplementation(async (path: string) => {
      if (path.endsWith("/files/tree")) {
        return {
          files: [{ name: "notes.txt", path: "notes.txt", type: "file" }],
        };
      }
      return path.endsWith("/files/changed")
        ? { files: [] }
        : { content: "downloadable file" };
    });

    render(
      <WorkspaceInspector
        workspaceId={WORKSPACE_ID}
        transport={transport(runtime)}
        refreshKey={0}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "notes.txt" }));

    expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute(
      "href",
      `/api/workspaces/${WORKSPACE_ID}/files/download?path=notes.txt`,
    );
  });
});
