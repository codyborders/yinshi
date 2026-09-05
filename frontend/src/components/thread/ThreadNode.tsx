import type {
  ThreadNodeOut,
  ThreadPlaceholderOut,
} from "../../api/client";
import ThreadStatusBadge, { type ThreadStatus } from "./ThreadStatusBadge";

const MAX_INDENT_DEPTH = 6;
const ACTIVE_STATES = new Set<ThreadStatus>(["provisioning", "queued", "running"]);
const RETRYABLE_STATES = new Set<ThreadStatus>(["failed", "cancelled", "interrupted"]);

export interface ThreadNodeProps {
  node?: ThreadNodeOut;
  placeholder?: ThreadPlaceholderOut;
  current?: boolean;
  onNavigate?: (threadId: string) => void;
  onCancel?: (threadId: string) => void;
  onRetry?: (threadId: string) => void;
  className?: string;
}

function roleTitle(role: string): string {
  const readableRole = role.trim() || "general";
  return `${readableRole.charAt(0).toUpperCase()}${readableRole.substring(1)} thread`;
}

function nodeTitle(node: ThreadNodeOut): string {
  return node.title?.trim() || roleTitle(node.role);
}

export default function ThreadNode({
  node,
  placeholder,
  current = false,
  onNavigate,
  onCancel,
  onRetry,
  className = "",
}: ThreadNodeProps) {
  if (!node && !placeholder) return null;
  if (node && placeholder) return null;

  const title = node ? nodeTitle(node) : placeholder!.title;
  const role = node?.role ?? placeholder!.role;
  const state: ThreadStatus = node?.state ?? placeholder!.status;
  const depth = Math.min(Math.max(node?.depth ?? 0, 0), MAX_INDENT_DEPTH);
  const actionId = node?.id ?? placeholder!.delegation_id;
  const retryable = RETRYABLE_STATES.has(state);
  const cancellable = ACTIVE_STATES.has(state);

  return (
    <div
      className={`flex min-w-0 items-center gap-1 border-b border-gray-800/70 py-1 ${current ? "bg-blue-500/10" : ""} ${className}`.trim()}
      style={{ paddingInlineStart: `${depth}rem` }}
    >
      <button
        type="button"
        onClick={() => onNavigate?.(actionId)}
        aria-current={current ? "page" : undefined}
        aria-label={`Open ${title}`}
        className="min-w-0 flex-1 rounded px-2 py-2 text-left text-sm text-gray-200 hover:bg-gray-800 focus:outline-none focus:ring-1 focus:ring-blue-500"
      >
        <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <span className="min-w-0 flex-1 truncate font-medium" title={title}>{title}</span>
          <span className="shrink-0 text-xs text-gray-500">{role}</span>
          <ThreadStatusBadge state={state} />
          {current ? <span className="sr-only">Current thread</span> : null}
        </span>
      </button>
      {onCancel && cancellable ? (
        <button type="button" onClick={() => onCancel(actionId)} aria-label={`Cancel ${title}`} className="min-h-touch shrink-0 rounded px-2 text-xs text-gray-400 hover:bg-gray-800 hover:text-gray-200 focus:outline-none focus:ring-1 focus:ring-blue-500">Cancel</button>
      ) : null}
      {onRetry && retryable ? (
        <button type="button" onClick={() => onRetry(actionId)} aria-label={`Retry ${title}`} className="min-h-touch shrink-0 rounded px-2 text-xs text-gray-400 hover:bg-gray-800 hover:text-gray-200 focus:outline-none focus:ring-1 focus:ring-blue-500">Retry</button>
      ) : null}
    </div>
  );
}
