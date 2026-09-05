import type { ThreadResultOut } from "../../api/client";

function changedFilePath(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && "path" in value) {
    const path = (value as { path?: unknown }).path;
    return typeof path === "string" ? path : null;
  }
  return null;
}

export interface ThreadResultProps { result?: ThreadResultOut | null; loading?: boolean; error?: string | null; sealed?: boolean; className?: string; }
export default function ThreadResult({
  result = null,
  loading = false,
  error = null,
  sealed,
  className = "",
}: ThreadResultProps) {
  if (loading) return <div role="status">Loading thread result...</div>;
  if (error) return <div role="alert">{error}</div>;
  if (!result) return <div>No result is available.</div>;
  if (!(sealed ?? result.sealed)) return <div>Result is not sealed yet.</div>;
  return (
    <section
      data-testid="thread-result"
      className={`min-w-0 max-w-full space-y-4 overflow-hidden ${className}`.trim()}
    >
      <h2 className="text-sm font-semibold text-gray-100">Thread result</h2>
      {result.summary ? (
        <p className="whitespace-pre-wrap break-words text-sm text-gray-300">
          {result.summary}
        </p>
      ) : null}
      {result.warnings.length > 0 ? (
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-yellow-400">
            Warnings
          </h3>
          <ul className="mt-2 space-y-1">
            {result.warnings.map((warning, index) => (
              <li key={`${warning}-${index}`} className="break-words text-sm text-yellow-200">
                {warning}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {result.changed_files.length > 0 ? (
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">
            Changed files
          </h3>
          <ul className="mt-2 space-y-1">
            {result.changed_files.map(changedFilePath).map((path, index) =>
              path ? (
                <li key={`${path}-${index}`} className="break-all text-sm text-gray-300">
                  {path}
                </li>
              ) : null,
            )}
          </ul>
        </div>
      ) : null}
      {result.result_commit || result.result_ref ? (
        <dl className="min-w-0 space-y-1 text-xs text-gray-500">
          {result.result_commit ? (
            <div className="flex min-w-0 flex-wrap gap-x-2">
              <dt className="shrink-0">Result commit</dt>
              <dd className="min-w-0 break-all font-mono text-gray-300">
                {result.result_commit}
              </dd>
            </div>
          ) : null}
          {result.result_ref ? (
            <div className="flex min-w-0 flex-wrap gap-x-2">
              <dt className="shrink-0">Result ref</dt>
              <dd className="min-w-0 break-all font-mono text-gray-300">
                {result.result_ref}
              </dd>
            </div>
          ) : null}
        </dl>
      ) : null}
      {result.tests.length > 0 ? (
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">
            Checks
          </h3>
          <ul className="mt-2 space-y-2">
            {result.tests.map((test, index) => (
              <li key={`${test.command}-${index}`} className="min-w-0 text-sm">
                <code className="break-all text-gray-200">{test.command}</code>
                <span className="ml-2 text-gray-500">{test.status}</span>
                {test.summary ? (
                  <p className="break-words text-xs text-gray-500">{test.summary}</p>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
