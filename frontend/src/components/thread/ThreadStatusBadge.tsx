import type { ThreadLifecycleStatus } from "../../api/client";

export type ThreadStatus = ThreadLifecycleStatus | string;

const STATUS_LABELS: Record<ThreadLifecycleStatus, string> = {
  provisioning: "Provisioning",
  queued: "Queued",
  running: "Running",
  cancelling: "Cancelling",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  interrupted: "Interrupted",
};

const STATUS_CLASSES: Record<ThreadLifecycleStatus, string> = {
  provisioning: "border-blue-500/40 bg-blue-500/10 text-blue-300",
  queued: "border-gray-600 bg-gray-800 text-gray-300",
  running: "border-blue-500/40 bg-blue-500/10 text-blue-300",
  cancelling: "border-yellow-500/40 bg-yellow-500/10 text-yellow-300",
  completed: "border-green-500/40 bg-green-500/10 text-green-300",
  failed: "border-red-500/40 bg-red-500/10 text-red-300",
  cancelled: "border-gray-600 bg-gray-800 text-gray-400",
  interrupted: "border-yellow-500/40 bg-yellow-500/10 text-yellow-300",
};

function labelFor(state: ThreadStatus): string {
  if (state in STATUS_LABELS) {
    return STATUS_LABELS[state as ThreadLifecycleStatus];
  }
  return state ? state.charAt(0).toUpperCase() + state.substring(1) : "Unknown";
}

export interface ThreadStatusBadgeProps {
  state: ThreadStatus;
  className?: string;
}

export default function ThreadStatusBadge({
  state,
  className = "",
}: ThreadStatusBadgeProps) {
  const knownState = state as ThreadLifecycleStatus;
  const animate = knownState === "running" || knownState === "provisioning";
  const classes = STATUS_CLASSES[knownState] ?? "border-gray-600 bg-gray-800 text-gray-400";

  return (
    <span
      role="status"
      aria-label={`Status: ${labelFor(state)}`}
      className={`inline-flex shrink-0 items-center rounded border px-2 py-0.5 text-xs font-medium ${classes} ${animate ? "motion-safe:animate-pulse" : ""} ${className}`.trim()}
    >
      {labelFor(state)}
    </span>
  );
}
