import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { api, type Message } from "../api/client";
import ChatView from "../components/ChatView";
import { useAgentStream, type ChatMessage } from "../hooks/useAgentStream";
import { useCatalog } from "../hooks/useCatalog";
import { usePiCommands } from "../hooks/usePiCommands";
import {
  DEFAULT_SESSION_MODEL,
  availableSessionModelsMarkdown,
  describeSessionModel,
  formatSessionModelOptionLabel,
  getSessionModelOption,
  resolveSessionModelKey,
} from "../models/sessionModels";

let cmdIdCounter = 0;
function nextCmdId(): string {
  return `cmd-${Date.now()}-${++cmdIdCounter}`;
}

export default function Session() {
  const { id } = useParams<{ id: string }>();
  const { messages, sendPrompt, cancel, streaming, setMessages } =
    useAgentStream(id);
  const { catalog, loading: loadingCatalog } = useCatalog();
  const piCommands = usePiCommands();
  const [sessionModel, setSessionModel] = useState(DEFAULT_SESSION_MODEL);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [updatingModel, setUpdatingModel] = useState(false);
  const [selectedModelOverride, setSelectedModelOverride] = useState<
    string | null
  >(null);
  const [thinkingEnabled, setThinkingEnabled] = useState(true);
  const [hasThinkingOverride, setHasThinkingOverride] = useState(false);

  // Load existing message history
  useEffect(() => {
    if (!id) return;
    let cancelled = false;

    async function loadHistory() {
      try {
        const history = await api.get<Message[]>(
          `/api/sessions/${id}/messages`,
        );
        if (cancelled) return;
        const mapped: ChatMessage[] = history.map((m) => ({
          id: m.id,
          role: m.role as ChatMessage["role"],
          content: m.content || "",
          blocks: [],
          timestamp: new Date(m.created_at).getTime(),
        }));
        setMessages(mapped);
        setHistoryError(null);
      } catch (error) {
        console.error(`Failed to load session history for ${id}`, error);
        if (!cancelled) {
          setHistoryError("Failed to load session history.");
        }
      } finally {
        if (!cancelled) setLoadingHistory(false);
      }
    }

    loadHistory();
    return () => {
      cancelled = true;
    };
  }, [id, setMessages]);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;

    async function loadSession() {
      try {
        const session = await api.get<{ model: string }>(`/api/sessions/${id}`);
        if (cancelled) return;
        setSessionModel(session.model);
      } catch (error) {
        console.error(`Failed to load session metadata for ${id}`, error);
      }
    }

    loadSession();
    return () => {
      cancelled = true;
    };
  }, [id]);

  useEffect(() => {
    setSelectedModelOverride(null);
    setThinkingEnabled(true);
    setHasThinkingOverride(false);
  }, [id]);

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
      if (!id) return false;
      if (!catalog) return false;

      const connectedProviderIds = new Set(
        catalog.providers
          .filter((provider) => provider.connected)
          .map((provider) => provider.id),
      );
      const providerLabelById = new Map(
        catalog.providers.map((provider) => [provider.id, provider.label] as const),
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
      const resolvedModelOption = getSessionModelOption(resolvedModel, catalog.models);
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
        const updated = await api.patch<{ model: string }>(
          `/api/sessions/${id}`,
          { model: resolvedModel },
        );
        setSessionModel(updated.model);
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
    [addSystemMessage, catalog, id],
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
            const data = await api.get<{ files: string[] }>(
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
      updateSessionModel,
    ],
  );

  const {
    catalogModels,
    connectedProviderIds,
    providerLabelById,
  } = useMemo(() => {
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
      const rightConnectionRank = connectedProviderIds.has(rightModel.provider)
        ? 0
        : 1;
      if (leftConnectionRank !== rightConnectionRank) {
        return leftConnectionRank - rightConnectionRank;
      }

      const leftProviderLabel =
        providerLabelById.get(leftModel.provider) || leftModel.provider;
      const rightProviderLabel =
        providerLabelById.get(rightModel.provider) || rightModel.provider;
      const providerComparison = leftProviderLabel.localeCompare(rightProviderLabel);
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
  const selectedModelRef = selectedModelOverride ?? sessionModel;
  const selectedModelOption = getSessionModelOption(
    selectedModelRef,
    catalogModels,
  );
  const selectedModelValue = selectedModelOption?.ref || selectedModelRef;
  const selectedProviderLabel = selectedModelOption
    ? providerLabelById.get(selectedModelOption.provider) || selectedModelOption.provider
    : null;
  const selectedModelRequiresConnection = selectedModelOption
    ? !connectedProviderIds.has(selectedModelOption.provider)
    : false;
  const canOverrideThinking = selectedModelOption?.reasoning === true;
  const thinkingOverride = canOverrideThinking && hasThinkingOverride
    ? thinkingEnabled
    : undefined;

  const handleModelChange = useCallback(
    (requestedModel: string) => {
      setSelectedModelOverride(requestedModel);
      void updateSessionModel(requestedModel, false).then((updated) => {
        setSelectedModelOverride((currentModel) =>
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
      // If the user starts a prompt while the model save is still in flight,
      // include the selected model in this prompt so the run does not fall back
      // to the previously persisted session model.
      await sendPrompt(
        prompt,
        selectedModelOverride ?? undefined,
        thinkingOverride,
      );
    },
    [selectedModelOverride, sendPrompt, thinkingOverride],
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
          {/* Thinking toggle */}
          <div className="flex items-center gap-1">
            <button
              type="button"
              disabled={streaming || !canOverrideThinking}
              onClick={() => {
                setHasThinkingOverride(true);
                setThinkingEnabled(
                  (currentThinkingEnabled) => !currentThinkingEnabled,
                );
              }}
              className={`flex items-center gap-1 rounded px-2 py-1 text-xs transition-colors ${
                streaming || !canOverrideThinking
                  ? "cursor-not-allowed opacity-40"
                  : thinkingEnabled
                    ? "bg-purple-900/50 text-purple-300 hover:bg-purple-900/70"
                    : "bg-gray-800 text-gray-500 hover:bg-gray-700"
              }`}
              title={
                !canOverrideThinking
                  ? "This model does not support thinking"
                  : !hasThinkingOverride
                    ? "Using the model default thinking setting - click to set an explicit override"
                  : thinkingEnabled
                    ? "Thinking enabled - click to disable"
                    : "Thinking disabled - click to enable"
              }
            >
              <svg
                className="h-3 w-3"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"
                />
              </svg>
              <span>Thinking</span>
            </button>
          </div>
        </div>
        {streaming && (
          <div className="flex items-center gap-2">
            <div className="h-2 w-2 animate-pulse rounded-full bg-blue-500" />
            <span className="text-xs text-gray-500">Streaming</span>
          </div>
        )}
      </header>

      {/* Chat */}
      <div className="flex-1 overflow-hidden">
        {loadingHistory ? (
          <div className="flex h-full items-center justify-center">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
          </div>
        ) : (
          <div className="flex h-full flex-col">
            {selectedModelRequiresConnection && selectedProviderLabel && (
              <div className="mx-4 mt-4 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
                Selected model requires a {selectedProviderLabel} connection. Pick a connected provider from the model list or add {selectedProviderLabel} in Settings.
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
                piCommands={piCommands}
              />
            </div>
          </div>
        )}
      </div>
    </>
  );
}
