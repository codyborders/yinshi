import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { type PiPackageRelease, type PiReleaseNotes } from "../api/client";
import { usePiReleaseNotes } from "../hooks/usePiReleaseNotes";

function formatDate(timestamp: string | null): string {
  if (!timestamp) {
    return "Unknown date";
  }
  return new Date(timestamp).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function formatVersion(version: string | null): string {
  return version || "Unknown";
}

function buildVersionStatus(releaseNotes: PiReleaseNotes): { text: string; tone: "current" | "warning" } {
  if (!releaseNotes.installed_version) {
    return { text: "Runtime version unavailable", tone: "warning" };
  }
  if (!releaseNotes.latest_version) {
    return { text: "Latest version unavailable", tone: "warning" };
  }
  if (releaseNotes.installed_version === releaseNotes.latest_version) {
    return { text: "Up to date", tone: "current" };
  }
  return { text: "Update available", tone: "warning" };
}

function updateStatusText(releaseNotes: PiReleaseNotes): string {
  const updateStatus = releaseNotes.update_status;
  if (!updateStatus) {
    return "No daily update run recorded yet.";
  }
  if (updateStatus.message) {
    return updateStatus.message;
  }
  if (updateStatus.status === "current") {
    return "Daily updater found pi already current.";
  }
  if (updateStatus.status === "updated") {
    return "Daily updater installed a newer pi package.";
  }
  if (updateStatus.status === "failed") {
    return "Daily updater failed on the last run.";
  }
  return "Daily updater status recorded.";
}

function ReleaseMarkdown({ body }: { body: string }) {
  if (!body.trim()) {
    return <p className="text-sm text-gray-500">No release notes published.</p>;
  }
  return (
    <div className="markdown-prose text-sm text-gray-300">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
}

function ReleaseCard({ release }: { release: PiPackageRelease }) {
  return (
    <article className="rounded-xl border border-gray-800 bg-gray-900/70 p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold text-gray-100">{release.name}</h3>
          <p className="mt-1 text-xs text-gray-500">
            {release.tag_name} · {formatDate(release.published_at)}
          </p>
        </div>
        <a
          href={release.html_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs text-blue-400 hover:text-blue-300"
        >
          View on GitHub
        </a>
      </div>
      <ReleaseMarkdown body={release.body_markdown} />
    </article>
  );
}

function RuntimeSummary({ releaseNotes }: { releaseNotes: PiReleaseNotes }) {
  const status = buildVersionStatus(releaseNotes);
  const statusClassName = status.tone === "current" ? "text-green-400" : "text-amber-300";
  return (
    <div className="grid gap-3 md:grid-cols-3">
      <div className="rounded-xl border border-gray-800 bg-gray-900/70 p-4">
        <div className="text-xs uppercase tracking-wide text-gray-500">Installed</div>
        <div className="mt-2 font-mono text-lg text-gray-100">
          {formatVersion(releaseNotes.installed_version)}
        </div>
        {releaseNotes.node_version ? (
          <div className="mt-1 text-xs text-gray-500">Node {releaseNotes.node_version}</div>
        ) : null}
      </div>
      <div className="rounded-xl border border-gray-800 bg-gray-900/70 p-4">
        <div className="text-xs uppercase tracking-wide text-gray-500">Latest</div>
        <div className="mt-2 font-mono text-lg text-gray-100">
          {formatVersion(releaseNotes.latest_version)}
        </div>
        <div className={`mt-1 text-xs ${statusClassName}`}>{status.text}</div>
      </div>
      <div className="rounded-xl border border-gray-800 bg-gray-900/70 p-4">
        <div className="text-xs uppercase tracking-wide text-gray-500">Daily updater</div>
        <div className="mt-2 text-sm text-gray-200">{updateStatusText(releaseNotes)}</div>
        {releaseNotes.update_status?.checked_at ? (
          <div className="mt-1 text-xs text-gray-500">
            Last checked {new Date(releaseNotes.update_status.checked_at).toLocaleString()}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default function PiReleaseNotesSection() {
  const { releaseNotes, loading, error, refresh } = usePiReleaseNotes();

  return (
    <section className="space-y-4" aria-labelledby="pi-release-notes-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="pi-release-notes-heading" className="text-lg font-semibold text-gray-200">
            Pi release notes
          </h2>
          <p className="mt-1 text-sm text-gray-400">
            Yinshi updates the bundled pi package daily and rebuilds the sidecar image when npm publishes a new version.
          </p>
          {releaseNotes ? (
            <a
              href={releaseNotes.release_notes_url}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-2 inline-flex text-sm text-blue-400 hover:text-blue-300"
            >
              View all pi releases
            </a>
          ) : null}
        </div>
        <button
          type="button"
          onClick={() => {
            void refresh();
          }}
          disabled={loading}
          className="rounded border border-gray-700 px-3 py-2 text-sm text-gray-200 hover:bg-gray-800 disabled:opacity-50"
        >
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      {loading && !releaseNotes ? (
        <div className="rounded border border-gray-700 bg-gray-800 px-4 py-3 text-sm text-gray-400">
          Loading pi release notes...
        </div>
      ) : null}

      {error ? (
        <div className="rounded border border-red-900/50 bg-gray-800 px-4 py-3 text-sm text-red-400">
          {error}
        </div>
      ) : null}

      {releaseNotes ? (
        <>
          <RuntimeSummary releaseNotes={releaseNotes} />
          {releaseNotes.runtime_error ? (
            <div className="rounded border border-amber-900/50 bg-amber-950/20 px-4 py-3 text-sm text-amber-200">
              Runtime version unavailable: {releaseNotes.runtime_error}
            </div>
          ) : null}
          {releaseNotes.release_error ? (
            <div className="rounded border border-amber-900/50 bg-amber-950/20 px-4 py-3 text-sm text-amber-200">
              Showing cached release notes. Latest fetch failed: {releaseNotes.release_error}
            </div>
          ) : null}
          <div className="space-y-4">
            {releaseNotes.releases.map((release) => (
              <ReleaseCard key={release.tag_name} release={release} />
            ))}
            {releaseNotes.releases.length === 0 && !loading ? (
              <div className="rounded border border-gray-800 bg-gray-900/70 px-4 py-3 text-sm text-gray-500">
                No release notes are available yet.
              </div>
            ) : null}
          </div>
        </>
      ) : null}
    </section>
  );
}
