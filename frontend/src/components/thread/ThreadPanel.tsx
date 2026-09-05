import { useState } from "react";
import type {
  ThreadChildCreate,
  ThreadLimitsOut,
  ThreadResultOut,
  ThreadSpawnOut,
  ThreadTreeOut,
} from "../../api/client";
import ManualChildDialog from "./ManualChildDialog";
import ThreadNode from "./ThreadNode";
import ThreadResult from "./ThreadResult";
export interface ThreadPanelProps { tree: ThreadTreeOut | null; loading?: boolean; error?: string | null; limits?: ThreadLimitsOut | null; result?: ThreadResultOut | null; resultLoading?: boolean; resultError?: string | null; currentThreadId?: string | null; onNavigate?: (threadId: string) => void; onCancel?: (threadId: string) => void; onRetry?: (threadId: string) => void; onCreateChild?: (payload: ThreadChildCreate) => ThreadSpawnOut | null | Promise<ThreadSpawnOut | null>; }
export default function ThreadPanel({ tree, loading = false, error = null, limits = null, result = null, resultLoading = false, resultError = null, onCreateChild, currentThreadId = null, onNavigate, onCancel, onRetry }: ThreadPanelProps) {
  const [dialogOpen, setDialogOpen] = useState(false);
  if (loading) return <section role="status">Loading thread tree...</section>;
  if (error && !tree) return <section role="alert">{error}</section>;
  if (!tree) return <section>No thread tree available.</section>;
  const currentId = currentThreadId ?? tree.root.id;
  return (
    <section data-testid="thread-panel" className="flex min-h-0 w-full min-w-0 flex-col overflow-hidden">
      {error && !dialogOpen ? (
        <p role="alert" className="m-3 text-sm text-red-300">{error}</p>
      ) : null}
      <button type="button" onClick={() => setDialogOpen(true)} disabled={!onCreateChild || !tree.root.can_spawn_child}>Create child thread</button>
      <nav aria-label="Thread tree" className="min-h-0 min-w-0 overflow-y-auto overflow-x-hidden">
        <ThreadNode node={tree.root} current={currentId === tree.root.id} onNavigate={onNavigate} onCancel={onCancel} onRetry={onRetry} />
        {tree.nodes.map((node) => <ThreadNode key={node.id} node={node} current={currentId === node.id} onNavigate={onNavigate} onCancel={onCancel} onRetry={onRetry} />)}
        {tree.placeholders.map((placeholder) => <ThreadNode key={placeholder.delegation_id} placeholder={placeholder} current={currentId === placeholder.delegation_id} onNavigate={onNavigate} onCancel={onCancel} onRetry={onRetry} />)}
      </nav>
      {(result !== null || resultLoading || resultError !== null) && (
        <div className="min-h-0 min-w-0 overflow-y-auto border-t border-gray-800 p-3">
          <ThreadResult result={result} loading={resultLoading} error={resultError} />
        </div>
      )}
      <ManualChildDialog open={dialogOpen} onClose={() => setDialogOpen(false)} onSubmit={async (payload) => {
        const created = await onCreateChild?.(payload);
        if (created) setDialogOpen(false);
      }} limits={limits} serverError={error} />
    </section>
  );
}
