import { useEffect, useState } from "react";
import { api, type Repo, type Workspace } from "../api/client";
import { deriveRepoName, isGitUrl, isLocalPath } from "../utils/repo";
import WorkspaceView from "./WorkspaceView";

export default function RepoList() {
  const [repos, setRepos] = useState<Repo[]>([]);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showImport, setShowImport] = useState(false);

  useEffect(() => {
    loadRepos();
  }, []);

  async function loadRepos() {
    try {
      const data = await api.get<Repo[]>("/api/repos");
      setRepos(data);
    } catch {
      /* silently retry later */
    } finally {
      setLoading(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400">
          Repositories
        </h2>
        <button
          onClick={() => setShowImport(true)}
          className="btn-primary text-sm px-3 py-2"
        >
          + Import
        </button>
      </div>

      {showImport && (
        <ImportForm
          onDone={(repo) => {
            if (repo) setRepos((prev) => [repo, ...prev]);
            setShowImport(false);
          }}
        />
      )}

      {repos.length === 0 && !showImport && (
        <div className="card text-center text-gray-500 py-8">
          No repositories yet. Tap Import to get started.
        </div>
      )}

      {repos.map((repo) => (
        <RepoCard
          key={repo.id}
          repo={repo}
          expanded={expandedId === repo.id}
          onToggle={() =>
            setExpandedId(expandedId === repo.id ? null : repo.id)
          }
        />
      ))}
    </div>
  );
}

function RepoCard({
  repo,
  expanded,
  onToggle,
}: {
  repo: Repo;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="card">
      <button
        onClick={onToggle}
        className="flex w-full items-center justify-between min-h-touch"
      >
        <div className="text-left">
          <div className="font-medium text-gray-100">{repo.name}</div>
          <div className="text-xs text-gray-500 truncate max-w-[250px]">
            {repo.remote_url || repo.root_path}
          </div>
        </div>
        <svg
          className={`h-5 w-5 text-gray-500 transition-transform ${
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
      </button>

      {expanded && (
        <div className="mt-3 border-t border-gray-700 pt-3">
          <WorkspaceView repoId={repo.id} />
        </div>
      )}
    </div>
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
        // Assume it's a GitHub shorthand like "user/repo"
        body.remote_url = `https://github.com/${value}`;
        body.name = deriveRepoName(value);
      }
      const repo = await api.post<Repo>("/api/repos", body);
      onDone(repo);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Import failed");
    } finally {
      setSubmitting(false);
    }
  }

  const value = input.trim();
  const hint = !value
    ? "GitHub URL, user/repo, or local path"
    : isGitUrl(value)
    ? `Clone from URL as "${deriveRepoName(value)}"`
    : isLocalPath(value)
    ? `Import local repo "${deriveRepoName(value)}"`
    : `Clone github.com/${value}`;

  return (
    <form onSubmit={handleSubmit} className="card space-y-3">
      <input
        type="text"
        placeholder="GitHub URL, user/repo, or local path"
        value={input}
        onChange={(e) => setInput(e.target.value)}
        className="w-full rounded-lg bg-gray-700 px-3 py-3 text-gray-100 placeholder-gray-500 outline-none focus:ring-2 focus:ring-blue-500"
        autoFocus
      />
      {value && (
        <p className="text-xs text-gray-400">{hint}</p>
      )}
      {error && <p className="text-sm text-red-400">{error}</p>}
      <div className="flex gap-2">
        <button type="submit" disabled={submitting || !value} className="btn-primary flex-1">
          {submitting ? "Importing..." : "Import"}
        </button>
        <button
          type="button"
          onClick={() => onDone(null)}
          className="btn-secondary flex-1"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
