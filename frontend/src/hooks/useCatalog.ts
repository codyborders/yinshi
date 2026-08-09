import { useEffect, useState } from "react";

import { api, type ProviderCatalog } from "../api/client";
import type { RuntimeTransport } from "../runtime/runtimeTransport";

export function useCatalog(runtimeTransport?: RuntimeTransport) {
  const [catalog, setCatalog] = useState<ProviderCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const catalogClient = runtimeTransport ?? api;

    async function loadCatalog() {
      try {
        const loadedCatalog = await catalogClient.get<ProviderCatalog>("/api/catalog");
        if (cancelled) {
          return;
        }
        setCatalog(loadedCatalog);
        setError(null);
      } catch (loadError) {
        if (cancelled) {
          return;
        }
        setError(loadError instanceof Error ? loadError.message : "Failed to load catalog");
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void loadCatalog();
    return () => {
      cancelled = true;
    };
  }, [runtimeTransport]);

  return { catalog, loading, error, setCatalog };
}
