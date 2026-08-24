import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useParams } from "react-router-dom";
import {
  type Message,
  type SessionInfo,
  type ThinkingLevel,
} from "../api/client";
import ChatView from "../components/ChatView";
import WorkspaceInspector from "../components/WorkspaceInspector";
import { useAgentStream, type ChatMessage } from "../hooks/useAgentStream";
import { useAuth } from "../hooks/useAuth";
import { useCatalog } from "../hooks/useCatalog";
import { usePiCommands } from "../hooks/usePiCommands";
import {
  DEFAULT_SESSION_MODEL,
  availableSessionModelsMarkdown,
  describeSessionModel,
  formatSessionModelOptionLabel,
  formatThinkingLevelLabel,
  getModelThinkingLevels,
  getSessionModelOption,
  resolveSessionModelKey,
} from "../models/sessionModels";
import {
  initializeSessionModelPreference,
  rememberSessionModel,
} from "../models/sessionModelPreference";
import { findActiveRuntimePromptRun } from "../runtime/promptStream";
import { loadSessionHistory } from "../runtime/sessionHistory";
import {
  invalidateSessionHistoryCache,
  isSessionHistoryCacheAvailable,
} from "../runtime/sessionHistoryCacheClient";
import { useRuntimeResource } from "../runtime/useRuntimeResource";
import { parseStoredTurnBlocks } from "../utils/turnEvents";

let cmdIdCounter = 0;
function nextCmdId(): string {
  return `cmd-${Date.now()}-${++cmdIdCounter}`;
}

const INSPECTOR_WIDTH_DEFAULT = 420;
const INSPECTOR_WIDTH_MIN = 320;
const INSPECTOR_WIDTH_MAX = 760;
const DESKTOP_INSPECTOR_QUERY = "(min-width: 1024px)";
const LEGACY_PI_CONTEXT_MESSAGE =
  "This session predates durable Pi context and cannot continue with exact model context. Start a new session in this workspace.";
const SESSION_HISTORY_PERFORMANCE_MARKS = [
  "yinshi:session-history-start",
  "yinshi:session-history-cache-rendered",
  "yinshi:session-history-live-rendered",
  "yinshi:session-history-live-failed",
] as const;

type SessionHistoryPerformanceMark =
  (typeof SESSION_HISTORY_PERFORMANCE_MARKS)[number];

function markSessionHistory(name: SessionHistoryPerformanceMark): void {
  if (typeof globalThis.performance?.mark === "function") {
    globalThis.performance.mark(name);
  }
}

function startSessionHistoryPerformanceTiming(): void {
  if (typeof globalThis.performance?.clearMarks === "function") {
    for (const name of SESSION_HISTORY_PERFORMANCE_MARKS) {
      globalThis.performance.clearMarks(name);
    }
  }
  markSessionHistory("yinshi:session-history-start");
}

function mapStoredHistory(
  history: Message[],
  activeRunId: string | null,
): ChatMessage[] {
  const mapped = history.map((message) => {
    let blockIndex = 0;
    const blocks =
      message.role === "assistant"
        ? parseStoredTurnBlocks(
            message.full_message,
            () => `${message.id}-block-${++blockIndex}`,
          )
        : [];
    return {
      id: message.id,
      role: message.role as ChatMessage["role"],
      content: message.content || "",
      blocks,
      turnId: message.turn_id,
      timestamp: new Date(message.created_at).getTime(),
    };
  });
  return activeRunId === null
    ? mapped
    : mapped.filter(
        (message) =>
          message.role !== "assistant" || message.turnId !== activeRunId,
      );
}

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    ) {
      return false;
    }
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    ) {
      return;
    }
    const mediaQuery = window.matchMedia(query);
    const updateMatches = () => setMatches(mediaQuery.matches);
    updateMatches();
    mediaQuery.addEventListener("change", updateMatches);
    return () => mediaQuery.removeEventListener("change", updateMatches);
  }, [query]);

  return matches;
}

function storedInspectorWidth(): number {
  const raw = sessionStorage.getItem("yinshi-inspector-width");
  const value = Number(raw);
  if (Number.isFinite(value)) {
    return Math.min(INSPECTOR_WIDTH_MAX, Math.max(INSPECTOR_WIDTH_MIN, value));
  }
  return INSPECTOR_WIDTH_DEFAULT;
}

export default function Session() {
  const { id: encodedSessionId } = useParams<{ id: string }>();
  const { status: authStatus, userId } = useAuth();
  const runtimeState = useRuntimeResource(encodedSessionId);
  const runtimeResource = runtimeState.resource;
  const id = runtimeResource?.resourceId;
  const transport = runtimeResource?.transport;
  const {
    messages,
    sendPrompt,
    cancel,
    streaming,
    setMessages,
    bootstrapSession,
  } = useAgentStream(id, transport);
  const { catalog, loading: loadingCatalog } = useCatalog(transport);
  const piCommands = usePiCommands(transport);
  const [sessionModel, setSessionModel] = useState(DEFAULT_SESSION_MODEL);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyRevalidating, setHistoryRevalidating] = useState(false);
  const [metadataError, setMetadataError] = useState<string | null>(null);
  const [updatingModel, setUpdatingModel] = useState(false);
  const [pendingModelSelection, setPendingModelSelection] = useState<
    string | null
  >(null);
  const [thinkingOverride, setThinkingOverride] =
    useState<ThinkingLevel | null>(null);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [piContextVersion, setPiContextVersion] = useState(0);
  const [workspacePanelOpen, setWorkspacePanelOpen] = useState(false);
  const [inspectorWidth, setInspectorWidth] = useState(storedInspectorWidth);
  const [fileRefreshKey, setFileRefreshKey] = useState(0);
  const isDesktopInspectorVisible = useMediaQuery(DESKTOP_INSPECTOR_QUERY);
  const wasStreamingRef = useRef(false);
  const historyGenerationRef = useRef(0);
  const cacheRefreshRef = useRef<{
    key: string;
    promise: Promise<void>;
  } | null>(null);
  const historyCacheEligible = Boolean(
    id &&
    transport?.runtime.location === "managed" &&
    transport.runtime.historyBundleSupported === true &&
    typeof DecompressionStream === "function" &&
    authStatus === "authenticated" &&
    userId &&
    isSessionHistoryCacheAvailable(),
  );

  useEffect(() => {
    setLoadingHistory(true);
    setHistoryError(null);
    setHistoryRevalidating(false);
    setMetadataError(null);
  }, [encodedSessionId]);

  useEffect(() => {
    if (runtimeState.error) {
      setHistoryError(runtimeState.error);
      setLoadingHistory(false);
    }
  }, [runtimeState.error]);

  // Load existing message history
  useEffect(() => {
    if (!id || !transport) return;
    let cancelled = false;
    const runtimeTransport = transport;
    const runtimeSessionId = id;
    const generation = ++historyGenerationRef.current;
    startSessionHistoryPerformanceTiming();
    const isStale = () =>
      cancelled || historyGenerationRef.current !== generation;

    async function loadHistory() {
      const expectsBundledActiveRun =
        runtimeTransport.runtime.location === "managed" &&
        runtimeTransport.runtime.historyBundleSupported === true &&
        typeof DecompressionStream === "function";
      let bundledActiveRunId: string | null = null;
      let receivedBundledActiveRun = false;
      let receivedCachedHistory = false;
      const discoveryPromise: Promise<{
        activeRunId: string | null;
        failed: boolean;
      }> = expectsBundledActiveRun
        ? Promise.resolve({ activeRunId: null, failed: false })
        : findActiveRuntimePromptRun(runtimeTransport, runtimeSessionId).then(
            (activeRunId) => ({ activeRunId, failed: false }),
            () => ({ activeRunId: null, failed: true }),
          );
      const historyPromise: Promise<
        { ok: true; history: Message[] } | { ok: false }
      > = loadSessionHistory(runtimeTransport, runtimeSessionId, {
        isCancelled: isStale,
        cacheUserId: historyCacheEligible ? (userId ?? undefined) : undefined,
        onCachedHistory: (history, activeRunId) => {
          if (isStale()) return;
          try {
            bootstrapSession(
              mapStoredHistory(history, activeRunId),
              activeRunId,
            );
            receivedCachedHistory = true;
            setHistoryRevalidating(true);
            setHistoryError(null);
            setLoadingHistory(false);
            markSessionHistory("yinshi:session-history-cache-rendered");
          } catch {
            return;
          }
        },
        onBundledActiveRun: (activeRunId) => {
          bundledActiveRunId = activeRunId;
          receivedBundledActiveRun = true;
        },
      }).then(
        (history) => ({ ok: true, history }),
        () => ({ ok: false }),
      );
      const [discovery, historyResult] = await Promise.all([
        discoveryPromise,
        historyPromise,
      ]);
      if (isStale()) return;

      let { activeRunId } = discovery;
      let discoveryFailed = discovery.failed;
      if (receivedBundledActiveRun) {
        activeRunId = bundledActiveRunId;
        discoveryFailed = false;
      }
      if (!historyResult.ok) {
        markSessionHistory("yinshi:session-history-live-failed");
        if (receivedCachedHistory) {
          setHistoryError("Failed to refresh session history.");
          setHistoryRevalidating(false);
          setLoadingHistory(false);
          return;
        }
        if (expectsBundledActiveRun && !receivedBundledActiveRun) {
          try {
            activeRunId = await findActiveRuntimePromptRun(
              runtimeTransport,
              runtimeSessionId,
            );
          } catch {
            activeRunId = null;
          }
          if (isStale()) return;
        }
        if (activeRunId !== null) bootstrapSession([], activeRunId);
        setHistoryError("Failed to load session history.");
        setHistoryRevalidating(false);
        setLoadingHistory(false);
        return;
      }

      try {
        if (activeRunId === null && !receivedBundledActiveRun) {
          try {
            activeRunId = await findActiveRuntimePromptRun(
              runtimeTransport,
              runtimeSessionId,
            );
            discoveryFailed = false;
          } catch {
            discoveryFailed = true;
          }
        }
        if (isStale()) return;
        bootstrapSession(
          mapStoredHistory(historyResult.history, activeRunId),
          activeRunId,
        );
        markSessionHistory("yinshi:session-history-live-rendered");
        setHistoryError(
          discoveryFailed ? "Failed to resume the active response." : null,
        );
      } catch {
        if (!isStale()) {
          markSessionHistory("yinshi:session-history-live-failed");
          if (activeRunId !== null) bootstrapSession([], activeRunId);
          setHistoryError("Failed to load session history.");
        }
      } finally {
        if (!isStale()) {
          setHistoryRevalidating(false);
          setLoadingHistory(false);
        }
      }
    }

    loadHistory();
    return () => {
      cancelled = true;
    };
  }, [bootstrapSession, historyCacheEligible, id, transport, userId]);

  useEffect(() => {
    if (!id || !transport) return;
    let cancelled = false;
    const runtimeTransport = transport;
    setMetadataError(null);

    async function loadSession() {
      try {
        const session = await runtimeTransport.get<SessionInfo>(
          `/api/sessions/${id}`,
        );
        if (cancelled) return;
        setSessionModel(session.model);
        initializeSessionModelPreference(userId, session.model);
        setWorkspaceId(session.workspace_id);
        setPiContextVersion(session.pi_context_version);
      } catch {
        if (!cancelled) setMetadataError("Failed to load session metadata.");
      }
    }

    loadSession();
    return () => {
      cancelled = true;
    };
  }, [id, transport, userId]);

  useEffect(() => {
    setPendingModelSelection(null);
    setThinkingOverride(null);
  }, [id]);

  useEffect(() => {
    wasStreamingRef.current = false;
  }, [id, transport]);

  useEffect(() => {
    if (wasStreamingRef.current && !streaming) {
      setFileRefreshKey((value) => value + 1);
      if (
        id &&
        transport?.runtime.location === "managed" &&
        transport.runtime.historyBundleSupported === true &&
        typeof DecompressionStream === "function" &&
        historyCacheEligible &&
        userId
      ) {
        const runtimeTransport = transport;
        const runtimeSessionId = id;
        const generation = historyGenerationRef.current;
        const key = `${userId.length}:${userId}${runtimeSessionId}`;
        if (cacheRefreshRef.current?.key !== key) {
          const promise = loadSessionHistory(
            runtimeTransport,
            runtimeSessionId,
            {
              cacheUserId: userId,
              skipCacheRead: true,
              isCancelled: () => historyGenerationRef.current !== generation,
            },
          )
            .then(() => undefined)
            .catch(() => undefined)
            .finally(() => {
              if (cacheRefreshRef.current?.promise === promise) {
                cacheRefreshRef.current = null;
              }
            });
          cacheRefreshRef.current = { key, promise };
        }
      }
    }
    wasStreamingRef.current = streaming;
  }, [historyCacheEligible, id, streaming, transport, userId]);

  const addSystemMessage = useCallback(
    (content: string) => {
      setMessages((prev) => [
        ...prev,
        {
          id: nextCmdId(),
          role: "assistant" as const,
          content,
          blocks: [{ id: nextCmdId(), type: "text" as const, text: content }],
          timestamp: Date.now(),
        },
      ]);
    },
    [setMessages],
  );

  const updateSessionModel = useCallback(
    async (requestedModel: string, announce: boolean) => {
      if (!id || !transport) return false;
      if (!catalog) return false;

      const connectedProviderIds = new Set(
        catalog.providers
          .filter((provider) => provider.connected)
          .map((provider) => provider.id),
      );
      const providerLabelById = new Map(
        catalog.providers.map(
          (provider) => [provider.id, provider.label] as const,
        ),
      );
      const resolvedModel = resolveSessionModelKey(
        requestedModel,
        catalog.models,
        connectedProviderIds,
      );
      if (!resolvedModel) {
        if (announce) {
          addSystemMessage(
            "Unknown model. Available models:\n\n" +
              availableSessionModelsMarkdown(catalog.models),
          );
        }
        return false;
      }
      const resolvedModelOption = getSessionModelOption(
        resolvedModel,
        catalog.models,
      );
      if (!resolvedModelOption) {
        if (announce) {
          addSystemMessage("Failed to resolve the requested model.");
        }
        return false;
      }
      if (!connectedProviderIds.has(resolvedModelOption.provider)) {
        const providerLabel =
          providerLabelById.get(resolvedModelOption.provider) ||
          resolvedModelOption.provider;
        if (announce) {
          addSystemMessage(
            `Model ${describeSessionModel(resolvedModelOption.ref, catalog.models)} requires a ${providerLabel} connection in Settings.`,
          );
        }
        return false;
      }

      setUpdatingModel(true);
      try {
        const updated = await transport.patch<{ model: string }>(
          `/api/sessions/${id}`,
          { model: resolvedModel },
        );
        setSessionModel(updated.model);
        rememberSessionModel(userId, updated.model);
        if (announce) {
          addSystemMessage(
            `Model changed to ${describeSessionModel(updated.model, catalog.models)}`,
          );
        }
        return true;
      } catch {
        if (announce) {
          addSystemMessage("Failed to update model.");
        }
        return false;
      } finally {
        setUpdatingModel(false);
      }
    },
    [addSystemMessage, catalog, id, transport, userId],
  );

  const handleCommand = useCallback(
    async (name: string, args: string) => {
      if (!id) return;

      const availableModelKeys = catalog
        ? catalog.models.map((model) => `\`${model.ref}\``).join(", ")
        : "";

      switch (name) {
        case "help":
          addSystemMessage(
            "**Available commands:**\n\n" +
              "- `/help` -- List available commands\n" +
              "- `/model [name]` -- Show or change the AI model\n" +
              "- `/tree` -- Show workspace file tree\n" +
              "- `/export` -- Download chat as markdown\n" +
              "- `/clear` -- Clear chat display\n\n" +
              `Available model keys: ${availableModelKeys}`,
          );
          break;

        case "model":
          if (args.trim()) {
            await updateSessionModel(args, true);
          } else {
            addSystemMessage(
              `Current model: ${describeSessionModel(sessionModel, catalog?.models || [])}\n\n` +
                "Available models:\n\n" +
                availableSessionModelsMarkdown(catalog?.models || []),
            );
          }
          break;

        case "tree":
          try {
            if (!transport) {
              throw new Error("Runtime transport is unavailable");
            }
            const data = await transport.get<{ files: string[] }>(
              `/api/sessions/${id}/tree`,
            );
            if (data.files.length === 0) {
              addSystemMessage("Workspace is empty.");
            } else {
              const tree = data.files.map((f) => `- \`${f}\``).join("\n");
              addSystemMessage(
                `**Workspace files** (${data.files.length}):\n\n${tree}`,
              );
            }
          } catch {
            addSystemMessage("Failed to load file tree.");
          }
          break;

        case "export": {
          const md = messages
            .map((m) => {
              const label = m.role === "user" ? "**You**" : "**Assistant**";
              return `${label}:\n\n${m.content}\n`;
            })
            .join("\n---\n\n");
          const blob = new Blob([md], { type: "text/markdown" });
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = `session-${id?.slice(0, 8)}.md`;
          a.click();
          URL.revokeObjectURL(url);
          addSystemMessage("Chat exported as markdown.");
          break;
        }

        case "clear":
          setMessages([]);
          break;
      }
    },
    [
      addSystemMessage,
      catalog,
      id,
      messages,
      sessionModel,
      setMessages,
      transport,
      updateSessionModel,
    ],
  );

  const { catalogModels, connectedProviderIds, providerLabelById } =
    useMemo(() => {
      const connectedProviderIds = new Set<string>();
      const providerLabelById = new Map<string, string>();
      const models = [...(catalog?.models || [])];

      for (const provider of catalog?.providers || []) {
        providerLabelById.set(provider.id, provider.label);
        if (provider.connected) {
          connectedProviderIds.add(provider.id);
        }
      }

      models.sort((leftModel, rightModel) => {
        const leftConnectionRank = connectedProviderIds.has(leftModel.provider)
          ? 0
          : 1;
        const rightConnectionRank = connectedProviderIds.has(
          rightModel.provider,
        )
          ? 0
          : 1;
        if (leftConnectionRank !== rightConnectionRank) {
          return leftConnectionRank - rightConnectionRank;
        }

        const leftProviderLabel =
          providerLabelById.get(leftModel.provider) || leftModel.provider;
        const rightProviderLabel =
          providerLabelById.get(rightModel.provider) || rightModel.provider;
        const providerComparison =
          leftProviderLabel.localeCompare(rightProviderLabel);
        if (providerComparison !== 0) {
          return providerComparison;
        }
        return leftModel.label.localeCompare(rightModel.label);
      });

      return {
        catalogModels: models,
        connectedProviderIds,
        providerLabelById,
      };
    }, [catalog]);
  const selectedModelRef = pendingModelSelection ?? sessionModel;
  const selectedModelOption = getSessionModelOption(
    selectedModelRef,
    catalogModels,
  );
  const selectedModelValue = selectedModelOption?.ref || selectedModelRef;
  const selectedProviderLabel = selectedModelOption
    ? providerLabelById.get(selectedModelOption.provider) ||
      selectedModelOption.provider
    : null;
  const selectedModelRequiresConnection = selectedModelOption
    ? !connectedProviderIds.has(selectedModelOption.provider)
    : false;
  const availableThinkingLevels = getModelThinkingLevels(selectedModelOption);
  const canOverrideThinking = availableThinkingLevels.some(
    (level) => level !== "off",
  );
  const selectedThinkingOverride =
    thinkingOverride && availableThinkingLevels.includes(thinkingOverride)
      ? thinkingOverride
      : null;
  const promptThinkingOverride = canOverrideThinking
    ? (selectedThinkingOverride ?? undefined)
    : undefined;
  const legacyInputDisabledReason =
    piContextVersion < 1 && messages.length > 0
      ? LEGACY_PI_CONTEXT_MESSAGE
      : null;
  const inputDisabledReason = historyRevalidating
    ? "Refreshing session history before new prompts."
    : legacyInputDisabledReason;

  const handleModelChange = useCallback(
    (requestedModel: string) => {
      setPendingModelSelection(requestedModel);
      void updateSessionModel(requestedModel, false).then((updated) => {
        setPendingModelSelection((currentModel) =>
          currentModel === requestedModel ? null : currentModel,
        );
        if (!updated) {
          addSystemMessage("Failed to update model.");
        }
      });
    },
    [addSystemMessage, updateSessionModel],
  );

  const handleSend = useCallback(
    async (prompt: string) => {
      if (inputDisabledReason) return;
      if (id && historyCacheEligible && userId) {
        invalidateSessionHistoryCache(userId, id);
      }
      if (piContextVersion < 1) {
        setPiContextVersion(1);
      }
      // If the user starts a prompt while the model save is still in flight,
      // include the selected model in this prompt so the run does not fall back
      // to the previously persisted session model.
      await sendPrompt(
        prompt,
        pendingModelSelection ?? undefined,
        promptThinkingOverride,
      );
    },
    [
      historyCacheEligible,
      id,
      inputDisabledReason,
      pendingModelSelection,
      piContextVersion,
      promptThinkingOverride,
      sendPrompt,
      transport,
      userId,
    ],
  );

  const beginInspectorResize = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      event.currentTarget.setPointerCapture(event.pointerId);
      const startX = event.clientX;
      const startWidth = inspectorWidth;
      const onMove = (moveEvent: PointerEvent) => {
        const nextWidth = Math.min(
          INSPECTOR_WIDTH_MAX,
          Math.max(
            INSPECTOR_WIDTH_MIN,
            startWidth - (moveEvent.clientX - startX),
          ),
        );
        setInspectorWidth(nextWidth);
        sessionStorage.setItem("yinshi-inspector-width", String(nextWidth));
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [inspectorWidth],
  );

  return (
    <>
      {/* Header */}
      <header className="flex items-center gap-3 border-b border-gray-800 px-4 py-2 pl-14 md:pl-4">
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-gray-100 truncate">
            Session {id?.slice(0, 8)}
          </div>
        </div>
        {workspaceId && (
          <button
            type="button"
            onClick={() => setWorkspacePanelOpen(true)}
            className="rounded-lg border border-gray-800 px-3 py-1 text-xs text-gray-300 hover:border-gray-700 hover:bg-gray-900 lg:hidden"
          >
            Workspace
          </button>
        )}
        <div className="flex items-center gap-2">
          <label
            htmlFor="session-model"
            className="hidden text-xs text-gray-500 sm:block"
          >
            Model
          </label>
          <select
            id="session-model"
            value={selectedModelValue}
            disabled={streaming || updatingModel || loadingCatalog}
            onChange={(event) => {
              handleModelChange(event.target.value);
            }}
            className="rounded-lg border border-gray-800 bg-gray-900 px-2 py-1 text-xs text-gray-300 outline-none focus:border-blue-500 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {!selectedModelOption && (
              <option value={sessionModel}>{sessionModel}</option>
            )}
            {catalogModels.map((model) => (
              <option
                key={model.ref}
                value={model.ref}
                disabled={!connectedProviderIds.has(model.provider)}
              >
                {formatSessionModelOptionLabel(
                  model,
                  providerLabelById.get(model.provider),
                  connectedProviderIds.has(model.provider),
                )}
              </option>
            ))}
          </select>
          <label
            htmlFor="thinking-level"
            className="hidden text-xs text-gray-500 sm:block"
          >
            Thinking
          </label>
          <select
            id="thinking-level"
            value={selectedThinkingOverride ?? "default"}
            disabled={streaming || !canOverrideThinking}
            onChange={(event) => {
              const value = event.target.value;
              setThinkingOverride(
                value === "default" ? null : (value as ThinkingLevel),
              );
            }}
            className="rounded-lg border border-gray-800 bg-gray-900 px-2 py-1 text-xs text-gray-300 outline-none focus:border-blue-500 disabled:cursor-not-allowed disabled:opacity-40"
            title={
              canOverrideThinking
                ? "Select a thinking level for the next prompt"
                : "This model does not support thinking"
            }
          >
            <option value="default">Model default</option>
            {availableThinkingLevels.map((level) => (
              <option key={level} value={level}>
                {formatThinkingLevelLabel(level)}
              </option>
            ))}
          </select>
        </div>
        {streaming && (
          <div className="flex items-center gap-2">
            <div className="h-2 w-2 animate-pulse rounded-full bg-blue-500" />
            <span className="text-xs text-gray-500">Streaming</span>
          </div>
        )}
      </header>

      {/* Workspace */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        <main className="min-w-0 flex-1 overflow-hidden">
          {loadingHistory ? (
            <div className="flex h-full items-center justify-center">
              <div className="h-6 w-6 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
            </div>
          ) : (
            <div className="flex h-full flex-col">
              {selectedModelRequiresConnection && selectedProviderLabel && (
                <div className="mx-4 mt-4 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
                  Selected model requires a {selectedProviderLabel} connection.
                  Pick a connected provider from the model list or add{" "}
                  {selectedProviderLabel} in Settings.
                </div>
              )}
              {metadataError && (
                <div className="mx-4 mt-4 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                  {metadataError}
                </div>
              )}
              {historyError && (
                <div className="mx-4 mt-4 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                  {historyError}
                </div>
              )}
              <div className="flex-1 overflow-hidden">
                <ChatView
                  messages={messages}
                  streaming={streaming}
                  onSend={handleSend}
                  onCancel={cancel}
                  onCommand={handleCommand}
                  inputDisabledReason={inputDisabledReason}
                  piCommands={piCommands}
                />
              </div>
            </div>
          )}
        </main>
        {workspaceId && transport && isDesktopInspectorVisible && (
          <>
            <div
              role="separator"
              aria-label="Resize workspace panel"
              onPointerDown={beginInspectorResize}
              className="w-1.5 cursor-col-resize border-x border-gray-800 bg-gray-900 hover:bg-blue-500/40"
            />
            <WorkspaceInspector
              workspaceId={workspaceId}
              transport={transport}
              refreshKey={fileRefreshKey}
              className="flex"
              style={{ width: inspectorWidth }}
            />
          </>
        )}
      </div>
      {workspaceId && transport && workspacePanelOpen && (
        <div className="fixed inset-0 z-50 bg-gray-950 lg:hidden">
          <div className="flex h-full min-h-0 flex-col">
            <div className="flex items-center justify-between border-b border-gray-800 px-4 py-3">
              <div className="text-sm font-medium text-gray-100">Workspace</div>
              <button
                type="button"
                onClick={() => setWorkspacePanelOpen(false)}
                className="rounded-lg border border-gray-800 px-3 py-1 text-xs text-gray-300"
              >
                Close
              </button>
            </div>
            <WorkspaceInspector
              workspaceId={workspaceId}
              transport={transport}
              refreshKey={fileRefreshKey}
              className="flex-1"
            />
          </div>
        </div>
      )}
    </>
  );
}
