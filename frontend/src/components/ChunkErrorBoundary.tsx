import React from "react";

interface State {
  hasError: boolean;
  reloadRecommended: boolean;
}

const CHUNK_RELOAD_STORAGE_KEY = "yinshi.chunk-reload-signature";
const CHUNK_ERROR_PATTERNS = [
  "chunkloaderror",
  "failed to fetch dynamically imported module",
  "error loading dynamically imported module",
  "importing a module script failed",
  "loading chunk",
] as const;

function readErrorSignature(error: unknown): string {
  if (error instanceof Error) {
    return `${error.name} ${error.message}`.toLowerCase();
  }
  return String(error).toLowerCase();
}

export function isChunkLoadError(error: unknown): boolean {
  const errorSignature = readErrorSignature(error);
  return CHUNK_ERROR_PATTERNS.some((pattern) => errorSignature.includes(pattern));
}

function isStorageAccessError(error: unknown): boolean {
  if (!(error instanceof DOMException)) {
    return false;
  }
  if (error.name === "QuotaExceededError") {
    return true;
  }
  if (error.name === "SecurityError") {
    return true;
  }
  return false;
}

function readEntryScriptSignature(): string {
  if (typeof document === "undefined") {
    return "server";
  }
  const entryScript = document.querySelector<HTMLScriptElement>('script[type="module"][src]');
  const entryScriptSource = entryScript?.src;
  if (typeof entryScriptSource === "string" && entryScriptSource.length > 0) {
    return entryScriptSource;
  }
  return "unknown-entry-script";
}

export function shouldReloadForChunkError(
  storage: Pick<Storage, "getItem" | "setItem">,
  pathname: string,
  entryScriptSignature: string,
): boolean {
  if (typeof pathname !== "string") {
    throw new TypeError("pathname must be a string");
  }
  if (!pathname) {
    throw new Error("pathname must not be empty");
  }
  if (typeof entryScriptSignature !== "string") {
    throw new TypeError("entryScriptSignature must be a string");
  }
  if (!entryScriptSignature) {
    throw new Error("entryScriptSignature must not be empty");
  }
  const reloadSignature = `${pathname}:${entryScriptSignature}`;
  if (storage.getItem(CHUNK_RELOAD_STORAGE_KEY) === reloadSignature) {
    return false;
  }
  storage.setItem(CHUNK_RELOAD_STORAGE_KEY, reloadSignature);
  return true;
}

/**
 * Catches errors thrown by React.lazy() when a code-split chunk fails to
 * load (network error, deploy mismatch, etc.) and renders a retry prompt
 * instead of crashing the entire app to a white screen.
 */
export default class ChunkErrorBoundary extends React.Component<
  React.PropsWithChildren,
  State
> {
  state: State = { hasError: false, reloadRecommended: false };

  static getDerivedStateFromError(): State {
    return { hasError: true, reloadRecommended: false };
  }

  componentDidCatch(error: unknown): void {
    if (typeof window === "undefined") {
      return;
    }
    if (!isChunkLoadError(error)) {
      return;
    }
    let shouldReload = false;
    try {
      shouldReload = shouldReloadForChunkError(
        window.sessionStorage,
        window.location.pathname,
        readEntryScriptSignature(),
      );
    } catch (storageError: unknown) {
      if (!isStorageAccessError(storageError)) {
        throw storageError;
      }
      this.setState({ reloadRecommended: true });
      return;
    }
    if (shouldReload) {
      window.location.reload();
      return;
    }
    this.setState({ reloadRecommended: true });
  }

  handleRetry = () => {
    if (this.state.reloadRecommended && typeof window !== "undefined") {
      window.location.reload();
      return;
    }
    this.setState({ hasError: false, reloadRecommended: false });
  };

  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: "2rem", textAlign: "center" }}>
          <p>
            {this.state.reloadRecommended
              ? "This page needs a refresh after the latest deploy."
              : "Something went wrong loading this page."}
          </p>
          <button onClick={this.handleRetry} style={{ marginTop: "1rem" }}>
            {this.state.reloadRecommended ? "Reload page" : "Try again"}
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
