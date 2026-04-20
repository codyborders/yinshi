import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api, type PiConfigCommands } from "../api/client";

export interface UsePiCommandsReturn {
  commands: PiConfigCommands;
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
}

const EMPTY_COMMANDS: PiConfigCommands = {
  skills: [],
  prompts: [],
  extension_commands: [],
};

function getErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return fallback;
}

export function usePiCommands(refreshKey: unknown = null): UsePiCommandsReturn {
  const [commands, setCommands] = useState<PiConfigCommands>(EMPTY_COMMANDS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const isMountedRef = useRef(true);

  const reload = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const nextCommands = await api.get<PiConfigCommands>(
        "/api/settings/pi-config/commands",
      );
      if (!isMountedRef.current) {
        return;
      }
      setCommands(nextCommands);
      setError(null);
    } catch (requestError) {
      if (!isMountedRef.current) {
        return;
      }
      // 404 = no imported pi-config yet; 503 = sidecar/container not ready.
      // Both mean "no commands available right now" and are not user-facing errors.
      const isTransientEmpty =
        requestError instanceof ApiError &&
        (requestError.status === 404 || requestError.status === 503);
      if (isTransientEmpty) {
        setCommands(EMPTY_COMMANDS);
        setError(null);
      } else {
        setError(getErrorMessage(requestError, "Failed to load Pi commands"));
      }
    } finally {
      if (isMountedRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    isMountedRef.current = true;
    void reload();
    return () => {
      isMountedRef.current = false;
    };
  }, [reload, refreshKey]);

  return { commands, loading, error, reload };
}
