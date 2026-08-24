import { api, type CloudRunner } from "../api/client";
import type { UnresolvedRuntimeRef } from "./runtimeRef";
import type { RuntimeRef } from "./runtimeTransport";

interface RunnerIdentity {
  readonly id: string;
  readonly status: CloudRunner["status"];
  readonly noise_public_key: string | null;
  readonly noise_key_confirmed: boolean;
}

interface RuntimeResolverDependencies {
  readonly getRunner?: (signal?: AbortSignal) => Promise<RunnerIdentity | null>;
  readonly getRuntime?: (signal?: AbortSignal) => Promise<unknown>;
  readonly provisionRuntime?: (signal?: AbortSignal) => Promise<unknown>;
  readonly desktop?: boolean;
  readonly sleep?: (
    milliseconds: number,
    signal?: AbortSignal,
  ) => Promise<void>;
  readonly maxPollAttempts?: number;
}

const MANAGED_RUNTIME_POLL_INTERVAL_MS = 500;
const MANAGED_RUNTIME_MAX_POLL_ATTEMPTS = 20;
const MANAGED_RUNTIME_FAILURE_MESSAGES: Readonly<Record<string, string>> = {
  artifact_invalid: "Managed runtime artifact is invalid",
  provider_unavailable: "Managed runtime provider is unavailable",
  network_policy_failed: "Managed runtime network policy setup failed",
  bootstrap_failed: "Managed runtime setup failed",
  runner_registration_failed: "Managed runtime registration failed",
  runner_identity_changed: "Managed runtime identity changed",
  wake_timeout: "Managed runtime startup timed out",
  checkpoint_failed: "Managed runtime checkpoint failed",
  delete_failed: "Managed runtime deletion failed",
};

const defaultDependencies = {
  getRunner: () => api.get<CloudRunner | null>("/api/settings/runner"),
  getRuntime: () => api.get<unknown>("/api/runtime"),
  provisionRuntime: () => api.post<unknown>("/api/runtime/provision"),
  sleep: (milliseconds: number, signal?: AbortSignal) =>
    new Promise<void>((resolve, reject) => {
      throwIfAborted(signal);
      const timer = window.setTimeout(() => {
        signal?.removeEventListener("abort", abort);
        resolve();
      }, milliseconds);
      const abort = (): void => {
        window.clearTimeout(timer);
        reject(abortError());
      };
      signal?.addEventListener("abort", abort, { once: true });
    }),
};

function abortError(): DOMException {
  return new DOMException("Runtime resolution was aborted", "AbortError");
}

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) {
    throw abortError();
  }
}

function parseManagedRuntime(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Managed runtime response is invalid");
  }
  const response = value as Record<string, unknown>;
  if (
    typeof response.provider !== "string" ||
    typeof response.status !== "string" ||
    !["absent", "provisioning", "ready", "failed", "deleting"].includes(
      response.status,
    ) ||
    (response.history_bundle_supported !== undefined &&
      typeof response.history_bundle_supported !== "boolean")
  ) {
    throw new Error("Managed runtime response is invalid");
  }
  return response;
}

function isCanonicalRunnerPublicKey(value: unknown): value is string {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{43}$/u.test(value)) {
    return false;
  }
  try {
    const binary = atob(`${value.replace(/-/gu, "+").replace(/_/gu, "/")}=`);
    const bytes = Uint8Array.from(binary, (character) =>
      character.charCodeAt(0),
    );
    let canonical = "";
    for (const byte of bytes) {
      canonical += String.fromCharCode(byte);
    }
    return (
      bytes.length === 32 &&
      btoa(canonical)
        .replace(/\+/gu, "-")
        .replace(/\//gu, "_")
        .replace(/=+$/u, "") === value
    );
  } catch {
    return false;
  }
}

export async function provisionRuntimeRef(
  runtime: UnresolvedRuntimeRef,
  dependencies: RuntimeResolverDependencies = defaultDependencies,
  signal?: AbortSignal,
): Promise<RuntimeRef> {
  if (runtime.location !== "hosted") {
    throw new Error("Only a hosted runtime can be provisioned");
  }
  const desktop =
    dependencies.desktop ??
    (typeof window !== "undefined" && window.yinshiDesktop !== undefined);
  if (desktop) {
    throw new Error("Desktop runtime provisioning is unavailable");
  }
  const provisionRuntime =
    dependencies.provisionRuntime ?? defaultDependencies.provisionRuntime;
  const getRuntime = dependencies.getRuntime ?? defaultDependencies.getRuntime;
  throwIfAborted(signal);
  let managed = parseManagedRuntime(await provisionRuntime(signal));
  throwIfAborted(signal);
  if (managed.provider !== "fly_sprites") {
    throw new Error("Managed runtime provider is unsupported");
  }
  if (managed.status === "provisioning") {
    const sleep = dependencies.sleep ?? defaultDependencies.sleep;
    const maxPollAttempts =
      dependencies.maxPollAttempts ?? MANAGED_RUNTIME_MAX_POLL_ATTEMPTS;
    for (let attempt = 0; attempt < maxPollAttempts; attempt += 1) {
      await sleep(MANAGED_RUNTIME_POLL_INTERVAL_MS, signal);
      throwIfAborted(signal);
      managed = parseManagedRuntime(await getRuntime(signal));
      throwIfAborted(signal);
      if (managed.status !== "provisioning") break;
    }
  }
  if (managed.status === "provisioning") {
    throw new Error("Managed runtime provisioning timed out");
  }
  if (managed.status === "ready") {
    if (!isCanonicalRunnerPublicKey(managed.runner_public_key)) {
      throw new Error("Managed runtime runner public key is invalid");
    }
    return {
      location: "managed",
      runnerPublicKey: managed.runner_public_key,
      ...(typeof managed.history_bundle_supported === "boolean"
        ? { historyBundleSupported: managed.history_bundle_supported }
        : {}),
    };
  }
  if (managed.status === "failed") {
    const message =
      typeof managed.last_error === "string"
        ? MANAGED_RUNTIME_FAILURE_MESSAGES[managed.last_error]
        : undefined;
    throw new Error(message ?? "Managed runtime setup failed");
  }
  if (managed.status === "deleting") {
    throw new Error("Managed runtime is deleting");
  }
  throw new Error("Managed runtime response is invalid");
}

export async function resolveRuntimeRef(
  runtime: UnresolvedRuntimeRef,
  dependencies: RuntimeResolverDependencies = defaultDependencies,
  signal?: AbortSignal,
): Promise<RuntimeRef> {
  if (runtime.location === "local") {
    return runtime;
  }
  if (runtime.location === "hosted") {
    const desktop =
      dependencies.desktop ??
      (typeof window !== "undefined" && window.yinshiDesktop !== undefined);
    if (desktop) {
      return runtime;
    }
    const getRuntime =
      dependencies.getRuntime ?? defaultDependencies.getRuntime;
    throwIfAborted(signal);
    let managed = parseManagedRuntime(await getRuntime(signal));
    throwIfAborted(signal);
    if (managed.provider === "local") {
      return runtime;
    }
    if (managed.provider === "fly_sprites" && managed.status === "absent") {
      throw new Error("Managed runtime is not provisioned");
    }
    if (
      managed.provider === "fly_sprites" &&
      managed.status === "provisioning"
    ) {
      const sleep = dependencies.sleep ?? defaultDependencies.sleep;
      const maxPollAttempts =
        dependencies.maxPollAttempts ?? MANAGED_RUNTIME_MAX_POLL_ATTEMPTS;
      for (let attempt = 0; attempt < maxPollAttempts; attempt += 1) {
        await sleep(MANAGED_RUNTIME_POLL_INTERVAL_MS, signal);
        throwIfAborted(signal);
        managed = parseManagedRuntime(await getRuntime(signal));
        throwIfAborted(signal);
        if (managed.status !== "provisioning") {
          break;
        }
      }
      if (managed.status === "provisioning") {
        throw new Error("Managed runtime provisioning timed out");
      }
    }
    if (managed.provider === "fly_sprites" && managed.status === "deleting") {
      throw new Error("Managed runtime is deleting");
    }
    if (managed.provider !== "fly_sprites") {
      throw new Error("Managed runtime provider is unsupported");
    }
    if (managed.status === "ready") {
      if (!isCanonicalRunnerPublicKey(managed.runner_public_key)) {
        throw new Error("Managed runtime runner public key is invalid");
      }
      return {
        location: "managed",
        runnerPublicKey: managed.runner_public_key,
        ...(typeof managed.history_bundle_supported === "boolean"
          ? { historyBundleSupported: managed.history_bundle_supported }
          : {}),
      };
    }
    if (managed.status === "failed") {
      const message =
        typeof managed.last_error === "string"
          ? MANAGED_RUNTIME_FAILURE_MESSAGES[managed.last_error]
          : undefined;
      throw new Error(message ?? "Managed runtime setup failed");
    }
    throw new Error("Managed runtime response is invalid");
  }
  if (!runtime.runnerId || runtime.runnerPublicKey !== null) {
    throw new Error("Unresolved BYOC runtime reference is invalid");
  }
  const getRunner = dependencies.getRunner ?? defaultDependencies.getRunner;
  throwIfAborted(signal);
  const runner = await getRunner(signal);
  throwIfAborted(signal);
  if (
    runner === null ||
    runner.id !== runtime.runnerId ||
    runner.status === "revoked" ||
    !runner.noise_key_confirmed ||
    !runner.noise_public_key
  ) {
    throw new Error("BYOC runner is unavailable or is not paired");
  }
  return {
    location: "byoc",
    runnerId: runner.id,
    runnerPublicKey: runner.noise_public_key,
  };
}
