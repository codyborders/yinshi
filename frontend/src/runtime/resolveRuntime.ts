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
  readonly getRunner: () => Promise<RunnerIdentity | null>;
}

const defaultDependencies: RuntimeResolverDependencies = {
  getRunner: () => api.get<CloudRunner | null>("/api/settings/runner"),
};

export async function resolveRuntimeRef(
  runtime: UnresolvedRuntimeRef,
  dependencies: RuntimeResolverDependencies = defaultDependencies,
): Promise<RuntimeRef> {
  if (runtime.location === "local" || runtime.location === "hosted") {
    return runtime;
  }
  if (!runtime.runnerId || runtime.runnerPublicKey !== null) {
    throw new Error("Unresolved BYOC runtime reference is invalid");
  }
  const runner = await dependencies.getRunner();
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
