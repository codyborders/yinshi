import { useState, type FormEvent } from "react";

import { usePiConfig } from "../hooks/usePiConfig";

const CATEGORY_LABELS: Record<string, string> = {
  skills: "Skills",
  extensions: "Extensions",
  prompts: "Prompts",
  agents: "Agents",
  themes: "Themes",
  settings: "Settings",
  models: "Models",
  sessions: "Sessions",
  instructions: "Instructions",
};

function getCategoryLabel(category: string): string {
  return CATEGORY_LABELS[category] ?? category;
}

function formatTimestamp(timestamp: string | null): string {
  if (!timestamp) {
    return "Never";
  }
  return new Date(timestamp).toLocaleString();
}

export default function PiConfigSection() {
  const {
    config,
    loading,
    error,
    importing,
    syncing,
    importFromGithub,
    importFromUpload,
    syncConfig,
    removeConfig,
    toggleCategory,
  } = usePiConfig();
  const [mode, setMode] = useState<"upload" | "github">("upload");
  const [repoUrl, setRepoUrl] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);

  async function handleGithubImport(event: FormEvent) {
    event.preventDefault();
    if (!repoUrl.trim()) {
      return;
    }
    const success = await importFromGithub(repoUrl);
    if (success) {
      setRepoUrl("");
    }
  }

  async function handleUpload(event: FormEvent) {
    event.preventDefault();
    if (!selectedFile) {
      return;
    }
    const success = await importFromUpload(selectedFile);
    if (success) {
      setSelectedFile(null);
    }
  }

  async function handleRetry() {
    if (!config || config.source_type !== "github") {
      return;
    }
    await syncConfig();
  }

  return (
    <section className="mt-8">
      <h2 className="mb-4 text-lg font-semibold text-gray-200">
        Pi Agent Configuration
      </h2>
      <p className="mb-4 text-sm text-gray-400">
        Bring your own Pi agent setup with imported skills, prompts,
        extensions, and settings.
      </p>

      {loading ? (
        <div className="rounded border border-gray-700 bg-gray-800 px-4 py-3 text-sm text-gray-400">
          Loading Pi config...
        </div>
      ) : null}

      {!loading && config?.status === "cloning" ? (
        <div className="rounded border border-gray-700 bg-gray-800 px-4 py-4">
          <p className="text-sm text-gray-300">
            Importing from: {config.source_label}
          </p>
          <p className="mt-2 text-sm text-gray-400">Cloning repository...</p>
        </div>
      ) : null}

      {!loading && config?.status === "error" ? (
        <div className="rounded border border-red-900/50 bg-gray-800 px-4 py-4">
          <p className="text-sm text-red-400">
            Import failed: {config.error_message || "Unknown error"}
          </p>
          <div className="mt-3 flex gap-3">
            {config.source_type === "github" ? (
              <button
                type="button"
                onClick={handleRetry}
                disabled={syncing}
                className="rounded border border-gray-600 px-3 py-2 text-sm text-gray-200 disabled:opacity-50"
              >
                {syncing ? "Retrying..." : "Retry"}
              </button>
            ) : null}
            <button
              type="button"
              onClick={() => {
                void removeConfig();
              }}
              className="text-sm text-red-400 hover:text-red-300"
            >
              Remove
            </button>
          </div>
        </div>
      ) : null}

      {!loading && config && config.status === "ready" ? (
        <div className="rounded border border-gray-700 bg-gray-800 px-4 py-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-sm text-gray-300">
                Imported from: {config.source_label}
              </p>
              {config.source_type === "github" ? (
                <p className="mt-1 text-xs text-gray-500">
                  Last synced: {formatTimestamp(config.last_synced_at)}
                </p>
              ) : null}
            </div>
            <div className="flex gap-3">
              {config.source_type === "github" ? (
                <button
                  type="button"
                  onClick={() => {
                    void syncConfig();
                  }}
                  disabled={syncing}
                  className="rounded border border-gray-600 px-3 py-2 text-sm text-gray-200 disabled:opacity-50"
                >
                  {syncing ? "Syncing..." : "Sync"}
                </button>
              ) : null}
              <button
                type="button"
                onClick={() => {
                  void removeConfig();
                }}
                className="text-sm text-red-400 hover:text-red-300"
              >
                Remove
              </button>
            </div>
          </div>

          <div className="mt-4 grid gap-3 md:grid-cols-2">
            {config.available_categories.map((category) => (
              <label
                key={category}
                className="flex items-center justify-between rounded border border-gray-700 px-3 py-2"
              >
                <span className="text-sm text-gray-200">
                  {getCategoryLabel(category)}
                </span>
                <input
                  type="checkbox"
                  checked={config.enabled_categories.includes(category)}
                  onChange={(event) => {
                    void toggleCategory(category, event.target.checked);
                  }}
                />
              </label>
            ))}
          </div>
        </div>
      ) : null}

      {!loading && !config ? (
        <div className="rounded border border-gray-700 bg-gray-800 px-4 py-4">
          <div className="mb-4 flex gap-2">
            <button
              type="button"
              onClick={() => setMode("upload")}
              className={`rounded px-3 py-2 text-sm ${
                mode === "upload"
                  ? "bg-gray-200 text-gray-900"
                  : "border border-gray-700 text-gray-300"
              }`}
            >
              Upload
            </button>
            <button
              type="button"
              onClick={() => setMode("github")}
              className={`rounded px-3 py-2 text-sm ${
                mode === "github"
                  ? "bg-gray-200 text-gray-900"
                  : "border border-gray-700 text-gray-300"
              }`}
            >
              GitHub
            </button>
          </div>

          {mode === "upload" ? (
            <form onSubmit={handleUpload} className="space-y-3">
              <input
                type="file"
                accept=".zip"
                onChange={(event) => {
                  setSelectedFile(event.target.files?.[0] ?? null);
                }}
                className="block w-full text-sm text-gray-300 file:mr-4 file:rounded file:border-0 file:bg-gray-200 file:px-3 file:py-2 file:text-sm file:text-gray-900"
              />
              <p className="text-xs text-gray-500">
                Upload a zip of your `.pi` directory or a partial archive rooted
                like `.pi`.
              </p>
              <button
                type="submit"
                disabled={importing || !selectedFile}
                className="btn-primary px-4 py-2 text-sm disabled:opacity-50"
              >
                {importing ? "Uploading..." : "Upload Pi Config"}
              </button>
            </form>
          ) : (
            <form onSubmit={handleGithubImport} className="space-y-3">
              <div className="flex gap-3">
                <input
                  type="text"
                  value={repoUrl}
                  onChange={(event) => setRepoUrl(event.target.value)}
                  placeholder="owner/repo or https://github.com/owner/repo"
                  className="flex-1 rounded border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-gray-200 placeholder-gray-500"
                />
                <button
                  type="submit"
                  disabled={importing || !repoUrl.trim()}
                  className="btn-primary px-4 py-2 text-sm disabled:opacity-50"
                >
                  {importing ? "Importing..." : "Import"}
                </button>
              </div>
              <p className="text-xs text-gray-500">
                Import a Pi config from a GitHub repository.
              </p>
            </form>
          )}
        </div>
      ) : null}

      {error ? <p className="mt-3 text-sm text-red-400">{error}</p> : null}
    </section>
  );
}
