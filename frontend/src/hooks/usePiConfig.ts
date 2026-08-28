import { useEffect, useRef, useState } from "react";

import { ApiError, type PiConfig } from "../api/client";
import type { RuntimeTransport } from "../runtime/runtimeTransport";
import { invalidatePiCommands } from "../api/piCommandsCache";

type PiConfigMutation = "idle" | "importing" | "syncing" | "removing" | "categories";

export interface UsePiConfigReturn {
  config: PiConfig | null;
  loading: boolean;
  error: string | null;
  importing: boolean;
  syncing: boolean;
  updatingCategories: boolean;
  busy: boolean;
  loadConfig: () => Promise<void>;
  importFromGithub: (repoUrl: string) => Promise<boolean>;
  importFromUpload: (file: File) => Promise<boolean>;
  syncConfig: () => Promise<boolean>;
  removeConfig: () => Promise<boolean>;
  toggleCategory: (category: string, enabled: boolean) => Promise<boolean>;
}

function errorStatus(error: unknown): number | null {
  if (error instanceof ApiError) return error.status;
  if (error !== null && typeof error === "object" && "status" in error) {
    const status = (error as { status?: unknown }).status;
    return typeof status === "number" ? status : null;
  }
  return null;
}

function getErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return fallback;
}

function buildEnabledCategories(
  currentCategories: string[],
  category: string,
  enabled: boolean,
): string[] {
  const nextCategories = new Set(currentCategories);
  if (enabled) {
    nextCategories.add(category);
  } else {
    nextCategories.delete(category);
  }
  return Array.from(nextCategories);
}

export function usePiConfig(transport: RuntimeTransport): UsePiConfigReturn {
  const [config, setConfig] = useState<PiConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [mutation, setMutation] = useState<PiConfigMutation>("idle");
  const isMountedRef = useRef(true);
  const mutationGenerationRef = useRef(0);

  function beginMutation(nextMutation: Exclude<PiConfigMutation, "idle">): number {
    mutationGenerationRef.current += 1;
    setLoading(false);
    setMutation(nextMutation);
    return mutationGenerationRef.current;
  }

  function finishMutation(generation: number): void {
    if (isMountedRef.current && isCurrentMutation(generation)) {
      setMutation("idle");
    }
  }

  function isCurrentMutation(generation: number): boolean {
    return mutationGenerationRef.current === generation;
  }

  const importing = mutation === "importing";
  const syncing = mutation === "syncing";
  const updatingCategories = mutation === "categories";

  async function loadConfigInternal(polling: boolean): Promise<void> {
    const generation = mutationGenerationRef.current;
    if (!polling) {
      setLoading(true);
    }
    try {
      const nextConfig = await transport.get<PiConfig>("/api/settings/pi-config");
      if (!isMountedRef.current || !isCurrentMutation(generation)) {
        return;
      }
      setConfig(nextConfig);
      setError(null);
    } catch (requestError) {
      if (!isMountedRef.current || !isCurrentMutation(generation)) {
        return;
      }
      if (errorStatus(requestError) === 404) {
        setConfig(null);
        setError(null);
      } else if (!polling) {
        setError(getErrorMessage(requestError, "Failed to load Pi config"));
      }
    } finally {
      if (!polling && isMountedRef.current && isCurrentMutation(generation)) {
        setLoading(false);
      }
    }
  }

  async function loadConfig(): Promise<void> {
    await loadConfigInternal(false);
  }

  async function importFromGithub(repoUrl: string): Promise<boolean> {
    const generation = beginMutation("importing");
    setError(null);
    try {
      const nextConfig = await transport.post<PiConfig>("/api/settings/pi-config/github", {
        repo_url: repoUrl,
      });
      if (isMountedRef.current && isCurrentMutation(generation)) {
        setConfig(nextConfig);
        invalidatePiCommands(transport);
        return true;
      }
      return false;
    } catch (requestError) {
      if (isMountedRef.current && isCurrentMutation(generation)) {
        setError(getErrorMessage(requestError, "Failed to import from GitHub"));
      }
      return false;
    } finally {
      finishMutation(generation);
    }
  }

  async function importFromUpload(file: File): Promise<boolean> {
    const generation = beginMutation("importing");
    setError(null);
    try {
      const nextConfig = await transport.upload<PiConfig>(
        "/api/settings/pi-config/upload",
        file,
      );
      if (isMountedRef.current && isCurrentMutation(generation)) {
        setConfig(nextConfig);
        invalidatePiCommands(transport);
        return true;
      }
      return false;
    } catch (requestError) {
      if (isMountedRef.current && isCurrentMutation(generation)) {
        setError(getErrorMessage(requestError, "Failed to upload Pi config"));
      }
      return false;
    } finally {
      finishMutation(generation);
    }
  }

  async function syncConfig(): Promise<boolean> {
    const generation = beginMutation("syncing");
    setError(null);
    try {
      const nextConfig = await transport.post<PiConfig>("/api/settings/pi-config/sync");
      if (isMountedRef.current && isCurrentMutation(generation)) {
        setConfig(nextConfig);
        invalidatePiCommands(transport);
        return true;
      }
      return false;
    } catch (requestError) {
      if (isMountedRef.current && isCurrentMutation(generation)) {
        setError(getErrorMessage(requestError, "Failed to sync Pi config"));
      }
      return false;
    } finally {
      finishMutation(generation);
    }
  }

  async function removeConfig(): Promise<boolean> {
    const generation = beginMutation("removing");
    setError(null);
    try {
      await transport.delete("/api/settings/pi-config");
      if (isMountedRef.current && isCurrentMutation(generation)) {
        setConfig(null);
        invalidatePiCommands(transport);
        return true;
      }
      return false;
    } catch (requestError) {
      if (isMountedRef.current && isCurrentMutation(generation)) {
        setError(getErrorMessage(requestError, "Failed to remove Pi config"));
      }
      return false;
    } finally {
      finishMutation(generation);
    }
  }

  async function toggleCategory(category: string, enabled: boolean): Promise<boolean> {
    if (!config) {
      return false;
    }
    if (mutation !== "idle") {
      return false;
    }
    const generation = beginMutation("categories");
    setError(null);
    const previousConfig = config;
    const enabledCategories = buildEnabledCategories(
      previousConfig.enabled_categories,
      category,
      enabled,
    );
    const optimisticConfig: PiConfig = {
      ...previousConfig,
      enabled_categories: enabledCategories,
    };
    if (isMountedRef.current) {
      setConfig(optimisticConfig);
    }
    try {
      const nextConfig = await transport.patch<PiConfig>(
        "/api/settings/pi-config/categories",
        {
          enabled_categories: enabledCategories,
        },
      );
      if (isMountedRef.current && isCurrentMutation(generation)) {
        setConfig(nextConfig);
        invalidatePiCommands(transport);
        return true;
      }
      return false;
    } catch (requestError) {
      if (isMountedRef.current && isCurrentMutation(generation)) {
        setConfig(previousConfig);
        setError(getErrorMessage(requestError, "Failed to update Pi config categories"));
      }
      return false;
    } finally {
      finishMutation(generation);
    }
  }

  useEffect(() => {
    isMountedRef.current = true;
    void loadConfigInternal(false);
    return () => {
      isMountedRef.current = false;
    };
  }, [transport]);

  useEffect(() => {
    if (config?.status !== "cloning") {
      return undefined;
    }
    const intervalId = window.setInterval(() => {
      void loadConfigInternal(true);
    }, 2000);
    return () => {
      window.clearInterval(intervalId);
    };
  }, [config?.status, transport]);

  return {
    config,
    loading,
    error,
    importing,
    syncing,
    updatingCategories,
    busy: mutation !== "idle",
    loadConfig,
    importFromGithub,
    importFromUpload,
    syncConfig,
    removeConfig,
    toggleCategory,
  };
}
