import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Terminal, type ITheme } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import type { WorkspaceChangedFile, WorkspaceFileNode } from "../api/client";
import {
  openRuntimeTerminal,
  type RuntimeTerminalChannel,
} from "../runtime/terminalChannel";
import type { RuntimeTransport } from "../runtime/runtimeTransport";

type InspectorTab = "files" | "changes";
type ViewerMode = "preview" | "diff" | "edit";

interface WorkspaceInspectorProps {
  workspaceId: string;
  transport: RuntimeTransport;
  refreshKey: number;
  active?: boolean;
  className?: string;
  style?: CSSProperties;
}

const TERMINAL_HEIGHT_DEFAULT = 260;
const TERMINAL_HEIGHT_MIN = 140;
const TERMINAL_HEIGHT_MAX = 620;
const FILE_STATUS_REFRESH_MS = 15000;
const TERMINAL_RECONNECT_DELAY_MS = 2000;
const TERMINAL_ACCENT_COLOR = "#c23b22";
const TERMINAL_SELECTION_BACKGROUND = "rgba(194, 59, 34, 0.28)";

const LIGHT_TERMINAL_ANSI_COLORS: ITheme = {
  black: "#1a1410",
  red: "#a02e18",
  green: "#4f6f37",
  yellow: "#8a681d",
  blue: "#75543c",
  magenta: "#8a4d58",
  cyan: "#4d6b64",
  white: "#3d3228",
  brightBlack: "#8c7a64",
  brightRed: TERMINAL_ACCENT_COLOR,
  brightGreen: "#5f7f45",
  brightYellow: "#b8963e",
  brightBlue: "#946846",
  brightMagenta: "#a15f6c",
  brightCyan: "#5d8179",
  brightWhite: "#1a1410",
};

const DARK_TERMINAL_ANSI_COLORS: ITheme = {
  black: "#4a3f35",
  red: "#d4543d",
  green: "#b0bf80",
  yellow: "#d7b95d",
  blue: "#c79b75",
  magenta: "#d38a9b",
  cyan: "#8fbbb0",
  white: "#e0d1b8",
  brightBlack: "#a89478",
  brightRed: "#e86a52",
  brightGreen: "#c3d38f",
  brightYellow: "#e7ca72",
  brightBlue: "#d7ad88",
  brightMagenta: "#e0a0ae",
  brightCyan: "#a6d0c5",
  brightWhite: "#f7f0e3",
};

function storedTerminalHeight(): number {
  const raw = sessionStorage.getItem("yinshi-terminal-height");
  const value = Number(raw);
  if (Number.isFinite(value)) {
    return Math.min(TERMINAL_HEIGHT_MAX, Math.max(TERMINAL_HEIGHT_MIN, value));
  }
  return TERMINAL_HEIGHT_DEFAULT;
}

const CHANGE_LABELS: Partial<Record<WorkspaceChangedFile["kind"], string>> = {
  added: "A",
  copied: "C",
  deleted: "D",
  modified: "M",
  renamed: "R",
  untracked: "U",
};

function statusLabel(file: WorkspaceChangedFile): string {
  return (CHANGE_LABELS[file.kind] ?? file.status.trim()) || "?";
}

function countFiles(nodes: WorkspaceFileNode[]): number {
  return nodes.reduce((total, node) => {
    if (node.type === "file") return total + 1;
    return total + countFiles(node.children);
  }, 0);
}

function themeVariableColor(
  variableName: string,
  fallback: string,
  alpha?: number,
): string {
  const rawValue = getComputedStyle(document.documentElement)
    .getPropertyValue(variableName)
    .trim();
  const components = rawValue.split(/\s+/).map(Number);
  if (
    components.length !== 3 ||
    components.some((component) => !Number.isFinite(component))
  ) {
    return fallback;
  }
  const [red, green, blue] = components;
  if (alpha === undefined) {
    return `rgb(${red}, ${green}, ${blue})`;
  }
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

function terminalTheme(): ITheme {
  const ansiColors = document.documentElement.classList.contains("dark")
    ? DARK_TERMINAL_ANSI_COLORS
    : LIGHT_TERMINAL_ANSI_COLORS;
  const background = themeVariableColor("--gray-900", "#f0e6d3");
  return {
    ...ansiColors,
    background,
    foreground: themeVariableColor("--gray-200", "#2d2520"),
    cursor: TERMINAL_ACCENT_COLOR,
    cursorAccent: background,
    selectionBackground: TERMINAL_SELECTION_BACKGROUND,
    selectionForeground: themeVariableColor("--gray-50", "#0f0c09"),
    selectionInactiveBackground: themeVariableColor(
      "--gray-600",
      "rgba(168, 148, 120, 0.24)",
      0.24,
    ),
    scrollbarSliderBackground: themeVariableColor(
      "--gray-400",
      "rgba(107, 93, 79, 0.24)",
      0.24,
    ),
    scrollbarSliderHoverBackground: themeVariableColor(
      "--gray-400",
      "rgba(107, 93, 79, 0.38)",
      0.38,
    ),
    scrollbarSliderActiveBackground: themeVariableColor(
      "--gray-400",
      "rgba(107, 93, 79, 0.5)",
      0.5,
    ),
  };
}

function FileTree({
  nodes,
  selectedPath,
  onSelect,
}: {
  nodes: WorkspaceFileNode[];
  selectedPath: string | null;
  onSelect: (path: string) => void;
}) {
  return (
    <ul className="space-y-0.5">
      {nodes.map((node) => (
        <li key={node.path}>
          {node.type === "directory" ? (
            <details open className="group">
              <summary className="cursor-pointer select-none rounded px-2 py-1 text-xs font-medium text-gray-400 hover:bg-gray-800 hover:text-gray-200">
                {node.name}
              </summary>
              <div className="ml-3 border-l border-gray-800 pl-2">
                <FileTree
                  nodes={node.children}
                  selectedPath={selectedPath}
                  onSelect={onSelect}
                />
              </div>
            </details>
          ) : (
            <button
              type="button"
              onClick={() => onSelect(node.path)}
              className={`block w-full truncate rounded px-2 py-1 text-left text-xs ${
                selectedPath === node.path
                  ? "bg-blue-500/15 text-blue-200"
                  : "text-gray-300 hover:bg-gray-800 hover:text-gray-100"
              }`}
              title={node.path}
            >
              {node.name}
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

function FileViewer({
  workspaceId,
  transport,
  path,
  mode,
  onModeChange,
  onSaved,
}: {
  workspaceId: string;
  transport: RuntimeTransport;
  path: string | null;
  mode: ViewerMode;
  onModeChange: (mode: ViewerMode) => void;
  onSaved: () => void;
}) {
  const [content, setContent] = useState("");
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!path) {
      setContent("");
      setDraft("");
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    const encodedPath = encodeURIComponent(path);
    const endpoint =
      mode === "diff"
        ? `/api/workspaces/${workspaceId}/files/diff?path=${encodedPath}`
        : `/api/workspaces/${workspaceId}/files/preview?path=${encodedPath}`;
    transport
      .get<{ content?: string; diff?: string }>(endpoint)
      .then((response) => {
        if (cancelled) return;
        const value =
          mode === "diff" ? (response.diff ?? "") : (response.content ?? "");
        setContent(value);
        setDraft(value);
      })
      .catch((apiError) => {
        if (cancelled) return;
        setError(
          apiError instanceof Error ? apiError.message : "Failed to load file",
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mode, path, transport, workspaceId]);

  const save = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    setError(null);
    try {
      await transport.put(
        `/api/workspaces/${workspaceId}/files/content?path=${encodeURIComponent(path)}`,
        {
          content: draft,
        },
      );
      setContent(draft);
      onSaved();
    } catch (apiError) {
      setError(
        apiError instanceof Error ? apiError.message : "Failed to save file",
      );
    } finally {
      setLoading(false);
    }
  }, [draft, onSaved, path, transport, workspaceId]);

  if (!path) {
    return (
      <div className="p-3 text-xs text-gray-500">
        Select a file to preview it.
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col border-t border-gray-800 bg-gray-950/60">
      <div className="flex items-center gap-1 border-b border-gray-800 px-2 py-2">
        <div
          className="min-w-0 flex-1 truncate text-xs font-medium text-gray-300"
          title={path}
        >
          {path}
        </div>
        {(["preview", "diff", "edit"] as ViewerMode[]).map((viewerMode) => (
          <button
            key={viewerMode}
            type="button"
            onClick={() => onModeChange(viewerMode)}
            className={`rounded px-2 py-1 text-[11px] capitalize ${
              mode === viewerMode
                ? "bg-gray-700 text-gray-100"
                : "text-gray-500 hover:bg-gray-800 hover:text-gray-200"
            }`}
          >
            {viewerMode}
          </button>
        ))}
        {transport.runtime.location === "local" ||
        transport.runtime.location === "hosted" ? (
          <a
            href={`/api/workspaces/${workspaceId}/files/download?path=${encodeURIComponent(path)}`}
            className="rounded px-2 py-1 text-[11px] text-gray-500 hover:bg-gray-800 hover:text-gray-200"
          >
            Download
          </a>
        ) : null}
      </div>
      {error && (
        <div className="border-b border-red-900/40 bg-red-950/40 px-3 py-2 text-xs text-red-200">
          {error}
        </div>
      )}
      {loading && (
        <div className="px-3 py-2 text-xs text-gray-500">Loading...</div>
      )}
      {mode === "edit" ? (
        <div className="flex min-h-0 flex-1 flex-col">
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            spellCheck={false}
            className="min-h-0 flex-1 resize-none bg-gray-950 p-3 font-mono text-xs leading-relaxed text-gray-200 outline-none focus:ring-1 focus:ring-blue-500"
          />
          <div className="border-t border-gray-800 p-2 text-right">
            <button
              type="button"
              onClick={save}
              disabled={loading || draft === content}
              className="rounded bg-blue-600 px-3 py-1 text-xs font-medium text-white disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400"
            >
              Save
            </button>
          </div>
        </div>
      ) : (
        <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap p-3 font-mono text-xs leading-relaxed text-gray-200">
          {content || (mode === "diff" ? "No diff for this file." : "")}
        </pre>
      )}
    </div>
  );
}

function RuntimeTerminalPane({
  workspaceId,
  transport,
  active,
}: {
  workspaceId: string;
  transport: RuntimeTransport;
  active: boolean;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const channelRef = useRef<RuntimeTerminalChannel | null>(null);
  const [status, setStatus] = useState(active ? "Connecting..." : "Paused");
  const [connectionVersion, setConnectionVersion] = useState(0);

  const fit = useCallback(() => {
    const terminal = terminalRef.current;
    const fitAddon = fitRef.current;
    if (!terminal || !fitAddon) return;
    try {
      fitAddon.fit();
      const channel = channelRef.current;
      if (channel) {
        void channel.resize(terminal.cols, terminal.rows).catch(() => {
          setStatus("Terminal resize failed");
        });
      }
    } catch {
      // The terminal can be hidden during mobile drawer transitions.
    }
  }, []);

  useEffect(() => {
    if (!active) {
      setStatus("Paused");
      return;
    }
    const container = containerRef.current;
    if (!container) return;
    const abortController = new AbortController();
    let disposed = false;
    let reconnectTimer: number | null = null;
    setStatus("Connecting...");

    const terminal = new Terminal({
      cursorBlink: true,
      fontFamily: 'Menlo, Monaco, "Courier New", monospace',
      fontSize: 12,
      scrollback: 1000,
      theme: terminalTheme(),
    });
    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(container);
    terminalRef.current = terminal;
    fitRef.current = fitAddon;
    try {
      fitAddon.fit();
    } catch {
      // Initial layout can still be hidden while a drawer opens.
    }

    const inputSubscription = terminal.onData((data) => {
      const channel = channelRef.current;
      if (channel) {
        void channel
          .sendInput(data)
          .catch(() => setStatus("Terminal input failed"));
      }
    });
    const observer = new ResizeObserver(() => fit());
    observer.observe(container);
    const themeObserver = new MutationObserver(() => {
      terminal.options.theme = terminalTheme();
    });
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });

    void (async () => {
      try {
        const channel = await openRuntimeTerminal(transport, workspaceId, {
          cols: terminal.cols,
          rows: terminal.rows,
          signal: abortController.signal,
        });
        if (disposed) {
          await channel.close();
          return;
        }
        channelRef.current = channel;
        setStatus("Connected");
        for await (const event of channel.events()) {
          if (event.type === "terminal_ready") {
            if (typeof event.replay === "string" && event.replay) {
              terminal.write(event.replay);
            }
            setStatus("Connected");
            fit();
          } else if (
            event.type === "terminal_data" &&
            typeof event.data === "string"
          ) {
            terminal.write(event.data);
          } else if (event.type === "terminal_exit") {
            setStatus("Shell exited. Restart to open a new terminal.");
          } else if (event.type === "error") {
            setStatus(
              typeof event.error === "string" ? event.error : "Terminal error",
            );
          }
        }
        if (!disposed) {
          setStatus("Disconnected. Retrying...");
          reconnectTimer = window.setTimeout(
            () => setConnectionVersion((value) => value + 1),
            TERMINAL_RECONNECT_DELAY_MS,
          );
        }
      } catch (error) {
        if (
          disposed ||
          (error instanceof DOMException && error.name === "AbortError")
        )
          return;
        setStatus(
          error instanceof Error ? error.message : "Terminal connection failed",
        );
        reconnectTimer = window.setTimeout(
          () => setConnectionVersion((value) => value + 1),
          TERMINAL_RECONNECT_DELAY_MS,
        );
      }
    })();

    return () => {
      disposed = true;
      abortController.abort();
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      themeObserver.disconnect();
      observer.disconnect();
      inputSubscription.dispose();
      const channel = channelRef.current;
      channelRef.current = null;
      if (channel) void channel.close().catch(() => undefined);
      terminal.dispose();
      terminalRef.current = null;
      fitRef.current = null;
    };
  }, [active, connectionVersion, fit, transport, workspaceId]);

  const restart = useCallback(() => {
    terminalRef.current?.reset();
    const channel = channelRef.current;
    if (channel) {
      void channel.restart().catch(() => setStatus("Terminal restart failed"));
    } else {
      setConnectionVersion((value) => value + 1);
    }
  }, []);

  return (
    <div className="flex h-full min-h-0 flex-col bg-gray-900">
      <div className="flex items-center justify-between border-b border-gray-800 px-3 py-2">
        <div className="text-xs font-medium text-gray-300">Terminal</div>
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-gray-500">{status}</span>
          <button
            type="button"
            onClick={restart}
            className="rounded px-2 py-1 text-[11px] text-gray-400 hover:bg-gray-800 hover:text-gray-100"
          >
            Restart
          </button>
        </div>
      </div>
      <div ref={containerRef} className="min-h-0 flex-1 overflow-hidden p-2" />
    </div>
  );
}

export default function WorkspaceInspector({
  workspaceId,
  transport,
  refreshKey,
  active = true,
  className = "",
  style,
}: WorkspaceInspectorProps) {
  const [tab, setTab] = useState<InspectorTab>("files");
  const [files, setFiles] = useState<WorkspaceFileNode[]>([]);
  const [changes, setChanges] = useState<WorkspaceChangedFile[]>([]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [viewerMode, setViewerMode] = useState<ViewerMode>("preview");
  const [terminalHeight, setTerminalHeight] = useState(storedTerminalHeight);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!active) return;
    try {
      const [treeResponse, changedResponse] = await Promise.all([
        transport.get<{ files: WorkspaceFileNode[] }>(
          `/api/workspaces/${workspaceId}/files/tree`,
        ),
        transport.get<{ files: WorkspaceChangedFile[] }>(
          `/api/workspaces/${workspaceId}/files/changed`,
        ),
      ]);
      setFiles(treeResponse.files);
      setChanges(changedResponse.files);
      setError(null);
    } catch (apiError) {
      setError(
        apiError instanceof Error
          ? apiError.message
          : "Failed to load workspace files",
      );
    }
  }, [active, transport, workspaceId]);

  const refreshChanges = useCallback(async () => {
    if (!active) return;
    try {
      const changedResponse = await transport.get<{
        files: WorkspaceChangedFile[];
      }>(`/api/workspaces/${workspaceId}/files/changed`);
      setChanges(changedResponse.files);
      setError(null);
    } catch (apiError) {
      setError(
        apiError instanceof Error
          ? apiError.message
          : "Failed to load workspace changes",
      );
    }
  }, [active, transport, workspaceId]);

  useEffect(() => {
    void refresh();
  }, [refresh, refreshKey]);

  useEffect(() => {
    if (!active) return;
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        void refreshChanges();
      }
    }, FILE_STATUS_REFRESH_MS);
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("focus", onFocus);
    };
  }, [active, refresh, refreshChanges]);

  const beginTerminalResize = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      event.currentTarget.setPointerCapture(event.pointerId);
      const startY = event.clientY;
      const startHeight = terminalHeight;
      const onMove = (moveEvent: PointerEvent) => {
        const nextHeight = Math.min(
          TERMINAL_HEIGHT_MAX,
          Math.max(
            TERMINAL_HEIGHT_MIN,
            startHeight - (moveEvent.clientY - startY),
          ),
        );
        setTerminalHeight(nextHeight);
        sessionStorage.setItem("yinshi-terminal-height", String(nextHeight));
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [terminalHeight],
  );

  return (
    <aside
      style={style}
      className={`flex min-h-0 flex-col border-l border-gray-800 bg-gray-900 ${className}`}
    >
      <div className="flex items-center border-b border-gray-800 px-3 py-2">
        <button
          type="button"
          onClick={() => setTab("files")}
          className={`rounded px-2 py-1 text-xs font-medium ${tab === "files" ? "bg-gray-800 text-gray-100" : "text-gray-500 hover:text-gray-200"}`}
        >
          All files <span className="text-gray-500">{countFiles(files)}</span>
        </button>
        <button
          type="button"
          onClick={() => setTab("changes")}
          className={`ml-1 rounded px-2 py-1 text-xs font-medium ${tab === "changes" ? "bg-gray-800 text-gray-100" : "text-gray-500 hover:text-gray-200"}`}
        >
          Changes <span className="text-gray-500">{changes.length}</span>
        </button>
        <button
          type="button"
          onClick={() => void refresh()}
          className="ml-auto rounded px-2 py-1 text-[11px] text-gray-500 hover:bg-gray-800 hover:text-gray-200"
        >
          Refresh
        </button>
      </div>

      {error && (
        <div className="border-b border-red-900/40 bg-red-950/40 px-3 py-2 text-xs text-red-200">
          {error}
        </div>
      )}

      <div className="flex min-h-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-auto p-2">
          {tab === "files" ? (
            files.length ? (
              <FileTree
                nodes={files}
                selectedPath={selectedPath}
                onSelect={(path) => {
                  setSelectedPath(path);
                  setViewerMode("preview");
                }}
              />
            ) : (
              <div className="p-3 text-xs text-gray-500">No visible files.</div>
            )
          ) : changes.length ? (
            <div className="space-y-1">
              {changes.map((file) => (
                <button
                  key={`${file.status}-${file.path}`}
                  type="button"
                  onClick={() => {
                    setSelectedPath(file.path);
                    setViewerMode("diff");
                  }}
                  className={`flex w-full items-center gap-2 rounded px-2 py-1 text-left text-xs ${
                    selectedPath === file.path
                      ? "bg-blue-500/15 text-blue-200"
                      : "text-gray-300 hover:bg-gray-800 hover:text-gray-100"
                  }`}
                  title={file.path}
                >
                  <span className="w-5 shrink-0 rounded bg-gray-800 px-1 text-center font-mono text-[10px] text-gray-400">
                    {statusLabel(file)}
                  </span>
                  <span className="truncate">{file.path}</span>
                </button>
              ))}
            </div>
          ) : (
            <div className="p-3 text-xs text-gray-500">
              No uncommitted changes.
            </div>
          )}
        </div>
        <div className="h-[42%] min-h-[180px] max-h-[50%]">
          <FileViewer
            workspaceId={workspaceId}
            transport={transport}
            path={selectedPath}
            mode={viewerMode}
            onModeChange={setViewerMode}
            onSaved={() => void refresh()}
          />
        </div>
      </div>

      <div
        role="separator"
        aria-label="Resize terminal"
        onPointerDown={beginTerminalResize}
        className="h-1.5 cursor-row-resize border-y border-gray-800 bg-gray-800/80 hover:bg-blue-500/40"
      />
      <div style={{ height: terminalHeight }} className="shrink-0">
        <RuntimeTerminalPane
          workspaceId={workspaceId}
          transport={transport}
          active={active}
        />
      </div>
    </aside>
  );
}
