import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, type Message } from "../api/client";
import ChatView from "../components/ChatView";
import { useWebSocket, type ChatMessage } from "../hooks/useWebSocket";

export default function Session() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { messages, sendPrompt, cancel, connected, streaming, setMessages } =
    useWebSocket(id);
  const [loadingHistory, setLoadingHistory] = useState(true);

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
          timestamp: new Date(m.created_at).getTime(),
        }));
        setMessages(mapped);
      } catch {
        /* ignore */
      } finally {
        if (!cancelled) setLoadingHistory(false);
      }
    }

    loadHistory();
    return () => {
      cancelled = true;
    };
  }, [id, setMessages]);

  return (
    <div className="flex h-screen flex-col bg-gray-900">
      {/* Header */}
      <header className="flex items-center gap-3 border-b border-gray-800 bg-gray-900/95 px-3 py-2 backdrop-blur-sm">
        <button
          onClick={() => navigate("/")}
          className="flex h-10 w-10 items-center justify-center rounded-lg active:bg-gray-800"
          aria-label="Back"
        >
          <svg
            className="h-5 w-5 text-gray-400"
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth={2}
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M15.75 19.5 8.25 12l7.5-7.5"
            />
          </svg>
        </button>
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-white truncate">
            Session {id?.slice(0, 8)}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div
            className={`h-2 w-2 rounded-full ${
              connected ? "bg-green-500" : "bg-gray-600"
            }`}
          />
          <span className="text-xs text-gray-500">
            {connected ? "Connected" : "Connecting..."}
          </span>
        </div>
      </header>

      {/* Chat */}
      <div className="flex-1 overflow-hidden">
        {loadingHistory ? (
          <div className="flex h-full items-center justify-center">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
          </div>
        ) : (
          <ChatView
            messages={messages}
            streaming={streaming}
            onSend={sendPrompt}
            onCancel={cancel}
          />
        )}
      </div>
    </div>
  );
}
