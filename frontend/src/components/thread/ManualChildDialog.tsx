import { useEffect, useRef, useState, type FormEvent } from "react";

import type {
  ThreadChildCreate,
  ThreadLimitsOut,
  ThinkingLevel,
} from "../../api/client";

export interface ManualChildDialogProps {
  open: boolean;
  onClose: () => void;
  onSubmit: (payload: ThreadChildCreate) => void | Promise<void>;
  limits?: ThreadLimitsOut | null;
  submitting?: boolean;
  serverError?: string | null;
}

export default function ManualChildDialog({
  open,
  onClose,
  onSubmit,
  limits,
  submitting = false,
  serverError,
}: ManualChildDialogProps) {
  const fieldClass =
    "mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 outline-none focus:border-blue-500";
  const labelClass = "block text-xs font-medium text-gray-400";
  const [title, setTitle] = useState("");
  const [task, setTask] = useState("");
  const [context, setContext] = useState("");
  const [role, setRole] = useState<NonNullable<ThreadChildCreate["role"]>>("general");
  const [model, setModel] = useState("");
  const [thinking, setThinking] = useState<ThinkingLevel | "">("");
  const [startImmediately, setStartImmediately] = useState(true);
  const [requestPending, setRequestPending] = useState(false);
  const titleRef = useRef<HTMLInputElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const wasOpenRef = useRef(false);
  const capacityReached = Boolean(
    limits &&
      (limits.can_spawn_child === false ||
        limits.tree_depth >= limits.max_depth ||
        limits.direct_children >= limits.max_direct_children ||
        limits.active_descendants >= limits.max_active_descendants ||
        limits.total_threads >= limits.max_total_threads),
  );

  useEffect(() => {
    if (open && !wasOpenRef.current) {
      previousFocusRef.current =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      titleRef.current?.focus();
    } else if (!open && wasOpenRef.current) {
      previousFocusRef.current?.focus();
    }
    wasOpenRef.current = open;
  }, [open]);

  if (!open) return null;

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !title.trim() ||
      !task.trim() ||
      submitting ||
      requestPending ||
      capacityReached
    ) {
      return;
    }
    setRequestPending(true);
    let result: void | Promise<void>;
    try {
      result = onSubmit({
        idempotency_key: crypto.randomUUID(),
        title: title.trim(),
        task: task.trim(),
        context: context.trim() || null,
        role,
        model: model.trim() || null,
        thinking: thinking || null,
        start_immediately: startImmediately,
      });
    } catch {
      setRequestPending(false);
      return;
    }
    void Promise.resolve(result).then(
      () => setRequestPending(false),
      () => setRequestPending(false),
    );
  }

  return (
    <div
      data-testid="child-dialog-backdrop"
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center bg-gray-950/75 p-4"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="child-dialog-title"
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            onClose();
            return;
          }
          if (event.key !== "Tab") return;
          const focusable = Array.from(
            event.currentTarget.querySelectorAll<HTMLElement>(
              "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled])",
            ),
          );
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (!first || !last) return;
          if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          } else if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          }
        }}
        className="max-h-[min(90vh,48rem)] w-full max-w-2xl overflow-y-auto rounded-xl border border-gray-700 bg-gray-900 p-5 text-gray-200 shadow-2xl"
      >
        <h2 id="child-dialog-title" className="text-lg font-semibold text-gray-100">
          Create child thread
        </h2>
      <form onSubmit={handleSubmit} className="mt-5 space-y-4">
        {capacityReached && (
          <p role="alert">Child capacity reached. Wait for an available slot.</p>
        )}
        {serverError && <p role="alert">{serverError}</p>}
        <label htmlFor="child-title" className={labelClass}>Title</label>
        <input
          id="child-title"
          ref={titleRef}
          className={fieldClass}
          value={title}
          onChange={(event) => setTitle(event.target.value)}
        />
        <label htmlFor="child-task" className={labelClass}>Task</label>
        <textarea
          id="child-task"
          className={`${fieldClass} min-h-28 resize-y`}
          value={task}
          onChange={(event) => setTask(event.target.value)}
        />
        <label htmlFor="child-context" className={labelClass}>Context</label>
        <textarea
          id="child-context"
          className={`${fieldClass} min-h-20 resize-y`}
          value={context}
          onChange={(event) => setContext(event.target.value)}
        />
        <label htmlFor="child-role" className={labelClass}>Role</label>
        <select
          id="child-role"
          className={fieldClass}
          value={role}
          onChange={(event) =>
            setRole(event.target.value as NonNullable<ThreadChildCreate["role"]>)
          }
        >
          <option value="general">General</option>
          <option value="research">Research</option>
          <option value="implementation">Implementation</option>
          <option value="test">Test</option>
          <option value="review">Review</option>
          <option value="debug">Debug</option>
        </select>
        <label htmlFor="child-model" className={labelClass}>Model</label>
        <input
          id="child-model"
          className={fieldClass}
          value={model}
          onChange={(event) => setModel(event.target.value)}
        />
        <label htmlFor="child-thinking" className={labelClass}>Thinking</label>
        <select
          id="child-thinking"
          className={fieldClass}
          value={thinking}
          onChange={(event) => setThinking(event.target.value as ThinkingLevel | "")}
        >
          <option value="">Model default</option>
          <option value="off">Off</option>
          <option value="minimal">Minimal</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
          <option value="xhigh">Extra high</option>
        </select>
        <label htmlFor="child-start-immediately" className="flex items-center gap-2 text-sm text-gray-300">
          <input
            id="child-start-immediately"
            type="checkbox"
            checked={startImmediately}
            onChange={(event) => setStartImmediately(event.target.checked)}
          />
          Start immediately
        </label>
        <div className="flex justify-end gap-2 border-t border-gray-800 pt-4">
          <button
            type="button"
            onClick={onClose}
            className="min-h-11 rounded-lg border border-gray-700 px-4 text-sm text-gray-300 hover:bg-gray-800"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitting || requestPending || capacityReached}
            className="min-h-11 rounded-lg bg-blue-600 px-4 text-sm font-medium text-white hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Create child
          </button>
        </div>
        </form>
      </div>
    </div>
  );
}
