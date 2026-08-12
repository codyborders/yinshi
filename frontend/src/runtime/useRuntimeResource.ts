import { useEffect, useState } from "react";

import { parseRuntimeResourceId } from "./runtimeRef";
import { resolveRuntimeRef } from "./resolveRuntime";
import {
  createRuntimeTransport,
  type RuntimeRef,
  type RuntimeTransport,
} from "./runtimeTransport";

export interface ResolvedRuntimeResource {
  readonly resourceId: string;
  readonly runtime: RuntimeRef;
  readonly transport: RuntimeTransport;
}

export interface RuntimeResourceState {
  readonly resource: ResolvedRuntimeResource | null;
  readonly loading: boolean;
  readonly error: string | null;
}

export function useRuntimeResource(encodedId: string | undefined): RuntimeResourceState {
  const [state, setState] = useState<RuntimeResourceState>({
    resource: null,
    loading: true,
    error: null,
  });

  useEffect(() => {
    let cancelled = false;
    let transport: RuntimeTransport | null = null;
    if (!encodedId) {
      setState({ resource: null, loading: false, error: "Runtime resource ID is missing" });
      return () => {
        cancelled = true;
      };
    }

    const controller = new AbortController();
    setState({ resource: null, loading: true, error: null });
    void (async () => {
      try {
        const parsed = parseRuntimeResourceId(encodedId, {
          desktop: window.yinshiDesktop !== undefined,
        });
        const runtime = await resolveRuntimeRef(
          parsed.runtime,
          {},
          controller.signal,
        );
        if (!cancelled) {
          transport = createRuntimeTransport(runtime);
          setState({
            resource: {
              resourceId: parsed.resourceId,
              runtime,
              transport,
            },
            loading: false,
            error: null,
          });
        }
      } catch (error) {
        if (!cancelled) {
          setState({
            resource: null,
            loading: false,
            error: error instanceof Error ? error.message : "Runtime could not be resolved",
          });
        }
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
      transport?.close();
    };
  }, [encodedId]);

  return state;
}
