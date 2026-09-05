import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  type ThreadChildrenOut,
  type ThreadChildCreate,
  type ThreadLimitsOut,
  type ThreadResultOut,
  type ThreadResultReportCreate,
  type ThreadRetryCreate,
  type ThreadSpawnOut,
  type ThreadTreeOut,
} from "../api/client";
import type { RuntimeTransport } from "../runtime/runtimeTransport";

export interface UseThreadTreeState {
  loading: boolean;
  error: string | null;
  tree: ThreadTreeOut | null;
  children: ThreadChildrenOut | null;
  limits: ThreadLimitsOut | null;
  result: ThreadResultOut | null;
  refresh: () => Promise<void>;
  createChild: (body: ThreadChildCreate) => Promise<ThreadSpawnOut | null>;
  cancelChild: (threadId: string) => Promise<boolean>;
  retryChild: (threadId: string, body: ThreadRetryCreate) => Promise<ThreadSpawnOut | null>;
  reportResult: (
    threadId: string,
    body: ThreadResultReportCreate,
  ) => Promise<ThreadResultOut | null>;
}

function statusOf(error: unknown): number | null {
  if (error instanceof ApiError) return error.status;
  if (error !== null && typeof error === "object" && "status" in error) {
    const status = (error as { status?: unknown }).status;
    return typeof status === "number" ? status : null;
  }
  return null;
}

function messageOf(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export function useThreadTree(
  sessionId: string | undefined,
  transport: RuntimeTransport | undefined,
): UseThreadTreeState {
  const [loading, setLoading] = useState(Boolean(sessionId && transport));
  const [error, setError] = useState<string | null>(null);
  const [tree, setTree] = useState<ThreadTreeOut | null>(null);
  const [children, setChildren] = useState<ThreadChildrenOut | null>(null);
  const [limits, setLimits] = useState<ThreadLimitsOut | null>(null);
  const [result, setResult] = useState<ThreadResultOut | null>(null);
  const generationRef = useRef(0);
  const mountedRef = useRef(true);
  const pendingActionsRef = useRef(new Set<string>());

  const refresh = useCallback(async (): Promise<void> => {
    if (!sessionId || !transport) {
      if (mountedRef.current) {
        setLoading(false);
        setTree(null);
        setChildren(null);
        setLimits(null);
        setResult(null);
      }
      return;
    }
    const generation = generationRef.current;
    setLoading(true);
    setError(null);
    const base = `/api/threads/${sessionId}`;
    const resultRequest = transport
      .get<ThreadResultOut>(`${base}/result`)
      .catch((requestError: unknown) => {
        if (statusOf(requestError) === 404) return null;
        throw requestError;
      });
    try {
      const [nextTree, nextChildren, nextLimits, nextResult] = await Promise.all([
        transport.get<ThreadTreeOut>(`${base}/tree`),
        transport.get<ThreadChildrenOut>(`${base}/children`),
        transport.get<ThreadLimitsOut>(`${base}/limits`),
        resultRequest,
      ]);
      if (!mountedRef.current || generation !== generationRef.current) return;
      setTree(nextTree);
      setChildren(nextChildren);
      setLimits(nextLimits);
      setResult(nextResult);
      setError(null);
    } catch (requestError) {
      if (!mountedRef.current || generation !== generationRef.current) return;
      setError(messageOf(requestError, "Failed to load thread tree"));
    } finally {
      if (mountedRef.current && generation === generationRef.current) setLoading(false);
    }
  }, [sessionId, transport]);

  useEffect(() => {
    mountedRef.current = true;
    generationRef.current += 1;
    setTree(null);
    setChildren(null);
    setLimits(null);
    setResult(null);
    void refresh();
    return () => {
      mountedRef.current = false;
    };
  }, [refresh]);

  async function runMutation<T>(
    action: string,
    operation: () => Promise<T>,
    fallback: string,
  ): Promise<T | null> {
    if (!transport || !sessionId || pendingActionsRef.current.has(action)) return null;
    pendingActionsRef.current.add(action);
    setError(null);
    try {
      return await operation();
    } catch (requestError) {
      if (mountedRef.current) setError(messageOf(requestError, fallback));
      return null;
    } finally {
      pendingActionsRef.current.delete(action);
    }
  }

  async function createChild(body: ThreadChildCreate): Promise<ThreadSpawnOut | null> {
    const spawn = await runMutation(
      "create",
      () => transport!.post<ThreadSpawnOut>(`/api/threads/${sessionId}/children`, body),
      "Failed to create child thread",
    );
    if (!spawn || !mountedRef.current) return spawn;
    setTree((currentTree) => {
      if (!currentTree) return currentTree;
      return {
        ...currentTree,
        placeholders: [
          ...currentTree.placeholders,
          {
            delegation_id: spawn.delegation_id,
            parent_id: sessionId!,
            title: body.title,
            role: body.role ?? "general",
            status: "provisioning",
            created_at: new Date().toISOString(),
          },
        ],
        thread_count: currentTree.thread_count + 1,
      };
    });
    await refresh();
    return spawn;
  }

  async function cancelChild(threadId: string): Promise<boolean> {
    const response = await runMutation(
      `cancel:${threadId}`,
      () => transport!.post<ThreadSpawnOut>(`/api/threads/${threadId}/cancel`),
      "Failed to cancel child thread",
    );
    if (!response) return false;
    await refresh();
    return true;
  }

  async function retryChild(
    threadId: string,
    body: ThreadRetryCreate,
  ): Promise<ThreadSpawnOut | null> {
    const response = await runMutation(
      `retry:${threadId}`,
      () => transport!.post<ThreadSpawnOut>(`/api/threads/${threadId}/retry`, body),
      "Failed to retry child thread",
    );
    if (!response) return null;
    await refresh();
    return response;
  }

  async function reportResult(
    threadId: string,
    body: ThreadResultReportCreate,
  ): Promise<ThreadResultOut | null> {
    const response = await runMutation(
      `report:${threadId}`,
      () => transport!.post<ThreadResultOut>(`/api/threads/${threadId}/report`, body),
      "Failed to report thread result",
    );
    if (!response) return null;
    await refresh();
    return response;
  }

  return {
    loading,
    error,
    tree,
    children,
    limits,
    result,
    refresh,
    createChild,
    cancelChild,
    retryChild,
    reportResult,
  };
}
