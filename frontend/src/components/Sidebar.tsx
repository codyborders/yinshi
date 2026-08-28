import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import {
  ApiError,
  api,
  type CloudRunner,
  type GitHubInstallation,
  type Repo,
  type SessionInfo,
  type Workspace,
} from "../api/client";
import { useAuth } from "../hooks/useAuth";
import { useTheme } from "../hooks/useTheme";
import { preferredSessionModel } from "../models/sessionModelPreference";
import { listRunnerRepositories } from "../runner/repositories";
import { resolveRuntimeRef } from "../runtime/resolveRuntime";
import {
  runtimeResourceId,
  type UnresolvedRuntimeRef,
} from "../runtime/runtimeRef";
import {
  createRuntimeTransport,
  type RuntimeRef,
  type RuntimeTransport,
} from "../runtime/runtimeTransport";
import {
  deriveRepoName,
  isGithubShorthand,
  isGitUrl,
  isLocalPath,
} from "../utils/repo";

interface LocatedRepo {
  readonly repository: Repo;
  readonly runtime: RuntimeRef;
}

const COLORS = [
  "bg-[#c23b22]",
  "bg-[#8c6d3f]",
  "bg-[#5a7247]",
  "bg-[#7a5230]",
  "bg-[#985a4a]",
  "bg-[#4a6b5a]",
  "bg-[#6b5040]",
  "bg-[#8a6848]",
];

function runtimeIdentity(runtime: RuntimeRef): string {
  if (runtime.location === "byoc") {
    return `byoc:${runtime.runnerId}`;
  }
  return runtime.location;
}

function runtimeLabel(runtime: RuntimeRef): string {
  if (runtime.location === "byoc") return "BYOC";
  if (runtime.location === "local") return "Local";
  return "Hosted";
}

function repoColor(name: string): string {
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
  return COLORS[Math.abs(hash) % COLORS.length];
}

function statusDotClass(hasRunning: boolean, workspaceState: string): string {
  if (hasRunning) return "bg-[#d4543d] animate-pulse";
  if (workspaceState === "ready") return "bg-[#5a7247]";
  return "bg-[#b8963e]";
}

const PlusIcon = (
  <svg
    className="h-4 w-4"
    fill="none"
    viewBox="0 0 24 24"
    strokeWidth={2}
    stroke="currentColor"
  >
    <path
      strokeLinecap="round"
      strokeLinejoin="round"
      d="M12 4.5v15m7.5-7.5h-15"
    />
  </svg>
);

type SidebarNotice = {
  message: string;
  tone: "error" | "success";
};

type ImportAction = {
  label: string;
  href: string;
  external: boolean;
};

function githubNoticeFromSearch(search: string): SidebarNotice | null {
  const params = new URLSearchParams(search);
  if (params.get("github_connected") === "1") {
    return { message: "GitHub connected.", tone: "success" };
  }

  const errorCode = params.get("github_connect_error");
  if (errorCode === "not_granted") {
    return {
      message: "GitHub access was not granted for that installation.",
      tone: "error",
    };
  }
  if (errorCode === "invalid_state") {
    return {
      message: "GitHub connect session expired. Try again.",
      tone: "error",
    };
  }
  if (errorCode === "missing_installation") {
    return {
      message: "GitHub did not return an installation for this request.",
      tone: "error",
    };
  }
  if (errorCode === "install_failed") {
    return {
      message: "GitHub installation could not be completed.",
      tone: "error",
    };
  }
  return null;
}

function ThemeIcon({ theme }: { theme: string }) {
  if (theme === "light") {
    return (
      <svg
        className="h-4 w-4"
        fill="none"
        viewBox="0 0 24 24"
        strokeWidth={1.5}
        stroke="currentColor"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M21.752 15.002A9.72 9.72 0 0 1 18 15.75c-5.385 0-9.75-4.365-9.75-9.75 0-1.33.266-2.597.748-3.752A9.753 9.753 0 0 0 3 11.25C3 16.635 7.365 21 12.75 21a9.753 9.753 0 0 0 9.002-5.998Z"
        />
      </svg>
    );
  }
  return (
    <svg
      className="h-4 w-4"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 3v2.25m6.364.386-1.591 1.591M21 12h-2.25m-.386 6.364-1.591-1.591M12 18.75V21m-4.773-4.227-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0Z"
      />
    </svg>
  );
}

export default function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { id: activeSessionId } = useParams<{ id: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const { status, email, userId, logout } = useAuth();
  const { theme, toggle: toggleTheme } = useTheme();
  const requestedPrimaryRuntime = useMemo<UnresolvedRuntimeRef>(
    () =>
      window.yinshiDesktop ? { location: "local" } : { location: "hosted" },
    [],
  );
  const [primaryRuntime, setPrimaryRuntime] = useState<RuntimeRef | null>(null);
  const [repos, setRepos] = useState<LocatedRepo[]>([]);
  const [availableRuntimes, setAvailableRuntimes] = useState<RuntimeRef[]>([]);
  const [githubInstallations, setGithubInstallations] = useState<
    GitHubInstallation[]
  >([]);
  const [githubNotice, setGithubNotice] = useState<SidebarNotice | null>(null);
  const [loading, setLoading] = useState(true);
  const [repoLoadError, setRepoLoadError] = useState<string | null>(null);
  const [locationErrors, setLocationErrors] = useState<string[]>([]);
  const [showImport, setShowImport] = useState(false);

  async function loadRepos() {
    setLoading(true);
    setRepoLoadError(null);
    setLocationErrors([]);
    try {
      const resolvedPrimary = await resolveRuntimeRef(requestedPrimaryRuntime);
      const primaryTransport = createRuntimeTransport(resolvedPrimary);
      const data = await primaryTransport
        .get<Repo[]>("/api/repos")
        .finally(() => {
          primaryTransport.close();
        });
      setPrimaryRuntime(resolvedPrimary);
      const locatedRepositories: LocatedRepo[] = data.map((repository) => ({
        repository,
        runtime: resolvedPrimary,
      }));
      const unavailableLocations: string[] = [];
      const discoveredRuntimes: RuntimeRef[] = [resolvedPrimary];
      if (window.yinshiDesktop !== undefined) {
        try {
          const hostedTransport = createRuntimeTransport({
            location: "hosted",
          });
          const hostedRepositories = await hostedTransport
            .get<Repo[]>("/api/repos")
            .finally(() => {
              hostedTransport.close();
            });
          discoveredRuntimes.push({ location: "hosted" });
          locatedRepositories.push(
            ...hostedRepositories.map((repository) => ({
              repository,
              runtime: { location: "hosted" as const },
            })),
          );
        } catch {
          unavailableLocations.push("Hosted repositories are unavailable.");
        }
      }
      try {
        const runner = await api.get<CloudRunner | null>(
          "/api/settings/runner",
        );
        if (
          runner?.noise_key_confirmed &&
          runner.noise_public_key &&
          runner.status !== "revoked"
        ) {
          const runnerTarget = {
            runnerId: runner.id,
            runnerPublicKey: runner.noise_public_key,
          };
          discoveredRuntimes.push({
            location: "byoc",
            runnerId: runner.id,
            runnerPublicKey: runner.noise_public_key,
          });
          const runnerRepositories = await listRunnerRepositories(runnerTarget);
          locatedRepositories.push(
            ...runnerRepositories.map((repository) => ({
              repository,
              runtime: {
                location: "byoc" as const,
                runnerId: runner.id,
                runnerPublicKey: runner.noise_public_key as string,
              },
            })),
          );
        }
      } catch {
        unavailableLocations.push("BYOC repositories are unavailable.");
      }
      setLocationErrors(unavailableLocations);
      setAvailableRuntimes(discoveredRuntimes);
      setRepos(locatedRepositories);
    } catch {
      setRepoLoadError("Failed to load repositories.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadRepos();
  }, []);

  async function loadGitHubInstallations() {
    if (status !== "authenticated") {
      setGithubInstallations([]);
      return;
    }

    try {
      let data: GitHubInstallation[];
      if (window.yinshiDesktop) {
        const hostedTransport = createRuntimeTransport({ location: "hosted" });
        data = await hostedTransport
          .get<GitHubInstallation[]>("/api/github/installations")
          .finally(() => {
            hostedTransport.close();
          });
      } else {
        data = await api.get<GitHubInstallation[]>("/api/github/installations");
      }
      setGithubInstallations(data);
    } catch {
      setGithubInstallations([]);
    }
  }

  useEffect(() => {
    void loadGitHubInstallations();
  }, [status]);

  useEffect(() => {
    const notice = githubNoticeFromSearch(location.search);
    if (!notice) {
      return;
    }

    setGithubNotice(notice);
    if (status === "authenticated") {
      void loadGitHubInstallations();
    }

    const params = new URLSearchParams(location.search);
    params.delete("github_connected");
    params.delete("github_connect_error");
    const nextSearch = params.toString();
    navigate(
      {
        pathname: location.pathname,
        search: nextSearch ? `?${nextSearch}` : "",
      },
      { replace: true },
    );
  }, [location.pathname, location.search, navigate, status]);

  function handleImported(repo: Repo | null, runtime?: RuntimeRef) {
    if (repo && runtime) {
      setRepos((prev) => [{ repository: repo, runtime }, ...prev]);
      setRepoLoadError(null);
    }
    setShowImport(false);
  }

  function handleRepoUpdated(updatedRepo: Repo, runtime: RuntimeRef) {
    setRepos((previousRepositories) =>
      previousRepositories.map((locatedRepository) => {
        const sameRuntime =
          runtimeIdentity(locatedRepository.runtime) ===
          runtimeIdentity(runtime);
        if (sameRuntime && locatedRepository.repository.id === updatedRepo.id) {
          return { repository: updatedRepo, runtime };
        }
        return locatedRepository;
      }),
    );
  }

  return (
    <aside className="flex h-full w-72 flex-col border-r border-gray-800 bg-gray-900">
      <div
        className={`flex items-center justify-between border-b border-gray-800 py-3 ${
          window.yinshiDesktop !== undefined ? "pl-24 pr-4" : "px-4"
        }`}
      >
        <span className="text-xs font-semibold uppercase tracking-wider text-gray-500">
          Workspaces
        </span>
        <button
          onClick={() => setShowImport(true)}
          disabled={!primaryRuntime || loading}
          className="text-gray-500 hover:text-gray-300 disabled:opacity-40"
          title="Add repository"
        >
          {PlusIcon}
        </button>
      </div>

      {showImport && availableRuntimes.length > 0 && (
        <ImportForm
          onDone={handleImported}
          canConnectGitHub={status === "authenticated"}
          githubInstallations={githubInstallations}
          runtimes={availableRuntimes}
        />
      )}

      <div className="flex-1 overflow-y-auto scrollbar-hide py-1">
        {githubNotice && (
          <div
            className={`mx-4 mt-4 rounded-md border px-3 py-2 text-xs ${
              githubNotice.tone === "success"
                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
                : "border-red-500/40 bg-red-500/10 text-red-300"
            }`}
          >
            {githubNotice.message}
          </div>
        )}

        {loading && (
          <div className="flex justify-center py-8">
            <div className="h-5 w-5 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
          </div>
        )}

        {!loading && repoLoadError && (
          <div className="mx-4 mt-4 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">
            <div>{repoLoadError}</div>
            <button
              onClick={() => void loadRepos()}
              className="mt-2 text-red-200 underline underline-offset-2 hover:text-red-100"
              type="button"
            >
              Retry
            </button>
          </div>
        )}

        {!loading &&
          locationErrors.map((locationError) => (
            <div
              key={locationError}
              className="mx-4 mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200"
            >
              {locationError}
            </div>
          ))}

        {!loading && !repoLoadError && repos.length === 0 && !showImport && (
          <div className="px-4 py-8 text-center text-sm text-gray-600">
            No repositories yet.
          </div>
        )}

        {repos.map(({ repository, runtime }) => (
          <RepoSection
            key={`${runtimeIdentity(runtime)}:${repository.id}`}
            repo={repository}
            runtime={runtime}
            activeSessionId={activeSessionId}
            userId={userId}
            onRepoUpdated={(updatedRepository) =>
              handleRepoUpdated(updatedRepository, runtime)
            }
            onNavigate={onNavigate}
          />
        ))}
      </div>

      <button
        onClick={() => setShowImport(true)}
        disabled={!primaryRuntime || loading}
        className="flex items-center gap-2 border-t border-gray-800 px-4 py-3 text-sm text-gray-500 hover:text-gray-300 disabled:opacity-40"
      >
        {PlusIcon}
        Add repository
      </button>

      <div className="flex items-center justify-between border-t border-gray-800 px-4 py-3">
        {status === "authenticated" && email ? (
          <>
            <span className="truncate text-xs text-gray-500" title={email}>
              {email}
            </span>
            <div className="flex items-center gap-2">
              <button
                onClick={() => {
                  navigate("/app/settings");
                  onNavigate?.();
                }}
                className="shrink-0 text-gray-600 hover:text-gray-400"
                title="Settings"
              >
                <svg
                  className="h-4 w-4"
                  fill="none"
                  viewBox="0 0 24 24"
                  strokeWidth={1.5}
                  stroke="currentColor"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z"
                  />
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"
                  />
                </svg>
              </button>
              <button
                onClick={toggleTheme}
                className="shrink-0 text-gray-600 hover:text-gray-400"
                title={
                  theme === "light"
                    ? "Switch to dark mode"
                    : "Switch to light mode"
                }
              >
                <ThemeIcon theme={theme} />
              </button>
              <button
                onClick={logout}
                className="shrink-0 text-xs text-gray-600 hover:text-gray-400"
              >
                Logout
              </button>
            </div>
          </>
        ) : (
          <button
            onClick={toggleTheme}
            className="text-gray-600 hover:text-gray-400"
            title={
              theme === "light" ? "Switch to dark mode" : "Switch to light mode"
            }
          >
            <ThemeIcon theme={theme} />
          </button>
        )}
      </div>
    </aside>
  );
}

function RepoSection({
  repo,
  runtime,
  activeSessionId,
  userId,
  onRepoUpdated,
  onNavigate,
}: {
  repo: Repo;
  runtime: RuntimeRef;
  activeSessionId: string | undefined;
  userId: string | null;
  onRepoUpdated: (repo: Repo) => void;
  onNavigate?: () => void;
}) {
  const navigate = useNavigate();
  const transport = useMemo(() => createRuntimeTransport(runtime), [runtime]);
  useEffect(
    () => () => {
      transport.close();
    },
    [transport],
  );
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [expanded, setExpanded] = useState(runtime.location !== "byoc");
  const [loaded, setLoaded] = useState(false);
  const [creating, setCreating] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [agentsMdDraft, setAgentsMdDraft] = useState(repo.agents_md ?? "");
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [settingsNotice, setSettingsNotice] = useState<string | null>(null);

  useEffect(() => {
    setAgentsMdDraft(repo.agents_md ?? "");
  }, [repo.agents_md]);

  useEffect(() => {
    if (expanded && !loaded) {
      transport
        .get<Workspace[]>(`/api/repos/${repo.id}/workspaces`)
        .then((data) => {
          setWorkspaces(data);
          setWorkspaceError(null);
          setLoaded(true);
        })
        .catch(() => {
          setWorkspaceError("Failed to load workspaces.");
        });
    }
  }, [expanded, loaded, repo.id, transport]);

  async function createBranch(e: React.MouseEvent) {
    e.stopPropagation();
    setCreating(true);
    setWorkspaceError(null);
    try {
      const ws = await transport.post<Workspace>(
        `/api/repos/${repo.id}/workspaces`,
        {},
      );
      setWorkspaces((prev) => [ws, ...prev]);
      setExpanded(true);
      // Auto-create a session and navigate to it
      const session = await transport.post<SessionInfo>(
        `/api/workspaces/${ws.id}/sessions`,
        { model: preferredSessionModel(userId) },
      );
      navigate(
        `/app/session/${runtimeResourceId(runtime, session.id, {
          desktop: window.yinshiDesktop !== undefined,
        })}`,
      );
      onNavigate?.();
    } catch {
      setWorkspaceError("Failed to create workspace.");
    } finally {
      setCreating(false);
    }
  }

  async function handleStateChange(workspaceId: string, newState: string) {
    try {
      const updated = await transport.patch<Workspace>(
        `/api/workspaces/${workspaceId}`,
        { state: newState },
      );
      setWorkspaces((prev) =>
        prev.map((ws) => (ws.id === workspaceId ? updated : ws)),
      );
    } catch {
      setWorkspaceError("Failed to update workspace state.");
    }
  }

  async function saveRepoSettings() {
    setSettingsSaving(true);
    setSettingsError(null);
    setSettingsNotice(null);
    try {
      const updatedRepo = await transport.patch<Repo>(`/api/repos/${repo.id}`, {
        agents_md: agentsMdDraft.trim() ? agentsMdDraft : null,
      });
      onRepoUpdated(updatedRepo);
      setAgentsMdDraft(updatedRepo.agents_md ?? "");
      setSettingsNotice("Repo instructions saved.");
    } catch {
      setSettingsError("Failed to save repo instructions.");
    } finally {
      setSettingsSaving(false);
    }
  }

  function cancelRepoSettings() {
    setAgentsMdDraft(repo.agents_md ?? "");
    setSettingsError(null);
    setSettingsNotice(null);
    setShowSettings(false);
  }

  function toggleRepoSettings(event: React.MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    setSettingsError(null);
    setSettingsNotice(null);

    if (showSettings) {
      setShowSettings(false);
    } else {
      setExpanded(true);
      setShowSettings(true);
    }
  }

  const activeWorkspaces = workspaces.filter((ws) => ws.state !== "archived");
  const archivedWorkspaces = workspaces.filter((ws) => ws.state === "archived");
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
          <span className="rounded border border-gray-700 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-gray-500">
            {runtimeLabel(runtime)}
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
            <svg
              className="h-3.5 w-3.5"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={2}
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M12 4.5v15m7.5-7.5h-15"
              />
            </svg>
          )}
        </button>
        <button
          onClick={toggleRepoSettings}
          className="shrink-0 px-3 py-2 text-gray-600 opacity-0 group-hover:opacity-100 hover:text-gray-300 transition-opacity"
          title="Repo settings"
        >
          <svg
            className="h-3.5 w-3.5"
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth={1.5}
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M4.5 12a7.5 7.5 0 1 1 15 0 7.5 7.5 0 0 1-15 0Zm7.5-3.75v3.75l2.25 2.25"
            />
          </svg>
        </button>
      </div>

      {expanded && (
        <>
          {showSettings && (
            <div className="space-y-2 border-b border-gray-800/80 bg-gray-950/50 px-4 py-3">
              <div className="pl-9">
                <label
                  htmlFor={`repo-agents-md-${repo.id}`}
                  className="text-[11px] font-semibold uppercase tracking-wide text-gray-500"
                >
                  AGENTS.md override
                </label>
                <p className="mt-1 text-xs text-gray-400">
                  Used as the repo-level runtime instructions for new sessions
                  from this repo.
                </p>
                <textarea
                  id={`repo-agents-md-${repo.id}`}
                  value={agentsMdDraft}
                  onChange={(event) => setAgentsMdDraft(event.target.value)}
                  rows={10}
                  spellCheck={false}
                  className="mt-2 w-full rounded-md border border-gray-800 bg-gray-900 px-3 py-2 text-sm text-gray-100 outline-none focus:border-blue-500"
                  placeholder="Leave blank to disable the repo-level override."
                />
                {settingsError && (
                  <div className="mt-2 rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">
                    {settingsError}
                  </div>
                )}
                {settingsNotice && !settingsError && (
                  <div className="mt-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">
                    {settingsNotice}
                  </div>
                )}
                <div className="mt-3 flex gap-2">
                  <button
                    type="button"
                    onClick={() => void saveRepoSettings()}
                    disabled={settingsSaving}
                    className="rounded-md bg-blue-500 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
                  >
                    {settingsSaving ? "Saving..." : "Save AGENTS.md"}
                  </button>
                  <button
                    type="button"
                    onClick={cancelRepoSettings}
                    disabled={settingsSaving}
                    className="rounded-md bg-gray-800 px-3 py-1.5 text-xs text-gray-400 disabled:opacity-40"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )}
          {workspaceError && (
            <div className="px-11 py-2 text-xs text-red-400">
              {workspaceError}
            </div>
          )}
          {activeWorkspaces.map((ws) => (
            <WorkspaceItem
              key={ws.id}
              workspace={ws}
              runtime={runtime}
              transport={transport}
              activeSessionId={activeSessionId}
              userId={userId}
              onNavigate={onNavigate}
              onArchive={() => handleStateChange(ws.id, "archived")}
            />
          ))}

          {archivedWorkspaces.length > 0 && (
            <>
              <button
                onClick={() => setShowArchived(!showArchived)}
                className="flex w-full items-center gap-1 px-11 py-1 text-xs text-gray-600 hover:text-gray-400"
              >
                <svg
                  className={`h-3 w-3 transition-transform ${showArchived ? "rotate-90" : ""}`}
                  fill="none"
                  viewBox="0 0 24 24"
                  strokeWidth={2}
                  stroke="currentColor"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M8.25 4.5l7.5 7.5-7.5 7.5"
                  />
                </svg>
                Archived ({archivedWorkspaces.length})
              </button>
              {showArchived &&
                archivedWorkspaces.map((ws) => (
                  <WorkspaceItem
                    key={ws.id}
                    workspace={ws}
                    runtime={runtime}
                    transport={transport}
                    activeSessionId={activeSessionId}
                    userId={userId}
                    onNavigate={onNavigate}
                    onRestore={() => handleStateChange(ws.id, "ready")}
                  />
                ))}
            </>
          )}
        </>
      )}
    </div>
  );
}

function WorkspaceItem({
  workspace,
  runtime,
  transport,
  activeSessionId,
  userId,
  onNavigate,
  onArchive,
  onRestore,
}: {
  workspace: Workspace;
  runtime: RuntimeRef;
  transport: RuntimeTransport;
  activeSessionId: string | undefined;
  userId: string | null;
  onNavigate?: () => void;
  onArchive?: () => void;
  onRestore?: () => void;
}) {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [loadedSessions, setLoadedSessions] = useState(false);
  const sessionCreationRef = useRef<Promise<SessionInfo> | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);

  useEffect(() => {
    if (!loadedSessions) {
      transport
        .get<SessionInfo[]>(`/api/workspaces/${workspace.id}/sessions`)
        .then((data) => {
          setSessions(data);
          setSessionError(null);
          setLoadedSessions(true);
        })
        .catch(() => {
          setSessionError("Failed to load sessions.");
        });
    }
  }, [workspace.id, loadedSessions, transport]);

  async function openOrCreateSession() {
    if (sessions.length > 0) {
      navigate(
        `/app/session/${runtimeResourceId(runtime, sessions[0].id, {
          desktop: window.yinshiDesktop !== undefined,
        })}`,
      );
      onNavigate?.();
      return;
    }

    if (sessionCreationRef.current) {
      return;
    }

    const creation = transport.post<SessionInfo>(
      `/api/workspaces/${workspace.id}/sessions`,
      { model: preferredSessionModel(userId) },
    );
    sessionCreationRef.current = creation;
    try {
      const session = await creation;
      setSessions([session]);
      navigate(
        `/app/session/${runtimeResourceId(runtime, session.id, {
          desktop: window.yinshiDesktop !== undefined,
        })}`,
      );
      onNavigate?.();
    } catch {
      setSessionError("Failed to create a session.");
    } finally {
      if (sessionCreationRef.current === creation) {
        sessionCreationRef.current = null;
      }
    }
  }

  const isActive = sessions.some(
    (session) =>
      runtimeResourceId(runtime, session.id, {
        desktop: window.yinshiDesktop !== undefined,
      }) === activeSessionId,
  );
  const hasRunning = sessions.some((s) => s.status === "running");

  return (
    <div
      className={`group flex w-full items-center py-1.5 pl-11 pr-4 hover:bg-gray-800/50 ${
        isActive ? "bg-gray-800/70" : ""
      }`}
    >
      <button
        onClick={openOrCreateSession}
        className="flex flex-1 items-center gap-2 min-w-0 text-left"
      >
        <span
          className={`h-2 w-2 shrink-0 rounded-full ${statusDotClass(hasRunning, workspace.state)}`}
        />
        <div className="flex-1 min-w-0">
          <div className="truncate text-sm text-gray-300">{workspace.name}</div>
          {sessionError ? (
            <div className="truncate text-[10px] text-red-400">
              {sessionError}
            </div>
          ) : null}
        </div>
      </button>
      {onArchive && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onArchive();
          }}
          className="shrink-0 ml-1 text-gray-600 opacity-0 group-hover:opacity-100 hover:text-gray-300 transition-opacity"
          title="Archive"
        >
          <svg
            className="h-3.5 w-3.5"
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth={1.5}
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="m20.25 7.5-.625 10.632a2.25 2.25 0 0 1-2.247 2.118H6.622a2.25 2.25 0 0 1-2.247-2.118L3.75 7.5M10 11.25h4M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125Z"
            />
          </svg>
        </button>
      )}
      {onRestore && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onRestore();
          }}
          className="shrink-0 ml-1 text-gray-600 opacity-0 group-hover:opacity-100 hover:text-gray-300 transition-opacity"
          title="Restore"
        >
          <svg
            className="h-3.5 w-3.5"
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth={1.5}
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M9 15 3 9m0 0 6-6M3 9h12a6 6 0 0 1 0 12h-3"
            />
          </svg>
        </button>
      )}
    </div>
  );
}

function ImportForm({
  onDone,
  canConnectGitHub,
  githubInstallations,
  runtimes,
}: {
  onDone: (repo: Repo | null, runtime?: RuntimeRef) => void;
  canConnectGitHub: boolean;
  githubInstallations: GitHubInstallation[];
  runtimes: RuntimeRef[];
}) {
  const [input, setInput] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorAction, setErrorAction] = useState<ImportAction | null>(null);
  const [selectedRuntimeIdentity, setSelectedRuntimeIdentity] = useState(
    runtimeIdentity(runtimes[0]),
  );
  const desktopBridge = window.yinshiDesktop;
  const selectedRuntime =
    runtimes.find(
      (runtime) => runtimeIdentity(runtime) === selectedRuntimeIdentity,
    ) ?? runtimes[0];
  const localPathImportEnabled =
    selectedRuntime.location === "local" ||
    (import.meta.env.DEV &&
      selectedRuntime.location === "hosted" &&
      !desktopBridge);

  useEffect(() => {
    if (
      !runtimes.some(
        (runtime) => runtimeIdentity(runtime) === selectedRuntimeIdentity,
      )
    ) {
      setSelectedRuntimeIdentity(runtimeIdentity(runtimes[0]));
    }
  }, [runtimes, selectedRuntimeIdentity]);

  async function handleDesktopImport() {
    if (!desktopBridge) return;
    setSubmitting(true);
    setError(null);
    setErrorAction(null);
    try {
      const result = await desktopBridge.importLocalRepository();
      if (result.status === "cancelled") return;
      const repo = await api.get<Repo>(`/api/repos/${result.repository.id}`);
      onDone(repo, { location: "local" });
    } catch {
      setError(
        "Local repository import failed. Check the repository and try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const value = input.trim();
    if (!value) return;
    setSubmitting(true);
    setError(null);
    setErrorAction(null);
    try {
      const body: { name: string; remote_url?: string; local_path?: string } = {
        name: deriveRepoName(value),
      };
      if (isGitUrl(value)) {
        body.remote_url = value;
      } else if (isLocalPath(value)) {
        if (!localPathImportEnabled) {
          setError("Local paths can only be imported to This Mac.");
          return;
        }
        if (desktopBridge) {
          setError(
            "Use Choose local repository so Yinshi can keep the selected checkout untouched.",
          );
          return;
        }
        body.local_path = value;
      } else if (isGithubShorthand(value)) {
        body.remote_url = `https://github.com/${value}`;
      } else {
        setError("Enter a GitHub URL, owner/repo, or local path.");
        return;
      }
      const importTransport = createRuntimeTransport(selectedRuntime);
      const repo = await importTransport
        .post<Repo>("/api/repos", body)
        .finally(() => {
          importTransport.close();
        });
      onDone(repo, selectedRuntime);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
        if (err.manageUrl) {
          setErrorAction({
            label: "Manage GitHub",
            href: err.manageUrl,
            external: true,
          });
        } else if (err.connectUrl) {
          setErrorAction({
            label: "Connect GitHub",
            href: err.connectUrl,
            external: false,
          });
        }
      } else {
        setError(err instanceof Error ? err.message : "Import failed");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="border-b border-gray-800 px-4 py-3 space-y-2"
    >
      <div>
        <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500">
          Import to
        </p>
        <div className="flex flex-wrap gap-1.5">
          {runtimes.map((runtime) => {
            const identity = runtimeIdentity(runtime);
            const selected = identity === runtimeIdentity(selectedRuntime);
            return (
              <button
                key={identity}
                type="button"
                onClick={() => setSelectedRuntimeIdentity(identity)}
                className={`rounded border px-2 py-1 text-xs ${
                  selected
                    ? "border-gray-500 bg-gray-800 text-gray-100"
                    : "border-gray-800 text-gray-500 hover:text-gray-300"
                }`}
              >
                {runtimeLabel(runtime)}
              </button>
            );
          })}
        </div>
      </div>
      {canConnectGitHub && selectedRuntime.location === "hosted" && (
        <div className="rounded-md border border-gray-800 bg-gray-950/60 px-3 py-2">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                Private GitHub repos
              </p>
              <p className="mt-1 text-xs text-gray-400">
                Connect GitHub once, then import private repos by URL.
              </p>
            </div>
            <a
              href="/auth/github/install"
              className="shrink-0 rounded-md border border-gray-700 px-2 py-1 text-[11px] text-gray-300 hover:border-gray-500 hover:text-gray-100"
            >
              Connect GitHub
            </a>
          </div>
          {githubInstallations.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {githubInstallations.map((installation) => (
                <a
                  key={installation.installation_id}
                  href={installation.html_url}
                  target="_blank"
                  rel="noreferrer"
                  className="rounded-full border border-gray-700 px-2 py-0.5 text-[11px] text-gray-300 hover:border-gray-500 hover:text-gray-100"
                  title={`${installation.account_login} (${installation.account_type})`}
                >
                  {installation.account_login}
                </a>
              ))}
            </div>
          )}
        </div>
      )}
      {desktopBridge && selectedRuntime.location === "local" && (
        <button
          type="button"
          onClick={handleDesktopImport}
          disabled={submitting}
          className="flex w-full items-center justify-center gap-2 rounded-md border border-gray-700 bg-gray-900 px-3 py-2 text-xs font-medium text-gray-200 hover:border-gray-500 hover:bg-gray-800 disabled:opacity-40"
        >
          <svg
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth={1.5}
            stroke="currentColor"
            aria-hidden="true"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M3.75 9.75h16.5m-15-4.5h4.129c.414 0 .81.171 1.094.472l1.054 1.117c.283.3.679.472 1.093.472h6.13c.828 0 1.5.672 1.5 1.5v8.939a1.5 1.5 0 0 1-1.5 1.5H5.25a1.5 1.5 0 0 1-1.5-1.5v-11a1.5 1.5 0 0 1 1.5-1.5Z"
            />
          </svg>
          {submitting
            ? "Importing local repository..."
            : "Choose local repository"}
        </button>
      )}
      <input
        type="text"
        placeholder={
          localPathImportEnabled
            ? "GitHub URL, user/repo, or local path"
            : "HTTPS GitHub URL or user/repo"
        }
        value={input}
        onChange={(e) => setInput(e.target.value)}
        className="w-full rounded-md bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 outline-none focus:ring-1 focus:ring-blue-500"
        autoFocus
      />
      {error && (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">
          <p>{error}</p>
          {errorAction && (
            <a
              href={errorAction.href}
              target={errorAction.external ? "_blank" : undefined}
              rel={errorAction.external ? "noreferrer" : undefined}
              className="mt-2 inline-flex text-red-100 underline underline-offset-2 hover:text-white"
            >
              {errorAction.label}
            </a>
          )}
        </div>
      )}
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
