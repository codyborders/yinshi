import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, type Repo, type SessionInfo, type Workspace } from "../api/client";
import { useAuth } from "../hooks/useAuth";
import { deriveRepoName, isGitUrl, isLocalPath } from "../utils/repo";

const COLORS = [
  "bg-violet-600",
  "bg-blue-600",
  "bg-emerald-600",
  "bg-amber-600",
  "bg-rose-600",
  "bg-cyan-600",
  "bg-pink-600",
  "bg-teal-600",
];

function repoColor(name: string): string {
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
  return COLORS[Math.abs(hash) % COLORS.length];
}

function statusDotClass(hasRunning: boolean, workspaceState: string): string {
  if (hasRunning) return "bg-blue-400 animate-pulse";
  if (workspaceState === "ready") return "bg-green-500";
  return "bg-yellow-500";
}

const PlusIcon = (
  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
    <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
  </svg>
);

export default function Sidebar() {
  const { id: activeSessionId } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { status, email, logout } = useAuth();
  const [repos, setRepos] = useState<Repo[]>([]);
  const [loading, setLoading] = useState(true);
  const [showImport, setShowImport] = useState(false);

  useEffect(() => {
    api
      .get<Repo[]>("/api/repos")
      .then(setRepos)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  function handleImported(repo: Repo | null) {
    if (repo) setRepos((prev) => [repo, ...prev]);
    setShowImport(false);
  }

  return (
    <aside className="flex h-full w-72 flex-col border-r border-gray-800 bg-gray-900">
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
        <span className="text-xs font-semibold uppercase tracking-wider text-gray-500">
          Workspaces
        </span>
        <button
          onClick={() => setShowImport(true)}
          className="text-gray-500 hover:text-gray-300"
          title="Add repository"
        >
          {PlusIcon}
        </button>
      </div>

      {showImport && <ImportForm onDone={handleImported} />}

      <div className="flex-1 overflow-y-auto scrollbar-hide py-1">
        {loading && (
          <div className="flex justify-center py-8">
            <div className="h-5 w-5 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
          </div>
        )}

        {!loading && repos.length === 0 && !showImport && (
          <div className="px-4 py-8 text-center text-sm text-gray-600">
            No repositories yet.
          </div>
        )}

        {repos.map((repo) => (
          <RepoSection
            key={repo.id}
            repo={repo}
            activeSessionId={activeSessionId}
          />
        ))}
      </div>

      <button
        onClick={() => setShowImport(true)}
        className="flex items-center gap-2 border-t border-gray-800 px-4 py-3 text-sm text-gray-500 hover:text-gray-300"
      >
        {PlusIcon}
        Add repository
      </button>

      {status === "authenticated" && email && (
        <div className="flex items-center justify-between border-t border-gray-800 px-4 py-3">
          <span className="truncate text-xs text-gray-500" title={email}>
            {email}
          </span>
          <button
            onClick={logout}
            className="shrink-0 text-xs text-gray-600 hover:text-gray-400"
          >
            Logout
          </button>
        </div>
      )}
    </aside>
  );
}

function RepoSection({
  repo,
  activeSessionId,
}: {
  repo: Repo;
  activeSessionId: string | undefined;
}) {
  const navigate = useNavigate();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [expanded, setExpanded] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    if (expanded && !loaded) {
      api
        .get<Workspace[]>(`/api/repos/${repo.id}/workspaces`)
        .then((data) => {
          setWorkspaces(data);
          setLoaded(true);
        })
        .catch(() => {});
    }
  }, [expanded, loaded, repo.id]);

  async function createBranch(e: React.MouseEvent) {
    e.stopPropagation();
    setCreating(true);
    try {
      const ws = await api.post<Workspace>(
        `/api/repos/${repo.id}/workspaces`,
        {},
      );
      setWorkspaces((prev) => [ws, ...prev]);
      setExpanded(true);
      // Auto-create a session and navigate to it
      const session = await api.post<SessionInfo>(
        `/api/workspaces/${ws.id}/sessions`,
        { model: "minimax" },
      );
      navigate(`/session/${session.id}`);
    } catch {
      /* ignore */
    } finally {
      setCreating(false);
    }
  }

  const initial = repo.name.charAt(0).toUpperCase();

  return (
    <div className="py-0.5">
      <div className="flex items-center hover:bg-gray-800/50 group">
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex flex-1 items-center gap-2.5 px-4 py-2 min-w-0"
        >
          <span
            className={`flex h-6 w-6 shrink-0 items-center justify-center rounded text-xs font-bold text-white ${repoColor(repo.name)}`}
          >
            {initial}
          </span>
          <span className="flex-1 truncate text-left text-sm font-medium text-gray-200">
            {repo.name}
          </span>
        </button>
        <button
          onClick={createBranch}
          disabled={creating}
          className="shrink-0 px-3 py-2 text-gray-600 opacity-0 group-hover:opacity-100 hover:text-gray-300 transition-opacity"
          title="New branch"
        >
          {creating ? (
            <div className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-500 border-t-transparent" />
          ) : (
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
            </svg>
          )}
        </button>
      </div>

      {expanded &&
        workspaces.map((ws) => (
          <WorkspaceItem
            key={ws.id}
            workspace={ws}
            activeSessionId={activeSessionId}
          />
        ))}
    </div>
  );
}

function WorkspaceItem({
  workspace,
  activeSessionId,
}: {
  workspace: Workspace;
  activeSessionId: string | undefined;
}) {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [loadedSessions, setLoadedSessions] = useState(false);

  useEffect(() => {
    if (!loadedSessions) {
      api
        .get<SessionInfo[]>(`/api/workspaces/${workspace.id}/sessions`)
        .then((data) => {
          setSessions(data);
          setLoadedSessions(true);
        })
        .catch(() => {});
    }
  }, [workspace.id, loadedSessions]);

  async function openOrCreateSession() {
    if (sessions.length > 0) {
      navigate(`/session/${sessions[0].id}`);
      return;
    }

    try {
      const session = await api.post<SessionInfo>(
        `/api/workspaces/${workspace.id}/sessions`,
        { model: "minimax" },
      );
      setSessions([session]);
      navigate(`/session/${session.id}`);
    } catch {
      /* ignore */
    }
  }

  const isActive = sessions.some((s) => s.id === activeSessionId);
  const hasRunning = sessions.some((s) => s.status === "running");

  return (
    <button
      onClick={openOrCreateSession}
      className={`flex w-full items-center gap-2 py-1.5 pl-11 pr-4 text-left hover:bg-gray-800/50 ${
        isActive ? "bg-gray-800/70" : ""
      }`}
    >
      <span
        className={`h-2 w-2 shrink-0 rounded-full ${statusDotClass(hasRunning, workspace.state)}`}
      />
      <div className="flex-1 min-w-0">
        <div className="truncate text-sm text-gray-300">
          {workspace.name}
        </div>
      </div>
    </button>
  );
}

function ImportForm({ onDone }: { onDone: (repo: Repo | null) => void }) {
  const [input, setInput] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const value = input.trim();
    if (!value) return;
    setSubmitting(true);
    setError(null);
    try {
      const body: { name: string; remote_url?: string; local_path?: string } = {
        name: deriveRepoName(value),
      };
      if (isGitUrl(value)) {
        body.remote_url = value;
      } else if (isLocalPath(value)) {
        body.local_path = value;
      } else {
        body.remote_url = `https://github.com/${value}`;
        body.name = deriveRepoName(value);
      }
      const repo = await api.post<Repo>("/api/repos", body);
      onDone(repo);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Import failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="border-b border-gray-800 px-4 py-3 space-y-2">
      <input
        type="text"
        placeholder="GitHub URL, user/repo, or local path"
        value={input}
        onChange={(e) => setInput(e.target.value)}
        className="w-full rounded-md bg-gray-800 px-3 py-2 text-sm text-white placeholder-gray-500 outline-none focus:ring-1 focus:ring-blue-500"
        autoFocus
      />
      {error && <p className="text-xs text-red-400">{error}</p>}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={submitting || !input.trim()}
          className="flex-1 rounded-md bg-blue-500 px-2 py-1.5 text-xs font-medium text-white disabled:opacity-40"
        >
          {submitting ? "Importing..." : "Import"}
        </button>
        <button
          type="button"
          onClick={() => onDone(null)}
          className="flex-1 rounded-md bg-gray-800 px-2 py-1.5 text-xs text-gray-400"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
