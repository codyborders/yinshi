import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type SessionInfo, type Workspace } from "../api/client";

export default function WorkspaceView({ repoId }: { repoId: string }) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    loadWorkspaces();
  }, [repoId]);

  async function loadWorkspaces() {
    try {
      const data = await api.get<Workspace[]>(
        `/api/repos/${repoId}/workspaces`,
      );
      setWorkspaces(data);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }

  async function createWorkspace() {
    setCreating(true);
    try {
      const ws = await api.post<Workspace>(
        `/api/repos/${repoId}/workspaces`,
        {},
      );
      setWorkspaces((prev) => [ws, ...prev]);
    } catch {
      /* ignore */
    } finally {
      setCreating(false);
    }
  }

  if (loading) {
    return (
      <div className="flex justify-center py-4">
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wider text-gray-500">
          Workspaces
        </span>
        <button
          onClick={createWorkspace}
          disabled={creating}
          className="rounded-md bg-gray-700 px-2 py-1 text-xs text-gray-300 active:bg-gray-600"
        >
          {creating ? "Creating..." : "+ New"}
        </button>
      </div>

      {workspaces.length === 0 && (
        <p className="text-sm text-gray-600">No workspaces yet.</p>
      )}

      {workspaces.map((ws) => (
        <WorkspaceCard key={ws.id} workspace={ws} navigate={navigate} />
      ))}
    </div>
  );
}

function WorkspaceCard({
  workspace,
  navigate,
}: {
  workspace: Workspace;
  navigate: ReturnType<typeof useNavigate>;
}) {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [creatingSession, setCreatingSession] = useState(false);

  const loadSessions = useCallback(async () => {
    try {
      const data = await api.get<SessionInfo[]>(
        `/api/workspaces/${workspace.id}/sessions`,
      );
      setSessions(data);
    } catch {
      /* ignore */
    }
  }, [workspace.id]);

  useEffect(() => {
    if (expanded) loadSessions();
  }, [expanded, loadSessions]);

  async function createSession() {
    setCreatingSession(true);
    try {
      const session = await api.post<SessionInfo>(
        `/api/workspaces/${workspace.id}/sessions`,
        { model: "minimax" },
      );
      navigate(`/app/session/${session.id}`);
    } catch {
      /* ignore */
    } finally {
      setCreatingSession(false);
    }
  }

  return (
    <div className="rounded-lg bg-gray-700/50 p-3">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center justify-between min-h-touch"
      >
        <div className="text-left">
          <div className="text-sm font-medium text-gray-200">
            {workspace.name}
          </div>
          <div className="text-xs text-gray-500">{workspace.branch}</div>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`inline-block h-2 w-2 rounded-full ${
              workspace.state === "ready" ? "bg-green-500" : "bg-yellow-500"
            }`}
          />
          <svg
            className={`h-4 w-4 text-gray-500 transition-transform ${
              expanded ? "rotate-180" : ""
            }`}
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth={2}
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M19.5 8.25l-7.5 7.5-7.5-7.5"
            />
          </svg>
        </div>
      </button>

      {expanded && (
        <div className="mt-2 space-y-2 border-t border-gray-600 pt-2">
          <button
            onClick={createSession}
            disabled={creatingSession}
            className="btn-primary w-full text-sm py-2"
          >
            {creatingSession ? "Starting..." : "New Session"}
          </button>

          {sessions.map((s) => (
            <button
              key={s.id}
              onClick={() => navigate(`/app/session/${s.id}`)}
              className="flex w-full items-center justify-between rounded-md bg-gray-700 p-2 min-h-touch active:bg-gray-600"
            >
              <span className="text-sm text-gray-300 truncate">
                {s.id.slice(0, 8)}...
              </span>
              <span
                className={`text-xs ${
                  s.status === "running" ? "text-blue-400" : "text-gray-500"
                }`}
              >
                {s.status}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
