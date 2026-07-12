import { useCallback, useEffect, useState } from "react";

import { type PiReleaseNotes } from "../api/client";
import type { RuntimeTransport } from "../runtime/runtimeTransport";

export function usePiReleaseNotes(transport: RuntimeTransport) {
  const [releaseNotes, setReleaseNotes] = useState<PiReleaseNotes | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadReleaseNotes = useCallback(async (isCancelled: () => boolean = () => false) => {
    setLoading(true);
    try {
      const loadedReleaseNotes = await transport.get<PiReleaseNotes>(
        "/api/settings/pi-release-notes",
      );
      if (isCancelled()) {
        return;
      }
      setReleaseNotes(loadedReleaseNotes);
      setError(null);
    } catch (loadError) {
      if (isCancelled()) {
        return;
      }
      setError(loadError instanceof Error ? loadError.message : "Failed to load pi release notes");
    } finally {
      if (!isCancelled()) {
        setLoading(false);
      }
    }
  }, [transport]);

  useEffect(() => {
    let cancelled = false;
    void loadReleaseNotes(() => cancelled);
    return () => {
      cancelled = true;
    };
  }, [loadReleaseNotes]);

  return {
    releaseNotes,
    loading,
    error,
    refresh: loadReleaseNotes,
  };
}
